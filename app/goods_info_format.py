# [功能摘要] 将待处理商品名称转换为符合协议的结构化信息，并保存每件商品的最终结果。
# [输入数据] MongoDB 商品字典（_id、name）、本地提示词，以及模型返回的 status/content/metrics/error_history。
# [数据流转/交互] 每轮查询一次 → 5 线程调用模型（最多 3 次）→ string_to_object 解析 → 结构与数量校验 → 保存一次。
# [输出数据] 写入成功或失败结果，返回 success/failed/skipped 计数并记录日志；每轮结束后等待一小时。
import json
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from pathlib import Path

from common.common_utils import read_file_to_str, setup_logger, string_to_object
from common.model_api import generate_content
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager

logger = setup_logger(app_name="goods_format")

PROMPT_FILE_PATH = Path(__file__).resolve().parents[1] / "prompt" / "商品数据结构化清洗.txt"
LLM_MAX_RETRIES = 3
FORMAT_WORKERS =1
ROUND_INTERVAL_SECONDS = 3600
BSON_MAX_INT64 = 2 ** 63 - 1


def _clean_string(value):
    """拒绝空白、首尾空格与无法编码为 UTF-8 的字符串，保护存储协议。"""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


import math  # 注意：需要在文件顶部加上这个导入，用于浮点数比对


def _check_ranked_items(items, field, text_keys):
    """校验评分列表；取消了吹毛求疵的属性组合判重逻辑，增加对大模型小瑕疵的宽容度。"""
    if not isinstance(items, list):
        return False, f"{field} 必须是列表"
    required_keys = {*text_keys, "score"}
    for index, item in enumerate(items):
        location = f"{field}[{index}]"
        if not isinstance(item, dict) or set(item) != required_keys:
            return False, f"{location} 必须且只能包含 {'、'.join((*text_keys, 'score'))}"
        if not all(_clean_string(item.get(key)) for key in text_keys):
            target = f"{location}.name" if len(text_keys) == 1 else f"{location} 的 attribute_name 和 attribute_value"
            return False, f"{target} 必须是非空且无首尾空白的字符串"
        score = item.get("score")
        if type(score) is not int or not 1 <= score <= 10:
            return False, f"{location}.score 必须是 1—10 的整数"

        # 移除了过于严苛的 normalized_key in seen 排重逻辑
    return True, ""


def _check_single_item(format_info, index):
    """【新增】抽离出的单品维度校验逻辑：拥抱浮点数，采用 isclose 解决精度陷阱。"""
    pid = format_info.get("product_id")
    location_prefix = f"元素[{index}](product_id={pid})"

    expected_keys = {"product_id", "core_entities", "decision_keywords", "pricing_basis"}
    if set(format_info) != expected_keys:
        return False, f"{location_prefix} 必须且只能包含 product_id, core_entities, decision_keywords, pricing_basis"

    # 校验评分列表
    for field, text_keys in (
            ("core_entities", ("name",)),
            ("decision_keywords", ("attribute_name", "attribute_value")),
    ):
        valid, error = _check_ranked_items(format_info.get(field), field, text_keys)
        if not valid:
            return False, f"{location_prefix} {error}"

    pricing = format_info.get("pricing_basis")
    if pricing is None:
        return False, f"{location_prefix} pricing_basis 严禁为 null，必须是一个完整的对象"

    if not isinstance(pricing, dict) or set(pricing) != {
        "is_inferred", "structure", "total_value", "base_unit", "equivalent_description",
    }:
        return False, f"{location_prefix} pricing_basis 必须包含指定字段"

    if not isinstance(pricing["is_inferred"], bool):
        return False, f"{location_prefix} pricing_basis.is_inferred 必须是布尔值"

    if pricing["equivalent_description"] is not None and not _clean_string(pricing["equivalent_description"]):
        return False, f"{location_prefix} pricing_basis.equivalent_description 必须为 null 或非空且无首尾空白的字符串"

    structure = pricing.get("structure")
    if not isinstance(structure, list) or not structure:
        return False, f"{location_prefix} pricing_basis.structure 必须是非空列表"

    total = 1.0  # 改用浮点数初始值
    for layer_index, layer in enumerate(structure):
        location = f"{location_prefix} pricing_basis.structure[{layer_index}]"
        if not isinstance(layer, dict) or set(layer) != {"value", "unit"}:
            return False, f"{location} 必须且只能包含 value、unit"

        # 【修改点 1】放宽类型，允许 int 和 float
        val = layer["value"]
        if not isinstance(val, (int, float)) or not (0 < val <= BSON_MAX_INT64):
            return False, f"{location}.value 必须是 (0, BSON_MAX_INT64] 之间的数值(允许小数)"

        if not _clean_string(layer.get("unit")):
            return False, f"{location}.unit 必须是非空且无首尾空白的字符串"
        total *= val

    # 【修改点 2】放宽类型，允许 int 和 float
    total_value = pricing["total_value"]
    if not isinstance(total_value, (int, float)) or not (0 < total_value <= BSON_MAX_INT64):
        return False, f"{location_prefix} pricing_basis.total_value 必须是大于0的数值(允许小数)"

    # 【修改点 3】解决浮点数相乘精度丢失问题（如 0.1 * 3 = 0.30000004）
    if not math.isclose(total_value, total, rel_tol=1e-5):
        return False, f"{location_prefix} pricing_basis.total_value ({total_value}) 必须等于所有层级 value 的乘积 ({total})"

    if not _clean_string(pricing.get("base_unit")) or pricing["base_unit"] != structure[-1]["unit"]:
        return False, f"{location_prefix} pricing_basis.base_unit 必须与最后一层 unit 一致"

    return True, ""


def check_format_info(format_info_list, input_product_ids):
    """【改造】取消连坐机制。校验批次数据，返回成功项与失败项。只要有≥1个成功，就不触发整体重试。
    返回: (bool 是否可采纳该批次, 成功字典 results_dict, 失败字典 item_errors_dict, 批次级错误提示 batch_error)
    """
    if not isinstance(format_info_list, list):
        return False, {}, {}, "顶层结构必须是一个列表"

    results_dict = {}
    item_errors_dict = {}

    # 建立输出映射
    output_map = {}
    for index, item in enumerate(format_info_list):
        if isinstance(item, dict) and "product_id" in item:
            output_map[item["product_id"]] = (index, item)

    # 校验每个输入的商品
    for pid in input_product_ids:
        if pid not in output_map:
            item_errors_dict[pid] = "模型未返回该商品的数据"
            continue

        index, format_info = output_map[pid]
        valid, error = _check_single_item(format_info, index)
        if valid:
            results_dict[pid] = format_info
        else:
            item_errors_dict[pid] = error

    # 只有当整批数据"全军覆没"时，才返回 False 让外面去扣动重试扳机
    if not results_dict:
        return False, {}, item_errors_dict, "批次内所有商品均解析或校验失败"

    return True, results_dict, item_errors_dict, ""


def gen_goods_format_info(product_batch):
    """执行一轮生成（批处理），兼容局部成功与失败。"""
    outcome = {"status": "failed", "results": {}, "item_errors": {}, "model_used": None, "error": ""}
    if not isinstance(product_batch, list) or not product_batch:
        outcome["error"] = "批处理输入必须是非空列表"
        return outcome

    prompt_input_list = []
    input_product_ids = []
    for p in product_batch:
        pid = p.get("product_id")
        name = p.get("name")
        if pid is None:
            outcome["error"] = f"商品(_id={p.get('_id')})缺少 product_id 字段"
            return outcome
        if not isinstance(name, str) or not name.strip():
            outcome["error"] = f"商品(product_id={pid}) name 必须是非空字符串"
            return outcome
        prompt_input_list.append({"product_desc": name, "product_id": pid})
        input_product_ids.append(pid)

    input_str = json.dumps(prompt_input_list, ensure_ascii=False)
    full_prompt = (f"{read_file_to_str(PROMPT_FILE_PATH)}\n"
                   f"<product_input>\n{input_str}\n</product_input>")

    errors = []
    content = ""
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            result = generate_content(prompt=full_prompt, preset_model_group="low")
            model_used = result.get("metrics", {}).get("model_used")
            if model_used:
                outcome["model_used"] = model_used
            if result.get("status") != "✅ 成功":
                detail = "；".join(str(error) for error in result.get("error_history", []))
                raise RuntimeError(detail or result.get("content") or "模型调用失败")

            content = result.get("content", "")
            format_info_list = string_to_object(content)

            # 接收分类校验结果
            valid, results_dict, item_errors, batch_error = check_format_info(format_info_list, input_product_ids)
            if not valid:
                raise ValueError(f"{batch_error} | 抽样详情: {str(item_errors)[:200]}")

            # 只要 valid 为 True (至少有一个商品成功)，我们就视为批次成功并跳出重试循环
            outcome["status"] = "success"
            outcome["results"] = results_dict
            outcome["item_errors"] = item_errors
            outcome["model_used"] = model_used
            return outcome

        except Exception as exc:
            detail = f"尝试 {attempt}/{LLM_MAX_RETRIES}: {type(exc).__name__}: {exc}"
            errors.append(detail)
            if attempt < LLM_MAX_RETRIES:
                delay = 2 ** attempt
                batch_preview = f"批次大小[{len(product_batch)}]首个ID[{input_product_ids[0]}]"
                logger.warning(
                    "[商品/重试] 模型调用、解析或结构校验未通过 | 批次: %s | 尝试: [%d/%d] | 等待: [%d秒] | 原因: [%s] | 排查: [模型服务与提示词协议]",
                    batch_preview, attempt, LLM_MAX_RETRIES, delay, " ".join(detail.split()),
                )
                time.sleep(delay)

    outcome["error"] = "；".join(errors) + content
    return outcome


def run_format_round(product_manager):
    """一次查询商品，进行分组批处理；改造以支持单品的局部失败和错误独立落库。"""
    started = time.monotonic()
    products = product_manager.find_pending_format_products()
    counts = {"success": 0, "failed": 0, "skipped": 0}
    if not products:
        logger.info("[调度/完成] 本轮无待处理商品 | 数量: [0] | 耗时: [%.2f秒]", time.monotonic() - started)
        return counts

    batch_size = 10
    batches = [products[i:i + batch_size] for i in range(0, len(products), batch_size)]
    logger.info("[调度/本轮] 开始处理商品 | 总数量: [%d] | 批次: [%d] | 并发: [%d]", len(products), len(batches),
                FORMAT_WORKERS)

    def _process_batch(batch):
        batch_started = time.monotonic()
        batch_result = gen_goods_format_info(batch)

        batch_counts = {"success": 0, "failed": 0, "skipped": 0}
        is_batch_failed = batch_result["status"] == "failed"

        for product in batch:
            product_id = product.get("product_id")
            if is_batch_failed:
                # 批次彻底崩溃，所有单品记为失败
                single_result = {
                    "status": "failed",
                    "format_info": None,
                    "model_used": batch_result.get("model_used"),
                    "error": batch_result.get("error", "批处理整体失败")
                }
            else:
                # 批次通过，区分该单品是个体成功还是个体失败
                if product_id in batch_result["results"]:
                    single_result = {
                        "status": "success",
                        "format_info": batch_result["results"][product_id],
                        "model_used": batch_result.get("model_used"),
                        "error": ""
                    }
                else:
                    single_result = {
                        "status": "failed",
                        "format_info": None,
                        "model_used": batch_result.get("model_used"),
                        "error": batch_result.get("item_errors", {}).get(product_id, "未知的单品解析错误")
                    }

            if not product_manager.save_format_result(product, single_result):
                logger.warning("[商品/跳过] 记录已变化或不再符合条件 | _id: [%s] | product_id: [%s] | 生成结果: [%s]",
                               product.get("_id"), product_id, single_result["status"])
                batch_counts["skipped"] += 1
                continue

            failed = single_result["status"] == "failed"
            log = logger.error if failed else logger.info
            message = "❌ [商品/格式化] 本轮生成失败，失败结果已保存" if failed else "[商品/格式化] 结构化结果已保存"
            reason = f" | 原因: [{' '.join(single_result['error'].split())}]" if failed else ""
            log("%s | _id: [%s] | product_id: [%s] | 结果: [%s] | 模型: [%s] | 批次耗时: [%.2f秒]%s",
                message, product.get("_id", "未知"), product_id, single_result["status"],
                single_result["model_used"] or "未返回", time.monotonic() - batch_started, reason)

            batch_counts[single_result["status"]] += 1

        return batch_counts

    unexpected_errors = 0
    with ThreadPoolExecutor(max_workers=FORMAT_WORKERS) as executor:
        futures = {executor.submit(_process_batch, batch): batch for batch in batches}
        for future in as_completed(futures):
            try:
                result_counts = future.result()
                counts["success"] += result_counts["success"]
                counts["failed"] += result_counts["failed"]
                counts["skipped"] += result_counts["skipped"]
            except Exception as exc:
                batch = futures[future]
                unexpected_errors += len(batch)
                first_pid = batch[0].get("product_id", "未知") if batch else "未知"
                logger.exception(
                    "❌ [商品/批次异常] 批生成或保存未完成，其他批次继续处理 | 批次首个 product_id: [%s] | 影响数量: [%d] | 原因: [%s]",
                    first_pid, len(batch), " ".join(str(exc).split()),
                )

    logger.info(
        "[调度/完成] 本轮处理结束 | 成功: [%d] | 失败: [%d] | 跳过: [%d] | 未计入结果的异常: [%d] | 耗时: [%.2f秒]",
        counts["success"], counts["failed"], counts["skipped"], unexpected_errors, time.monotonic() - started,
    )
    return counts


def main_controller():
    """常驻调度，复用健康连接；轮级异常后重建连接，每轮结束等待一小时，退出时关闭连接。"""
    db_instance = product_manager = None
    try:
        while True:
            try:
                if product_manager is None:
                    if db_instance is not None:
                        db_instance.close()
                        db_instance = None
                    db_instance = gen_db_object()
                    db_instance.ping()
                    product_manager = ProductManager(db_instance)
                run_format_round(product_manager)
            except Exception as exc:
                product_manager = None
                logger.exception(
                    "❌ [调度/异常] 本轮终止，下轮重新连接 | 间隔: [%d秒] | 原因: [%s] | 排查: [数据库连接、候选查询与线程池调度]",
                    ROUND_INTERVAL_SECONDS, " ".join(str(exc).split()),
                )
                # : 故障连接沿用原关闭时机，在下一轮重连前或退出时释放；立即关闭会改变关闭失败时的重试时机。
            logger.info("[调度/等待] 本轮结束 | 等待: [%d秒]", ROUND_INTERVAL_SECONDS)
            time.sleep(ROUND_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("[系统/退出] 收到中断，停止格式化任务")
    finally:
        if db_instance is not None:
            db_instance.close()

def get_recent_successful_formats():
    db_instance = gen_db_object()
    db_instance.ping()
    product_manager = ProductManager(db_instance)
    products = product_manager.find_recent_successful_formats(hours=24)
    # 只保留 name 和 format_info 字段
    result = [{"name": product.get("name"), "format_info": product.get("format_info")} for product in products]

    simple_result = [{"name": product.get("name"), "delivery_quantity": product.get("format_info",{}).get("delivery_quantity")} for product in products]

    # 筛选出 delivery_quantity 为 None 的记录
    filtered_result = [{"name": product.get("name"), "format_info": product.get("format_info")} for product in products  if product.get("format_info",{}).get("delivery_quantity") is None]


    return result, simple_result




if __name__ == "__main__":
    main_controller()

    # get_recent_successful_formats()
    # print()
    #
