# [功能摘要] 将待处理商品名称转换为符合协议的结构化信息，并保存每件商品的最终结果。
# [输入数据] MongoDB 商品字典（_id、name）、本地提示词，以及模型返回的 status/content/metrics/error_history。
# [数据流转/交互] 每轮查询一次 → 5 线程调用模型（最多 3 次）→ string_to_object 解析 → 结构与数量校验 → 保存一次。
# [输出数据] 写入成功或失败结果，返回 success/failed/skipped 计数并记录日志；每轮结束后等待一小时。

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
    seen, previous_score = set(), 10
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
        if score > previous_score:
            return False, f"{field} 必须按 score 降序排列"
        previous_score = score
        # : 属性组合沿用冒号拼接；字段包含冒号时可能误判重复，需确认业务后再改为元组。
        combined_key = ":".join(item[key] for key in text_keys)
        normalized_key = " ".join(unicodedata.normalize("NFKC", combined_key).casefold().split())
        if normalized_key in seen:
            suffix = ".name 重复" if len(text_keys) == 1 else " 属性名与属性值的组合重复"
            return False, f"{location}{suffix}"
        seen.add(normalized_key)
    return True, ""


def check_format_info(format_info):
    """校验结构与数量一致性，返回 (bool, 错误原因)，不判断商品语义。

    输入必须仅含 core_entities: [{name, score}]、decision_keywords: [{attribute_name, attribute_value, score}]，
    以及 delivery_quantity: None 或 {is_inferred, structure: [{value, unit}], total_value, base_unit, equivalent_description}。
    """
    if not isinstance(format_info, dict) or set(format_info) != {
        "core_entities", "decision_keywords", "delivery_quantity",
    }:
        return False, "顶层必须且只能包含 core_entities、decision_keywords、delivery_quantity"
    # : 两类评分列表允许为空；是否至少包含一个实体或属性属于业务协议，保持原行为。
    for field, text_keys in (
        ("core_entities", ("name",)),
        ("decision_keywords", ("attribute_name", "attribute_value")),
    ):
        valid, error = _check_ranked_items(format_info[field], field, text_keys)
        if not valid:
            return False, error

    quantity = format_info["delivery_quantity"]
    if quantity is None:
        return True, ""
    if not isinstance(quantity, dict) or set(quantity) != {
        "is_inferred", "structure", "total_value", "base_unit", "equivalent_description",
    }:
        return False, "delivery_quantity 必须为 null 或包含 is_inferred, structure, total_value, base_unit, equivalent_description 的对象"
    if not isinstance(quantity["is_inferred"], bool):
        return False, "delivery_quantity.is_inferred 必须是布尔值 (True 或 False)"
    if quantity["equivalent_description"] is not None and not _clean_string(quantity["equivalent_description"]):
        return False, "delivery_quantity.equivalent_description 必须为 null 或非空且无首尾空白的字符串"

    structure = quantity["structure"]
    if not isinstance(structure, list) or not structure:
        return False, "delivery_quantity.structure 必须是非空列表"
    total = 1
    for index, layer in enumerate(structure):
        location = f"delivery_quantity.structure[{index}]"
        if not isinstance(layer, dict) or set(layer) != {"value", "unit"}:
            return False, f"{location} 必须且只能包含 value、unit"
        if type(layer["value"]) is not int or not 1 <= layer["value"] <= BSON_MAX_INT64:
            return False, f"{location}.value 必须是 BSON int64 范围内的正整数"
        if not _clean_string(layer["unit"]):
            return False, f"{location}.unit 必须是非空且无首尾空白的字符串"
        total *= layer["value"]
    if type(quantity["total_value"]) is not int or not 1 <= quantity["total_value"] <= BSON_MAX_INT64:
        return False, "delivery_quantity.total_value 必须是 BSON int64 范围内的正整数"
    if quantity["total_value"] != total:
        return False, "delivery_quantity.total_value 必须等于所有层级 value 的乘积"
    if not _clean_string(quantity["base_unit"]) or quantity["base_unit"] != structure[-1]["unit"]:
        return False, "delivery_quantity.base_unit 必须与最后一层 unit 一致"
    return True, ""


def gen_goods_format_info(good_desc):
    """执行一轮生成，返回 {status, format_info, model_used, error}；重试不增加失败轮数，最终日志由保存节点聚合。"""
    outcome = {"status": "failed", "format_info": None, "model_used": None, "error": ""}
    if not isinstance(good_desc, str) or not good_desc.strip():
        outcome["error"] = "商品 name 必须是非空字符串"
        return outcome
    # 读取故障直接抛出，不转换为商品失败结果；调度层沿用原异常处理策略。
    full_prompt = (f"{read_file_to_str(PROMPT_FILE_PATH)}\n"
                   f"<product_input>\n{escape(good_desc, quote=False)}\n</product_input>")
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
            format_info = string_to_object(content)
            valid, error = check_format_info(format_info)
            if not valid:
                raise ValueError(error)
            return {"status": "success", "format_info": format_info, "model_used": model_used, "error": ""}
        except Exception as exc:
            detail = f"尝试 {attempt}/{LLM_MAX_RETRIES}: {type(exc).__name__}: {exc}"
            errors.append(detail)
            if attempt < LLM_MAX_RETRIES:
                delay = 2 ** attempt
                logger.warning(
                    "[商品/重试] 模型调用、解析或结构校验未通过 | 商品: [%r] | 尝试: [%d/%d] | 等待: [%d秒] | 原因: [%s] | 排查: [模型服务与提示词协议]",
                    good_desc[:80], attempt, LLM_MAX_RETRIES, delay, " ".join(detail.split()),
                )
                time.sleep(delay)
    outcome["error"] = "；".join(errors) + content
    return outcome


def run_format_round(product_manager):
    """一次查询商品 [{_id, name, ...}]，每件保存一次 {status, format_info, model_used, error}；返回 {success, failed, skipped}。"""
    started = time.monotonic()
    products = product_manager.find_pending_format_products()
    counts = {"success": 0, "failed": 0, "skipped": 0}
    if not products:
        logger.info("[调度/完成] 本轮无待处理商品 | 数量: [0] | 耗时: [%.2f秒]", time.monotonic() - started)
        return counts
    logger.info("[调度/本轮] 开始处理商品 | 数量: [%d] | 并发: [%d]", len(products), FORMAT_WORKERS)

    def _process_single_product(product):
        """处理商品 {_id, name, ...}，仅保存最终结果；返回 success/failed/skipped，基础设施异常交给本轮调度。"""
        product_started = time.monotonic()
        result = gen_goods_format_info(product.get("name"))
        if not product_manager.save_format_result(product, result):
            logger.warning("[商品/跳过] 记录已变化或不再符合条件 | _id: [%s] | 生成结果: [%s]", product["_id"], result["status"])
            return "skipped"
        failed = result["status"] == "failed"
        log = logger.error if failed else logger.info
        message = "❌ [商品/格式化] 本轮生成失败，失败结果已保存" if failed else "[商品/格式化] 结构化结果已保存"
        reason = f" | 原因: [{' '.join(result['error'].split())}] | 排查: [商品名称、模型服务与提示词协议]" if failed else ""
        log("%s | _id: [%s] | 结果: [%s] | 模型: [%s] | 耗时: [%.2f秒]%s",
            message, product.get("_id", "未知"), result["status"], result["model_used"] or "未返回",
            time.monotonic() - product_started, reason)
        return result["status"]

    unexpected_errors = 0
    with ThreadPoolExecutor(max_workers=FORMAT_WORKERS) as executor:
        futures = {executor.submit(_process_single_product, product): product for product in products}
        for future in as_completed(futures):
            try:
                counts[future.result()] += 1
            except Exception as exc:
                # : 原实现记录单件异常后继续，且不计入 failed；与“存储异常终止本轮”的原注释冲突，保持实际行为。
                unexpected_errors += 1
                product = futures[future]
                product_id = product.get("_id", "未知") if isinstance(product, dict) else "未知"
                logger.exception(
                    "❌ [商品/异常] 生成或保存未完成，其他商品继续处理 | _id: [%s] | 原因: [%s] | 排查: [商品字段、提示词文件与数据库写入]",
                    product_id, " ".join(str(exc).split()),
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


if __name__ == "__main__":
    main_controller()
