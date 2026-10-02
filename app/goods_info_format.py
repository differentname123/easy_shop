import concurrent.futures
import json
import time
import unicodedata
from html import escape
from pathlib import Path

from common.common_utils import read_file_to_str, setup_logger, string_to_object
from common.model_api import generate_content
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager

logger = setup_logger(app_name="goods_format")

PROMPT_FILE_PATH = Path(__file__).resolve().parents[1] / "prompt" / "商品数据结构化清洗.txt"
LLM_MAX_RETRIES = 3
ROUND_INTERVAL_SECONDS = 3600
BSON_MAX_INT64 = 2 ** 63 - 1


def _clean_string(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def check_format_info(format_info):
    """检查提示词的结构协议和数量一致性，不判断商品语义是否真实。"""
    if not isinstance(format_info, dict) or set(format_info) != {
        "core_entities", "decision_keywords", "delivery_quantity",
    }:
        return False, "顶层必须且只能包含 core_entities、decision_keywords、delivery_quantity"

    # 1. 校验 core_entities
    core_items = format_info["core_entities"]
    if not isinstance(core_items, list):
        return False, "core_entities 必须是列表"
    seen_core = set()
    previous_score = 10
    for index, item in enumerate(core_items):
        location = f"core_entities[{index}]"
        if not isinstance(item, dict) or set(item) != {"name", "score"}:
            return False, f"{location} 必须且只能包含 name、score"
        if not _clean_string(item["name"]):
            return False, f"{location}.name 必须是非空且无首尾空白的字符串"
        score = item["score"]
        if type(score) is not int or not 1 <= score <= 10:
            return False, f"{location}.score 必须是 1—10 的整数"
        if score > previous_score:
            return False, "core_entities 必须按 score 降序排列"
        previous_score = score
        normalized_name = " ".join(unicodedata.normalize("NFKC", item["name"]).casefold().split())
        if normalized_name in seen_core:
            return False, f"{location}.name 重复"
        seen_core.add(normalized_name)

    # 2. 校验 decision_keywords
    decision_items = format_info["decision_keywords"]
    if not isinstance(decision_items, list):
        return False, "decision_keywords 必须是列表"
    seen_decision = set()
    previous_score = 10
    for index, item in enumerate(decision_items):
        location = f"decision_keywords[{index}]"
        if not isinstance(item, dict) or set(item) != {"attribute_name", "attribute_value", "score"}:
            return False, f"{location} 必须且只能包含 attribute_name、attribute_value、score"
        if not _clean_string(item["attribute_name"]) or not _clean_string(item["attribute_value"]):
            return False, f"{location} 的 attribute_name 和 attribute_value 必须是非空且无首尾空白的字符串"
        score = item["score"]
        if type(score) is not int or not 1 <= score <= 10:
            return False, f"{location}.score 必须是 1—10 的整数"
        if score > previous_score:
            return False, "decision_keywords 必须按 score 降序排列"
        previous_score = score
        # 使用 属性名+属性值 联合去重
        combined_key = f"{item['attribute_name']}:{item['attribute_value']}"
        normalized_name = " ".join(unicodedata.normalize("NFKC", combined_key).casefold().split())
        if normalized_name in seen_decision:
            return False, f"{location} 属性名与属性值的组合重复"
        seen_decision.add(normalized_name)

    # 3. 校验 delivery_quantity
    quantity = format_info["delivery_quantity"]
    if quantity is None:
        return True, ""

    if not isinstance(quantity, dict) or set(quantity) != {
        "is_inferred", "structure", "total_value", "base_unit", "equivalent_description"
    }:
        return False, "delivery_quantity 必须为 null 或包含 is_inferred, structure, total_value, base_unit, equivalent_description 的对象"

    if not isinstance(quantity["is_inferred"], bool):
        return False, "delivery_quantity.is_inferred 必须是布尔值 (True 或 False)"

    eq_desc = quantity["equivalent_description"]
    if eq_desc is not None:
        if not isinstance(eq_desc, str) or not _clean_string(eq_desc):
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

def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复: {key}")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f"非标准 JSON 数值: {value}")


def gen_goods_format_info(good_desc):
    """一件商品的一轮生成；内部尝试不计入数据库的失败轮数。"""
    outcome = {"status": "failed", "format_info": None, "model_used": None, "error": ""}
    if not isinstance(good_desc, str) or not good_desc.strip():
        outcome["error"] = "商品 name 必须是非空字符串"
        return outcome

    # 提示词读取失败属于轮级故障，不消耗商品的失败次数。
    full_prompt = (f"{read_file_to_str(PROMPT_FILE_PATH)}\n"
                   f"<product_input>\n{escape(good_desc, quote=False)}\n</product_input>")
    errors = []
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            result = generate_content(prompt=full_prompt, preset_model_group="low")
            model_used = result.get("metrics", {}).get("model_used")
            if model_used:
                outcome["model_used"] = model_used
            if result.get("status") != "✅ 成功":
                detail = "；".join(str(error) for error in result.get("error_history", []))
                raise RuntimeError(detail or result.get("content") or "模型调用失败")
            raw_response = result.get("content", "")

            format_info = string_to_object(raw_response)

            valid, error = check_format_info(format_info)
            if not valid:
                raise ValueError(error)
            return {"status": "success", "format_info": format_info,
                    "model_used": model_used, "error": ""}
        except Exception as exc:
            detail = f"尝试 {attempt}/{LLM_MAX_RETRIES}: {type(exc).__name__}: {exc}"
            errors.append(detail)
            exhausted = attempt == LLM_MAX_RETRIES
            log = logger.error if exhausted else logger.warning
            log("[商品/格式化] %s", detail)
            if not exhausted:
                time.sleep(2 ** attempt)
    outcome["error"] = "；".join(errors)
    return outcome


def run_format_round(product_manager):
    """只查询一次本轮候选列表，每件商品仅保存一次最终结果。"""
    products = product_manager.find_pending_format_products()
    counts = {"success": 0, "failed": 0, "skipped": 0}
    logger.info("[调度/本轮] 待格式化商品: [%d]", len(products))

    # 定义单件商品的完整处理流程
    def _process_single_product(product):
        result = gen_goods_format_info(product.get("name"))
        # 存储异常直接结束本轮，不能当作商品格式化失败或再次补计次数。
        if product_manager.save_format_result(product, result):
            return result["status"]
        else:
            logger.warning("[商品/跳过] 记录已变化或不再符合条件 | _id: [%s]", product["_id"])
            return "skipped"

    # 使用并行度为 5 的线程池执行处理
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_process_single_product, p) for p in products]
        for future in concurrent.futures.as_completed(futures):
            try:
                status = future.result()
                counts[status] += 1
            except Exception as e:
                logger.error("[商品/异常] 并发处理商品时发生未捕获异常: %s", e)

    logger.info("[调度/完成] 成功: [%d] 失败: [%d] 跳过: [%d]",
                counts["success"], counts["failed"], counts["skipped"])
    return counts


def main_controller():
    """独立常驻进程：每轮完成或异常后等待一小时，退出时关闭连接。"""
    db_instance = None
    product_manager = None
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
            except Exception:
                logger.exception("[调度/异常] 本轮终止，未补计商品失败次数，一小时后重试")
                product_manager = None
            logger.info("[调度/等待] 本轮结束，等待 [%d] 秒", ROUND_INTERVAL_SECONDS)
            time.sleep(ROUND_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("[系统/退出] 收到中断，停止格式化任务")
    finally:
        if db_instance is not None:
            db_instance.close()


if __name__ == "__main__":
    main_controller()