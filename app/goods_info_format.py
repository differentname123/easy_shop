# [功能摘要] 批量将商品名称转换为结构化信息，逐商品保存成功或失败结果。
# [输入数据] products 中的 {_id, platform, product_id, name}；本地提示词；模型 status/content/metrics/error_history。
# [数据流转/交互] 每轮查询候选、读取一次提示词 → 每批最多十件且 ID 不冲突 → 配置数量的线程调用模型
#                 （最多三次）→ string_to_object 解析 → 单品校验 → 带候选和名称条件局部写回。
# [输出数据] format_* 字段、原子失败次数及 success/failed/skipped 统计；每轮结束等待一小时。

import json
import math
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common.common_utils import read_file_to_str, setup_logger, string_to_object
from common.model_api import generate_content
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager

logger = setup_logger(app_name="goods_format")

PROMPT_FILE_PATH = Path(__file__).resolve().parents[1] / "prompt" / "商品数据结构化清洗.txt"
LLM_MAX_RETRIES = 3
FORMAT_MAX_RETRIES = 3
FORMAT_WORKERS = 5
FORMAT_BATCH_SIZE = 10
ROUND_INTERVAL_SECONDS = 600
BSON_MAX_INT64 = 2 ** 63 - 1
COUNT_KEYS = ("success", "failed", "skipped")


def pending_format_query():
    """新采集商品尚无 format_* 字段，自然进入候选；成功或累计失败三轮后不再处理。"""
    return {
        "format_status": {"$ne": "success"},
        "$or": [
            {"format_retry_count": {"$exists": False}},
            {"format_retry_count": {"$lt": FORMAT_MAX_RETRIES}},
        ],
    }


def _is_clean_string(value):
    """拒绝空白、首尾空格和不能编码为 UTF-8 的文本，保护模型输出协议。"""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_positive_number(value):
    """数量支持整数和小数，但不能把布尔值当数量，也不能接收 NaN/无穷大。"""
    return type(value) in (int, float) and 0 < value <= BSON_MAX_INT64 and math.isfinite(value)


def _check_ranked_items(items, field, text_keys):
    """items 为 [{文本键, score}]；text_keys 指定 name 或 attribute_name/attribute_value，返回 (通过, 原因)。"""
    if not isinstance(items, list):
        return False, f"{field} 必须是列表"
    required_keys = {*text_keys, "score"}
    for index, item in enumerate(items):
        location = f"{field}[{index}]"
        if not isinstance(item, dict) or set(item) != required_keys:
            return False, f"{location} 必须且只能包含 {'、'.join((*text_keys, 'score'))}"
        if not all(_is_clean_string(item[key]) for key in text_keys):
            return False, f"{location} 的 {'、'.join(text_keys)} 必须是非空、无首尾空白的 UTF-8 字符串"
        if type(item["score"]) is not int or not 1 <= item["score"] <= 10:
            return False, f"{location}.score 必须是 1—10 的整数"
    return True, ""


def _check_single_item(format_info, index):
    """format_info 必含 product_id/core_entities/decision_keywords/pricing_basis；返回 (通过, 原因)。
    pricing_basis 必含 is_inferred/structure/total_value/base_unit/equivalent_description，structure 为 [{value, unit}]。
    """
    if not isinstance(format_info, dict):
        return False, f"元素[{index}] 必须是对象"
    prefix = f"元素[{index}](product_id={format_info.get('product_id')})"
    if set(format_info) != {"product_id", "core_entities", "decision_keywords", "pricing_basis"}:
        return False, f"{prefix} 必须且只能包含 product_id、core_entities、decision_keywords、pricing_basis"
    for field, text_keys in (
        ("core_entities", ("name",)),
        ("decision_keywords", ("attribute_name", "attribute_value")),
    ):
        valid, error = _check_ranked_items(format_info[field], field, text_keys)
        if not valid:
            return False, f"{prefix} {error}"
    pricing = format_info["pricing_basis"]
    if not isinstance(pricing, dict) or set(pricing) != {
        "is_inferred", "structure", "total_value", "base_unit", "equivalent_description",
    }:
        return False, f"{prefix} pricing_basis 必须是包含指定字段的完整对象，不能为 null"
    if type(pricing["is_inferred"]) is not bool:
        return False, f"{prefix} pricing_basis.is_inferred 必须是布尔值"
    description = pricing["equivalent_description"]
    if description is not None and not _is_clean_string(description):
        return False, f"{prefix} pricing_basis.equivalent_description 必须为 null 或非空、无首尾空白的字符串"
    structure = pricing["structure"]
    if not isinstance(structure, list) or not structure:
        return False, f"{prefix} pricing_basis.structure 必须是非空列表"
    total = 1.0
    for layer_index, layer in enumerate(structure):
        location = f"{prefix} pricing_basis.structure[{layer_index}]"
        if not isinstance(layer, dict) or set(layer) != {"value", "unit"}:
            return False, f"{location} 必须且只能包含 value、unit"
        if not _is_positive_number(layer["value"]):
            return False, f"{location}.value 必须是 (0, BSON_MAX_INT64] 范围内的有限数值，允许小数"
        if not _is_clean_string(layer["unit"]):
            return False, f"{location}.unit 必须是非空、无首尾空白的字符串"
        total *= layer["value"]
    total_value = pricing["total_value"]
    if not _is_positive_number(total_value):
        return False, f"{prefix} pricing_basis.total_value 必须是 (0, BSON_MAX_INT64] 范围内的有限数值"
    if not math.isclose(total_value, total, rel_tol=1e-5):
        return False, f"{prefix} pricing_basis.total_value ({total_value}) 必须等于各层 value 的乘积 ({total})"
    if not _is_clean_string(pricing["base_unit"]) or pricing["base_unit"] != structure[-1]["unit"]:
        return False, f"{prefix} pricing_basis.base_unit 必须与最后一层 unit 一致"
    return True, ""


def check_format_info(format_info_list, input_product_ids):
    """模型列表形貌为 [{product_id, core_entities, decision_keywords, pricing_basis}]，输入 ID 必须互不重复。
    返回 (至少一项成功, {ID: 合格对象}, {ID: 失败原因}, 批次错误)，仅全批失败触发整体重试。
    """
    if not isinstance(format_info_list, list):
        return False, {}, {}, "顶层结构必须是一个列表"
    output_map = {}
    for index, item in enumerate(format_info_list):
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id")
        if type(product_id) not in (str, int):
            continue
        # : 保留模型重复 ID 以后一个为准、额外 ID 忽略的规则；不改成批次协议错误。
        output_map[product_id] = (index, item)
    results, item_errors = {}, {}
    for product_id in input_product_ids:
        if product_id not in output_map:
            item_errors[product_id] = "模型未返回该商品的数据或 product_id 类型不匹配"
            continue
        index, item = output_map[product_id]
        valid, error = _check_single_item(item, index)
        if valid:
            results[product_id] = item
        else:
            item_errors[product_id] = error
    if not results:
        return False, {}, item_errors, "批次内所有商品均解析或校验失败"
    return True, results, item_errors, ""


def gen_goods_format_info(product_batch, prompt_text=None):
    """product_batch 为 [{product_id, name}]；返回 {status, results: {ID: 对象}, item_errors: {ID: 原因}, model_used, error}。
    保留最多三次模型尝试及 2/4 秒退避；部分成功立即采纳，失败单品本轮不另行调用模型。
    """
    outcome = {"status": "failed", "results": {}, "item_errors": {}, "model_used": None, "error": ""}
    if not isinstance(product_batch, list) or not product_batch:
        outcome["error"] = "批处理输入必须是非空列表"
        return outcome
    inputs, input_ids = [], []
    for product in product_batch:
        if not isinstance(product, dict):
            outcome["error"] = "批处理中的每个商品必须是字典"
            return outcome
        product_id, name = product.get("product_id"), product.get("name")
        # : 输入中一个商品的 ID/名称无效仍使整批失败，保留原规则。
        if type(product_id) not in (str, int) or not str(product_id).strip():
            outcome["error"] = f"商品(_id={product.get('_id')})缺少有效 product_id"
            return outcome
        if not isinstance(name, str) or not name.strip():
            outcome["error"] = f"商品(product_id={product_id}) name 必须是非空字符串"
            return outcome
        if product_id in input_ids:
            raise ValueError("同一次模型请求不能包含重复 product_id，请将不同平台的同名 ID 分批")
        inputs.append({"product_desc": name, "product_id": product_id})
        input_ids.append(product_id)
    if prompt_text is None:
        prompt_text = read_file_to_str(PROMPT_FILE_PATH)
    full_prompt = f"{prompt_text}\n<product_input>\n{json.dumps(inputs, ensure_ascii=False)}\n</product_input>"
    errors, content = [], ""
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            result = generate_content(prompt=full_prompt, preset_model_group="low")
            model_used = (result.get("metrics") or {}).get("model_used")
            if model_used:
                outcome["model_used"] = model_used
            if result.get("status") != "✅ 成功":
                detail = "；".join(str(error) for error in result.get("error_history", []) or [])
                raise RuntimeError(detail or result.get("content") or "模型调用失败")
            content = result.get("content", "")
            valid, results, item_errors, batch_error = check_format_info(string_to_object(content), input_ids)
            if not valid:
                raise ValueError(f"{batch_error} | 抽样详情: {str(item_errors)[:200]}")
            # : 批内至少一项成功便停止重试；失败单品只记一次本轮失败，不单独补跑。
            outcome.update(status="success", results=results, item_errors=item_errors)
            return outcome
        except Exception as exc:
            detail = f"尝试 {attempt}/{LLM_MAX_RETRIES}: {type(exc).__name__}: {exc}"
            errors.append(detail)
            if attempt < LLM_MAX_RETRIES:
                delay = 2 ** attempt
                logger.warning("[商品/重试] 模型调用、解析或校验未通过 | 批次大小: [%d] | 首个 ID: [%s] "
                               "| 尝试: [%d/%d] | 等待: [%d 秒] | 原因: [%s] | 排查: [模型服务、提示词协议]",
                               len(product_batch), input_ids[0], attempt, LLM_MAX_RETRIES, delay,
                               " ".join(detail.split())[:400])
                time.sleep(delay)
    outcome["error"] = "；".join(errors) + (f"\n模型原文：{content}" if content else "")
    return outcome


def run_format_round(product_manager):
    """一次查询 {_id, platform, product_id, name} 候选，独立写回结果；返回 {success, failed, skipped}。
    批次进度在写入确认后立即累计，后续异常不会丢失已经保存商品的统计。
    """
    started = time.monotonic()
    products = product_manager.query(
        pending_format_query(), projection={"_id": 1, "platform": 1, "product_id": 1, "name": 1},
    )
    counts = dict.fromkeys(COUNT_KEYS, 0)
    if not products:
        logger.info("[调度/完成] 本轮无待处理商品 | 数量: [0] | 耗时: [%.2f 秒]", time.monotonic() - started)
        return counts
    prompt_text = read_file_to_str(PROMPT_FILE_PATH)
    batches, batch, batch_ids = [], [], set()
    for product in products:
        product_id = product["product_id"]
        if len(batch) == FORMAT_BATCH_SIZE or product_id in batch_ids:
            batches.append(batch)
            batch, batch_ids = [], set()
        batch.append(product)
        batch_ids.add(product_id)
    if batch:
        batches.append(batch)
    logger.info("[调度/本轮] 开始格式化商品 | 商品: [%d] | 批次: [%d] | 每批上限: [%d] | 并发: [%d]",
                len(products), len(batches), FORMAT_BATCH_SIZE, FORMAT_WORKERS)

    def process_batch(batch, batch_counts):
        """batch 为候选商品列表；batch_counts 是当前批次已确认保存/跳过的计数，异常时供主线程统计。"""
        batch_started = time.monotonic()
        generated = gen_goods_format_info(batch, prompt_text)
        failure_details, skipped_ids = [], []
        for product in batch:
            product_id = product["product_id"]
            format_info = generated["results"].get(product_id)
            status = "success" if format_info is not None else "failed"
            error = "" if status == "success" else (
                generated["error"] if generated["status"] == "failed"
                else generated["item_errors"].get(product_id, "未知的单品解析错误")
            )
            condition = pending_format_query()
            condition.update(
                _id=product["_id"],
                name={"$eq": product["name"]} if "name" in product else {"$exists": False},
            )
            saved = product_manager.update(
                {
                    "platform": product["platform"], "product_id": product_id,
                    "format_status": status, "format_info": format_info,
                    "format_model": generated["model_used"], "format_updated_at": datetime.now(timezone.utc),
                    "format_error": error,
                },
                condition=condition, increments={"format_retry_count": 1 if status == "failed" else 0}, upsert=False,
            )
            if not saved["update"]:
                batch_counts["skipped"] += 1
                skipped_ids.append(product_id)
                continue
            batch_counts[status] += 1
            if status == "failed":
                failure_details.append(f"{product_id}: {' '.join(error.split())[:240]}")
        failed, skipped = batch_counts["failed"], batch_counts["skipped"]
        all_failed = failed == len(batch)
        log = logger.error if all_failed else logger.warning if failed or skipped else logger.info
        message = "❌ [商品/批次完成] 全批生成失败，失败结果已保存" if all_failed else "[商品/批次完成] 商品结果已逐项处理"
        detail = " ".join(generated["error"].split())[:800] if generated["status"] == "failed" else "; ".join(failure_details)[:800]
        log("%s | 商品 ID: [%s] | 成功/失败/跳过: [%d/%d/%d] | 模型: [%s] | 耗时: [%.2f 秒] "
            "| 失败摘要: [%s] | 跳过 ID: [%s] | 排查: [模型协议、名称变化或候选条件]", message,
            ", ".join(str(product["product_id"]) for product in batch), batch_counts["success"], failed, skipped,
            generated["model_used"] or "未返回", time.monotonic() - batch_started, detail or "无",
            ", ".join(map(str, skipped_ids)) or "无")

    unexpected_errors = 0
    with ThreadPoolExecutor(max_workers=FORMAT_WORKERS) as executor:
        futures = {}
        for batch in batches:
            batch_counts = dict.fromkeys(COUNT_KEYS, 0)
            futures[executor.submit(process_batch, batch, batch_counts)] = (batch, batch_counts)
        for future in as_completed(futures):
            batch, batch_counts = futures[future]
            try:
                future.result()
            except Exception as exc:
                # : 保留批次异常后其他批次继续的规则；已保存记录不回滚，未完成记录留待后续轮次。
                unfinished = len(batch) - sum(batch_counts.values())
                unexpected_errors += unfinished
                logger.exception("❌ [商品/批次异常] 本批未完成，其他批次继续 | 首个 ID: [%s] | 未完成: [%d] "
                                 "| 已成功/失败/跳过: [%d/%d/%d] | 原因: [%s] | 排查: [异常链、模型接口、MongoDB 写入]",
                                 batch[0]["product_id"], unfinished, batch_counts["success"], batch_counts["failed"],
                                 batch_counts["skipped"], " ".join(str(exc).split())[:400])
            for key in COUNT_KEYS:
                counts[key] += batch_counts[key]
    log = logger.warning if unexpected_errors else logger.info
    log("[调度/完成] 本轮处理结束 | 成功/失败/跳过: [%d/%d/%d] | 异常未完成: [%d] | 耗时: [%.2f 秒] "
        "| 排查: [异常批次日志]", counts["success"], counts["failed"], counts["skipped"], unexpected_errors,
        time.monotonic() - started)
    return counts


def main_controller():
    """复用健康连接，每小时执行一轮；轮级异常保留原重连规则，退出时关闭连接。"""
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
                logger.exception("❌ [调度/异常] 本轮终止，下轮重新连接 | 间隔: [%d 秒] | 原因: [%s] "
                                 "| 排查: [数据库、提示词路径、候选查询与线程池]",
                                 ROUND_INTERVAL_SECONDS, " ".join(str(exc).split())[:400])
                # : 沿用下一轮前关闭故障连接的时机；等待一小时期间仍持有该连接池。
            logger.info("[调度/等待] 本轮结束 | 等待: [%d 秒]", ROUND_INTERVAL_SECONDS)
            time.sleep(ROUND_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("[系统/退出] 收到中断，停止格式化任务")
    finally:
        if db_instance is not None:
            db_instance.close()


def get_recent_successful_formats(hours=24, limit=0):
    """查询近期成功记录，按 category 分组返回。"""
    with closing(gen_db_object()) as db_instance:
        db_instance.ping()
        product_manager = ProductManager(db_instance)
        # : 原规则按商品 updated_at 筛选，并非 format_updated_at；采集刷新可能使旧格式化结果入选。
        products = product_manager.query(
            {"format_status": "success", "updated_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=hours)}},
            sort=[("updated_at", -1)], limit=limit,
        )

    # 使用 defaultdict 初始化嵌套字典，每个 category 下包含 result 和 simple_result 两个列表
    grouped_data = defaultdict(lambda: {"result": [], "simple_result": []})

    for product in products:
        # 提取 category，如果数据库中可能没有该字段，默认归入 "uncategorized"
        category = product.get("category", "uncategorized")

        name = product.get("name")
        format_info = product.get("format_info")
        pricing_basis = (format_info or {}).get("pricing_basis")

        # 将数据分别追加到对应 category 的两个列表中
        grouped_data[category]["result"].append({
            "name": name,
            "format_info": format_info
        })

        # : 保留原返回键 pricing_basis，但当前协议仅定义 pricing_basis；是否改为 total_value 需业务确认。
        grouped_data[category]["simple_result"].append({
            "name": name,
            "pricing_basis": pricing_basis
        })


    # 转换为普通 dict 并返回
    grouped_data_info = dict(grouped_data)
    return grouped_data_info


if __name__ == "__main__":
    # get_recent_successful_formats()
    main_controller()
