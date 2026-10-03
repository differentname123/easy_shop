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


def _check_ranked_items(items, field, text_keys):
    """校验评分列表；items 为 [{name, score}] 或 [{attribute_name, attribute_value, score}]，返回 (bool, 错误原因)。"""
    if not isinstance(items, list):
        return False, f"{field} 必须是列表"
    required_keys = {*text_keys, "score"}
    seen = set()  # 移除了 previous_score 变量
    for index, item in enumerate(items):
        location = f"{field}[{index}]"
        if not isinstance(item, dict) or set(item) != required_keys:
            return False, f"{location} 必须且只能包含 {'、'.join((*text_keys, 'score'))}"
        if not all(_clean_string(item[key]) for key in text_keys):
            target = f"{location}.name" if len(text_keys) == 1 else f"{location} 的 attribute_name 和 attribute_value"
            return False, f"{target} 必须是非空且无首尾空白的字符串"
        score = item["score"]
        if type(score) is not int or not 1 <= score <= 10:
            return False, f"{location}.score 必须是 1—10 的整数"

        # 已删除这里的按 score 降序排列的校验逻辑

        # : 属性组合沿用冒号拼接；字段包含冒号时可能误判重复，需确认业务后再改为元组。
        combined_key = ":".join(item[key] for key in text_keys)
        normalized_key = " ".join(unicodedata.normalize("NFKC", combined_key).casefold().split())
        if normalized_key in seen:
            suffix = ".name 重复" if len(text_keys) == 1 else " 属性名与属性值的组合重复"
            return False, f"{location}{suffix}"
        seen.add(normalized_key)
    return True, ""


def check_format_info(format_info_list, input_product_ids):
    """校验结构与数量一致性，返回 (bool, 错误原因)，不判断商品语义。

    输入必须是一个列表，元素包含 product_id、core_entities、decision_keywords、pricing_basis。
    额外校验大模型输出的 product_id 集合与输入的 product_id 集合必须完全一致。
    """
    if not isinstance(format_info_list, list):
        return False, "顶层结构必须是一个列表"

    output_product_ids = []
    for index, item in enumerate(format_info_list):
        if not isinstance(item, dict):
            return False, f"列表元素[{index}]必须是字典"
        if "product_id" not in item:
            return False, f"列表元素[{index}]缺少 product_id 字段"
        output_product_ids.append(item["product_id"])

    # 校验 product_id 集合严格一致（不能多、不能少、不能变）
    if set(output_product_ids) != set(input_product_ids) or len(output_product_ids) != len(input_product_ids):
        return False, f"返回的 product_id 集合与输入不一致。输入: {input_product_ids}, 输出: {output_product_ids}"

    # 对列表里的每个商品详细结构进行原有的规则校验
    for index, format_info in enumerate(format_info_list):
        pid = format_info["product_id"]
        location_prefix = f"元素[{index}](product_id={pid})"

        expected_keys = {"product_id", "core_entities", "decision_keywords", "pricing_basis"}
        if set(format_info) != expected_keys:
            return False, f"{location_prefix} 必须且只能包含 product_id, core_entities, decision_keywords, pricing_basis"

        # 校验评分列表
        for field, text_keys in (
                ("core_entities", ("name",)),
                ("decision_keywords", ("attribute_name", "attribute_value")),
        ):
            valid, error = _check_ranked_items(format_info[field], field, text_keys)
            if not valid:
                return False, f"{location_prefix} {error}"

        pricing = format_info["pricing_basis"]
        if pricing is None:
            return False, f"{location_prefix} pricing_basis 严禁为 null，必须是一个完整的对象"

        if not isinstance(pricing, dict) or set(pricing) != {
            "is_inferred", "structure", "total_value", "base_unit", "equivalent_description",
        }:
            return False, f"{location_prefix} pricing_basis 必须包含 is_inferred, structure, total_value, base_unit, equivalent_description 的对象"

        if not isinstance(pricing["is_inferred"], bool):
            return False, f"{location_prefix} pricing_basis.is_inferred 必须是布尔值 (True 或 False)"

        if pricing["equivalent_description"] is not None and not _clean_string(pricing["equivalent_description"]):
            return False, f"{location_prefix} pricing_basis.equivalent_description 必须为 null 或非空且无首尾空白的字符串"

        structure = pricing["structure"]
        if not isinstance(structure, list) or not structure:
            return False, f"{location_prefix} pricing_basis.structure 必须是非空列表"

        total = 1
        for layer_index, layer in enumerate(structure):
            location = f"{location_prefix} pricing_basis.structure[{layer_index}]"
            if not isinstance(layer, dict) or set(layer) != {"value", "unit"}:
                return False, f"{location} 必须且只能包含 value、unit"
            if type(layer["value"]) is not int or not 1 <= layer["value"] <= BSON_MAX_INT64:
                return False, f"{location}.value 必须是 BSON int64 范围内的正整数"
            if not _clean_string(layer["unit"]):
                return False, f"{location}.unit 必须是非空且无首尾空白的字符串"
            total *= layer["value"]

        if type(pricing["total_value"]) is not int or not 1 <= pricing["total_value"] <= BSON_MAX_INT64:
            return False, f"{location_prefix} pricing_basis.total_value 必须是 BSON int64 范围内的正整数"
        if pricing["total_value"] != total:
            return False, f"{location_prefix} pricing_basis.total_value 必须等于所有层级 value 的乘积"
        if not _clean_string(pricing["base_unit"]) or pricing["base_unit"] != structure[-1]["unit"]:
            return False, f"{location_prefix} pricing_basis.base_unit 必须与最后一层 unit 一致"

    return True, ""


def gen_goods_format_info(product_batch):
    """执行一轮生成（批处理），返回 {status, results, model_used, error}；重试不增加失败轮数。
    results 是一个以 product_id 为 key，结构化数据为 value 的字典。
    """
    outcome = {"status": "failed", "results": {}, "model_used": None, "error": ""}
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

    # 将组装好的列表转为JSON字符串灌入 prompt
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

            # 引入 list 和 product_id 的双重校验
            valid, error = check_format_info(format_info_list, input_product_ids)
            if not valid:
                raise ValueError(error)

            # 将列表转换为按 product_id 映射的字典，方便 run_format_round 进行对应存储
            results_dict = {item["product_id"]: item for item in format_info_list}
            return {"status": "success", "results": results_dict, "model_used": model_used, "error": ""}

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
    """一次查询商品，进行分组(默认10件/组)批处理；每件单品分别保存其 {status, format_info, model_used, error} 结果。"""
    started = time.monotonic()
    products = product_manager.find_pending_format_products()
    counts = {"success": 0, "failed": 0, "skipped": 0}
    if not products:
        logger.info("[调度/完成] 本轮无待处理商品 | 数量: [0] | 耗时: [%.2f秒]", time.monotonic() - started)
        return counts

    # 分组，每组 10 条
    batch_size = 10
    batches = [products[i:i + batch_size] for i in range(0, len(products), batch_size)]
    logger.info("[调度/本轮] 开始处理商品 | 总数量: [%d] | 批次: [%d] | 并发: [%d]", len(products), len(batches),
                FORMAT_WORKERS)

    def _process_batch(batch):
        """处理一个商品批次并分别保存结果；返回该批次的 success/failed/skipped 统计，基础设施异常交由外层接管。"""
        batch_started = time.monotonic()
        batch_result = gen_goods_format_info(batch)

        batch_counts = {"success": 0, "failed": 0, "skipped": 0}
        is_batch_failed = batch_result["status"] == "failed"

        # 拆解批处理结果并针对每一件商品进行落库
        for product in batch:
            product_id = product.get("product_id")
            if is_batch_failed:
                single_result = {
                    "status": "failed",
                    "format_info": None,
                    "model_used": batch_result.get("model_used"),
                    "error": batch_result.get("error", "批处理整体失败")
                }
            else:
                product_format_info = batch_result["results"].get(product_id)
                single_result = {
                    "status": "success",
                    "format_info": product_format_info,
                    "model_used": batch_result.get("model_used"),
                    "error": ""
                }

            if not product_manager.save_format_result(product, single_result):
                logger.warning("[商品/跳过] 记录已变化或不再符合条件 | _id: [%s] | product_id: [%s] | 生成结果: [%s]",
                               product.get("_id"), product_id, single_result["status"])
                batch_counts["skipped"] += 1
                continue

            failed = single_result["status"] == "failed"
            log = logger.error if failed else logger.info
            message = "❌ [商品/格式化] 本轮生成失败，失败结果已保存" if failed else "[商品/格式化] 结构化结果已保存"
            reason = f" | 原因: [{' '.join(single_result['error'].split())}] | 排查: [商品名称、模型服务与提示词协议]" if failed else ""
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
                    "❌ [商品/批次异常] 批生成或保存未完成，其他批次继续处理 | 批次首个 product_id: [%s] | 影响数量: [%d] | 原因: [%s] | 排查: [商品字段、提示词文件与数据库写入]",
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
