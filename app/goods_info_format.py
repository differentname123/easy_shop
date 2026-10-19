# [功能摘要] 批量将商品名称转换为结构化信息，逐商品保存成功或失败结果。
# [输入数据] products 中的 {_id, platform, product_id, name}；本地提示词；模型 status/content/metrics/error_history。
# [数据流转/交互] 每轮查询候选、读取一次提示词 → 每批最多十件且 ID 不冲突 → 配置数量的线程调用模型
#                 （最多三次）→ string_to_object 解析 → 单品校验 → 带候选和名称条件局部写回。
# [输出数据] format_* 字段、原子失败次数及 success/failed/skipped 统计；每轮结束等待一小时。
import threading
import json
import math
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import multiprocessing
from common.common_utils import read_file_to_str, setup_logger, string_to_object, get_config
logger = setup_logger(app_name="goods_format")
from app.pdd_utils import batch_convert_pdd_urls, verify_and_convert_pdd_goods, generate_goods_promotion_url

from common.model_api import generate_content
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager


PROMPT_FILE_PATH = Path(__file__).resolve().parents[1] / "prompt" / "商品数据结构化清洗.txt"
LLM_MAX_RETRIES = 3
FORMAT_MAX_RETRIES = 3
PROMOTION_MAX_RETRIES = 3
FORMAT_WORKERS = 5
FORMAT_BATCH_SIZE = 10
ROUND_INTERVAL_SECONDS = 600
BSON_MAX_INT64 = 2 ** 63 - 1
COUNT_KEYS = ("success", "failed", "skipped")


def pending_format_query():
    """
    候选商品查询条件：
    1. 尚未成功格式化，或者标记了 need_reformat: True
    2. 或者已存在 format_info，但未达到预期 SKU 数量（支持漏洗或分批补洗）
    3. 且累计重试次数未超过阈值
    """
    return {
        "$and": [
            {
                "$or": [
                    {"format_status": {"$ne": "success"}},
                    {"need_reformat": True},
                    # 当存在 sku_info 列表且 format_info 未包含对应数量时允许继续洗
                    {"$expr": {"$gt": [{"$size": {"$ifNull": ["$sku_info", []]}}, {"$size": {"$objectToArray": {"$ifNull": ["$format_info", {}]}}}]}}
                ]
            },
            {
                "$or": [
                    {"format_retry_count": {"$exists": False}},
                    {"format_retry_count": {"$lt": FORMAT_MAX_RETRIES}},
                ]
            }
        ]
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
    """
    format_info 必含 sku_id/core_entities/decision_keywords/pricing_basis；返回 (通过, 原因)。
    pricing_basis 必含 is_inferred/structure/total_value/base_unit/equivalent_description，structure 为 [{value, unit}]。
    """
    if not isinstance(format_info, dict):
        return False, f"元素[{index}] 必须是对象"
    prefix = f"元素[{index}](sku_id={format_info.get('sku_id')})"
    if set(format_info) != {"sku_id", "core_entities", "decision_keywords", "pricing_basis"}:
        return False, f"{prefix} 必须且只能包含 sku_id、core_entities、decision_keywords、pricing_basis"
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

def check_format_info(format_info_list, input_sku_ids):
    """
    模型返回列表形貌为 [{sku_id, core_entities, decision_keywords, pricing_basis}]。
    返回: (至少一项成功, {sku_id: 合格对象}, {sku_id: 失败原因}, 批次整体错误信息)
    """
    if not isinstance(format_info_list, list):
        return False, {}, {}, "顶层结构必须是一个列表"
    output_map = {}
    for index, item in enumerate(format_info_list):
        if not isinstance(item, dict):
            continue
        sku_id = item.get("sku_id")
        if type(sku_id) not in (str, int):
            continue
        output_map[str(sku_id).strip()] = (index, item)
    results, item_errors = {}, {}
    for sku_id in input_sku_ids:
        if sku_id not in output_map:
            item_errors[sku_id] = "模型未返回该规格的数据或 sku_id 缺失"
            continue
        index, item = output_map[sku_id]
        valid, error = _check_single_item(item, index)
        if valid:
            results[sku_id] = item
        else:
            item_errors[sku_id] = error
    if not results:
        return False, {}, item_errors, "批次内所有规格均解析或校验失败"
    return True, results, item_errors, ""

def gen_goods_format_info(sku_task_batch, prompt_text=None):
    """
    sku_task_batch 为拆解后的任务列表:
    [{sku_id, product_id, product_desc, sku_desc, 价格, product_ref}]
    返回: {status, results: {sku_id: 结构化对象}, item_errors: {sku_id: 原因}, model_used, error}
    """
    outcome = {"status": "failed", "results": {}, "item_errors": {}, "model_used": None, "error": ""}
    if not isinstance(sku_task_batch, list) or not sku_task_batch:
        outcome["error"] = "批处理输入必须是非空列表"
        return outcome

    inputs = []
    input_sku_ids = []
    for task in sku_task_batch:
        sku_id = task.get("sku_id")
        name = task.get("product_desc")
        sku_desc = task.get("sku_desc", "")
        price = task.get("价格", "")

        if not sku_id or not str(sku_id).strip():
            outcome["error"] = "存在无效的 sku_id"
            return outcome
        if not isinstance(name, str) or not name.strip():
            outcome["error"] = f"规格(sku_id={sku_id}) product_desc 必须是非空字符串"
            return outcome
        if sku_id in input_sku_ids:
            raise ValueError(f"同一次模型请求不能包含重复的 sku_id: {sku_id}")

        # 严格按照规范组装大模型入参格式
        inputs.append({
            "product_desc": name,
            "sku_id": str(sku_id).strip(),
            "sku_desc": sku_desc,
            "价格": price
        })
        input_sku_ids.append(str(sku_id).strip())

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
            valid, results, item_errors, batch_error = check_format_info(string_to_object(content), input_sku_ids)
            if not valid:
                raise ValueError(f"{batch_error} | 抽样详情: {str(item_errors)[:200]}")
            outcome.update(status="success", results=results, item_errors=item_errors)
            return outcome
        except Exception as exc:
            detail = f"尝试 {attempt}/{LLM_MAX_RETRIES}: {type(exc).__name__}: {exc}"
            errors.append(detail)
            if attempt < LLM_MAX_RETRIES:
                delay = 2 ** attempt
                logger.warning("[商品/重试] 模型调用、解析或校验未通过 | 规格批次大小: [%d] | 首个 SKU: [%s] "
                               "| 尝试: [%d/%d] | 等待: [%d 秒] | 原因: [%s]",
                               len(sku_task_batch), input_sku_ids[0], attempt, LLM_MAX_RETRIES, delay,
                               " ".join(detail.split())[:400])
                time.sleep(delay)

    outcome["error"] = "；".join(errors) + (f"\n模型原文：{content}" if content else "")
    return outcome

def run_format_round(product_manager):
    """
    按 SKU 粒度并发生成结构化信息，支持断点补洗与增量写回 format_info.{sku_id}。
    """
    started = time.monotonic()
    query_condition = pending_format_query()

    products = product_manager.query(
        query_condition,
        projection={
            "_id": 1, "platform": 1, "product_id": 1, "name": 1,
            "need_reformat": 1, "sku_info": 1, "activity_price": 1,
            "format_info": 1
        },
    )

    counts = {
        "success": 0, "failed": 0, "skipped": 0,
        "normal_success": 0, "normal_failed": 0, "normal_skipped": 0,
        "reformat_success": 0, "reformat_failed": 0, "reformat_skipped": 0
    }

    if not products:
        logger.info("[调度/完成] 本轮无待处理商品 | 数量: [0] | 耗时: [%.2f 秒]", time.monotonic() - started)
        return counts

    # 1. 将商品按 SKU 拆解为待清洗的任务单元
    all_sku_tasks = []
    total_reformat_prods = 0

    for product in products:
        is_reformat = bool(product.get("need_reformat"))
        if is_reformat:
            total_reformat_prods += 1

        product_id = str(product.get("product_id")).strip()
        name = str(product.get("name") or "").strip()
        sku_info = product.get("sku_info")
        existing_format_info = product.get("format_info")
        if not isinstance(existing_format_info, dict):
            existing_format_info = {}

        # 【极简改动 1】：针对重洗（或旧版单层结构）商品，第一次拉取时直接将数据库和内存中的
        # need_reformat 设为 False、清空旧 format_info 并置为 pending，后续完全交给未完成规格逻辑自然推进
        if is_reformat or "pricing_basis" in existing_format_info:
            existing_format_info = {}
            product["format_info"] = existing_format_info
            product["need_reformat"] = False
            product_manager.update(
                {
                    "platform": product["platform"],
                    "product_id": product_id,
                    "need_reformat": False,
                    "format_status": "pending",
                    "format_info": {},
                },
                condition={"_id": product["_id"]},
                upsert=False,
            )
        else:
            product["format_info"] = existing_format_info

        product_pending_tasks = []
        if isinstance(sku_info, list) and len(sku_info) > 0:
            total_expected = len(sku_info)
            for idx, item in enumerate(sku_info, start=1):
                sku_id = f"{product_id}_{idx}"
                if sku_id in existing_format_info:
                    continue

                desc_parts = [
                    f"{k}：{v}" for k, v in item.items()
                    if k not in ("价格", "截图地址") and str(v).strip()
                ]
                sku_desc = " | ".join(desc_parts)
                price_str = str(item.get("价格", "")).strip()

                product_pending_tasks.append({
                    "sku_id": sku_id,
                    "product_id": product_id,
                    "product_desc": name,
                    "sku_desc": sku_desc,
                    "价格": price_str,
                    "product_ref": product,
                    "total_expected_skus": total_expected,
                    "is_reformat": is_reformat,
                })
        else:
            total_expected = 1
            sku_id = f"{product_id}_0"
            if sku_id not in existing_format_info:
                act_price = str(product.get("activity_price", "")).strip()
                product_pending_tasks.append({
                    "sku_id": sku_id,
                    "product_id": product_id,
                    "product_desc": name,
                    "sku_desc": "",
                    "价格": act_price,
                    "product_ref": product,
                    "total_expected_skus": 1,
                    "is_reformat": is_reformat,
                })

        # 【极简改动 2】：如果该商品下所有规格其实已在上一轮全部生成完毕，直接补标为 success，防止第二轮空转
        if not product_pending_tasks and len(existing_format_info) >= total_expected:
            product_manager.update(
                {
                    "platform": product["platform"],
                    "product_id": product_id,
                    "format_status": "success",
                    "need_reformat": False,
                    "format_error": "",
                    "format_updated_at": datetime.now(timezone.utc),
                },
                condition={"_id": product["_id"]},
                upsert=False,
            )
        else:
            all_sku_tasks.extend(product_pending_tasks)

    if not all_sku_tasks:
        logger.info("[调度/完成] 查到候选商品，其下规格均已生成完毕并已同步状态 | 耗时: [%.2f 秒]", time.monotonic() - started)
        return counts

    prompt_text = read_file_to_str(PROMPT_FILE_PATH)

    # 2. 将规格任务按 FORMAT_BATCH_SIZE (10) 切批，同一批次内 sku_id 互不重复
    batches, current_batch, batch_sku_ids = [], [], set()
    for task in all_sku_tasks:
        if len(current_batch) == FORMAT_BATCH_SIZE or task["sku_id"] in batch_sku_ids:
            batches.append(current_batch)
            current_batch, batch_sku_ids = [], set()
        current_batch.append(task)
        batch_sku_ids.add(task["sku_id"])
    if current_batch:
        batches.append(current_batch)

    logger.info(
        "[调度/本轮] 开始格式化规格 | 涉及商品: [%d] (常规: %d, 重洗: %d) | 待洗SKU数: [%d] | 批次: [%d] | 并发: [%d]",
        len(products), len(products) - total_reformat_prods, total_reformat_prods,
        len(all_sku_tasks), len(batches), FORMAT_WORKERS
    )

    def process_batch(batch, batch_counts):
        batch_started = time.monotonic()
        generated = gen_goods_format_info(batch, prompt_text)
        failure_details, skipped_ids = [], []

        for task in batch:
            sku_id = task["sku_id"]
            product = task["product_ref"]
            product_id = product["product_id"]
            is_reformat = task["is_reformat"]
            total_expected_skus = task["total_expected_skus"]

            format_info_item = generated["results"].get(sku_id)
            is_success = format_info_item is not None
            error = "" if is_success else (
                generated["error"] if generated["status"] == "failed"
                else generated["item_errors"].get(sku_id, "未知的规格解析错误")
            )

            condition = {"_id": product["_id"]}
            update_fields = {
                "platform": product["platform"],
                "product_id": product_id,
                "format_model": generated["model_used"],
                "format_updated_at": datetime.now(timezone.utc),
            }

            if is_success:
                update_fields[f"format_info.{sku_id}"] = format_info_item
                # 【极简改动 3】：直接将当前完成的 sku_id 写回共享的 product["format_info"] 字典中，
                # 这样第一轮最后一个完成的规格就能看到完整数量并当场置为 success；即便极端并发差一点，第二轮也会被改动 2 自动收口
                fmt_map = product.setdefault("format_info", {})
                fmt_map[sku_id] = format_info_item
                if len(fmt_map) >= total_expected_skus:
                    update_fields["format_status"] = "success"
                    update_fields["need_reformat"] = False
                    update_fields["format_error"] = ""
            else:
                update_fields["format_status"] = "failed"
                update_fields["format_error"] = error

            saved = product_manager.update(
                update_fields,
                condition=condition,
                increments={"format_retry_count": 0 if is_success else 1},
                upsert=False,
            )

            status_key = "success" if is_success else "failed"
            if not saved.get("update"):
                batch_counts["skipped"] += 1
                batch_counts["reformat_skipped" if is_reformat else "normal_skipped"] += 1
                skipped_ids.append(sku_id)
                continue

            batch_counts[status_key] += 1
            batch_counts[f"reformat_{status_key}" if is_reformat else f"normal_{status_key}"] += 1

            if not is_success:
                failure_details.append(f"{sku_id}: {' '.join(error.split())[:240]}")

        failed = batch_counts["failed"]
        all_failed = failed == len(batch)
        log = logger.error if all_failed else logger.warning if failed or batch_counts["skipped"] else logger.info
        message = "❌ [规格/批次完成] 全批生成失败" if all_failed else "[规格/批次完成] 规格结果已增量写入"
        detail = " ".join(generated["error"].split())[:800] if generated["status"] == "failed" else "; ".join(failure_details)[:800]

        log("%s | 规格列表: [%s] | 成功/失败/跳过: [%d/%d/%d] | 耗时: [%.2f 秒] | 失败摘要: [%s] | 跳过: [%s]",
            message, ", ".join(t["sku_id"] for t in batch),
            batch_counts["success"], failed, batch_counts["skipped"],
            time.monotonic() - batch_started, detail or "无", ", ".join(skipped_ids) or "无")

    unexpected_errors = 0
    with ThreadPoolExecutor(max_workers=FORMAT_WORKERS) as executor:
        futures = {}
        for batch in batches:
            batch_counts = {
                "success": 0, "failed": 0, "skipped": 0,
                "normal_success": 0, "normal_failed": 0, "normal_skipped": 0,
                "reformat_success": 0, "reformat_failed": 0, "reformat_skipped": 0
            }
            futures[executor.submit(process_batch, batch, batch_counts)] = (batch, batch_counts)

        for future in as_completed(futures):
            batch, batch_counts = futures[future]
            try:
                future.result()
            except Exception as exc:
                unfinished = len(batch) - (batch_counts["success"] + batch_counts["failed"] + batch_counts["skipped"])
                unexpected_errors += unfinished
                logger.exception("❌ [规格/批次异常] 本批未完成 | 首个 SKU: [%s] | 未完成: [%d] | 原因: %s",
                                 batch[0]["sku_id"], unfinished, exc)

            for key in counts.keys():
                counts[key] += batch_counts[key]

    log = logger.warning if unexpected_errors else logger.info
    log("[调度/完成] 本轮处理结束 | 规格成功/失败/跳过: [%d/%d/%d] | 常规: [%d/%d/%d] | 重洗: [%d/%d/%d] | 耗时: [%.2f 秒]",
        counts["success"], counts["failed"], counts["skipped"],
        counts["normal_success"], counts["normal_failed"], counts["normal_skipped"],
        counts["reformat_success"], counts["reformat_failed"], counts["reformat_skipped"],
        time.monotonic() - started)

    return counts

def pending_promotion_query():
    """查询没有推广链接、goods_sign 存在且重试未达上限的待转链商品。"""
    return {
        "promotion_url": {"$exists": False},
        "goods_sign": {"$exists": True},
        "$or": [
            {"promotion_retry_count": {"$exists": False}},
            {"promotion_retry_count": {"$lt": PROMOTION_MAX_RETRIES}},
        ],
    }


def run_promotion_round(product_manager):
    pdd_client_id = get_config("myself_pdd_client_id")
    pdd_client_secret = get_config("myself_pdd_client_secret")
    pdd_pid = get_config("myself_pdd_pid")
    pdd_custom_parameters = None
    """一次查询待转链商品候选，批量并行处理与 DB 更新。返回 {success, failed, skipped} 统计。"""
    started = time.monotonic()

    # 直接使用最新的查询条件，不处理任何历史旧数据兼容
    strict_query_condition = pending_promotion_query()

    # 拉取 goods_sign 及必要的基础字段
    products = product_manager.query(
        strict_query_condition,
        projection={"_id": 1, "platform": 1, "product_id": 1, "goods_sign": 1}
    )
    counts = dict.fromkeys(COUNT_KEYS, 0)

    if not products:
        logger.info("[转链调度/完成] 本轮无待处理商品 | 数量: [0] | 耗时: [%.2f 秒]", time.monotonic() - started)
        return counts

    total_products = len(products)
    PROMOTION_WORKERS = 5  # 设置并行度为 5
    logger.info("[转链调度/本轮] 开始批量转链商品 | 待处理: [%d] | 并发度: [%d]", total_products, PROMOTION_WORKERS)

    failure_details = []
    skipped_ids = []

    # 提取单次处理逻辑，供线程池并发调用
    def process_promotion(index, product):
        goods_sign = product.get("goods_sign")
        product_id = product.get("product_id")

        # 兜底校验 goods_sign
        if not goods_sign:
            status = "failed"
            error = "goods_sign 为空"
            short_url = ""
        else:
            # 直接调用 generate_goods_promotion_url 生成推广链接
            item_result = generate_goods_promotion_url(
                client_id=pdd_client_id,
                client_secret=pdd_client_secret,
                pid=pdd_pid,
                goods_sign=goods_sign,
                uid=None  # 确保 uid 为 None，避免风控问题
            )

            # 判断接口返回状态与数据
            if item_result.get("status") == "success" and item_result.get("data"):
                status = "success"
                res_data = item_result["data"]
                # 优先获取 short_url，备选 mobile_short_url
                short_url = res_data.get("short_url") or res_data.get("mobile_short_url", "")
                error = ""
                # 在日志中加入进度参数，打印当前索引和总数
                logger.info("[转链调度/成功] 商品转链成功 | 进度: [%d/%d] | product_id: [%s] ", index, total_products, product_id)
            else:
                status = "failed"
                short_url = ""
                error = item_result.get("msg") or item_result.get("error_msg", "未知生成推广链接错误")

        # 构造更新的 condition（为了防止在处理期间被别人修改，带上_id约束以及规则校验）
        condition = pending_promotion_query()
        condition["_id"] = product["_id"]

        # 构造需要写入 DB 的数据
        db_update_data = {
            "platform": product.get("platform"),
            "product_id": product_id,
            "promotion_status": status,
            "promotion_updated_at": datetime.now(timezone.utc),
            "promotion_error": error,
        }

        if status == "success":
            db_update_data["promotion_url"] = short_url

        # 将结果写回 DB，若失败累加 promotion_retry_count
        saved = product_manager.update(
            db_update_data,
            condition=condition,
            increments={"promotion_retry_count": 1 if status == "failed" else 0},
            upsert=False,
        )

        return {
            "product_id": product_id,
            "status": status,
            "error": error,
            "updated": saved["update"]
        }

    # 使用线程池并发执行单品转链与 DB 写入
    with ThreadPoolExecutor(max_workers=PROMOTION_WORKERS) as executor:
        futures = {
            executor.submit(process_promotion, index, product): product
            for index, product in enumerate(products, 1)
        }

        # 通过 as_completed 实时捕获已完成的线程结果
        for future in as_completed(futures):
            product = futures[future]
            product_id = product.get("product_id")
            try:
                result = future.result()
                status = result["status"]

                # 统计结果
                if not result["updated"]:
                    counts["skipped"] += 1
                    skipped_ids.append(product_id)
                else:
                    counts[status] += 1
                    if status == "failed":
                        failure_details.append(f"{product_id}: {' '.join(result['error'].split())[:240]}")

            except Exception as exc:
                # 处理单条线程执行中发生的未捕获异常
                counts["failed"] += 1
                error_msg = f"并行处理异常: {type(exc).__name__}: {exc}"
                failure_details.append(f"{product_id}: {error_msg[:240]}")
                logger.exception("❌ [转链调度/单品异常] 线程执行出错 | product_id: [%s] | 原因: [%s]", product_id, error_msg)

    # 打印本轮统计日志
    failed, skipped = counts["failed"], counts["skipped"]
    all_failed = failed == total_products and total_products > 0

    log = logger.error if all_failed else logger.warning if failed or skipped else logger.info
    message = "❌ [转链调度/批次完成] 全批生成失败" if all_failed else "[转链调度/批次完成] 商品结果已逐项处理"
    detail = "; ".join(failure_details)[:800] if failure_details else "无"

    log("%s | 成功/失败/跳过: [%d/%d/%d] | 耗时: [%.2f 秒] | 失败摘要: [%s] | 跳过 ID: [%s]",
        message, counts["success"], failed, skipped, time.monotonic() - started,
        detail, ", ".join(map(str, skipped_ids)) or "无")

    return counts

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


def _task_worker_loop(task_name, round_func):
    """
    通用的后台任务守护循环。
    复用健康连接，每小时执行一轮；
    每个线程维护自己独立的数据库连接对象，避免多线程下的网络读写冲突。
    """
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

                # 执行具体的一轮业务逻辑
                round_func(product_manager)

            except Exception as exc:
                product_manager = None
                logger.exception("❌ [%s调度/异常] 本轮终止，下轮重新连接 | 间隔: [%d 秒] | 原因: [%s] "
                                 "| 排查: [数据库、候选查询与线程池或网络请求]",
                                 task_name, ROUND_INTERVAL_SECONDS, " ".join(str(exc).split())[:400])

            logger.info("[%s调度/等待] 本轮结束 | 等待: [%d 秒]", task_name, ROUND_INTERVAL_SECONDS)
            time.sleep(ROUND_INTERVAL_SECONDS)

    # 移除了原本用于多进程的 KeyboardInterrupt 屏蔽，多线程下由主线程统一处理
    finally:
        if db_instance is not None:
            db_instance.close()


def format_task():
    """将商品格式化任务包装为无参函数，便于任务列表管理"""
    _task_worker_loop("格式化", run_format_round)


def promotion_task():
    """将商品转链任务包装为无参函数，便于任务列表管理"""
    _task_worker_loop("转链", run_promotion_round)


def _run_task(task):
    """为后台入口的未处理异常补充上下文并重抛；保留线程退出、不自动重启的行为。"""
    try:
        task()
    except Exception:
        logger.exception(
            "[任务/退出] 后台任务异常结束 | 任务: [%s] | 结果: [当前线程停止] "
            "| 排查: [检查对应链路的数据、文件权限及外部服务]",
            task.__name__,
        )
        raise
def extract_sku():

    prompt_text = read_file_to_str(r"W:\project\python_project\easy_shop\prompt\商品SKU提取.txt")

    # 获取 W:\project\python_project\easy_shop\common\results_success 下面的所有png文件列表
    results_success_dir = Path(r"W:\project\python_project\easy_shop\common\results_success")

    png_file_list = list(results_success_dir.glob("*.png"))
    # 分成batch，每个batch 5 张图片
    batch_size = 5

    batch_list = [png_file_list[i:i + batch_size] for i in range(0, len(png_file_list), batch_size)]
    for batch in batch_list:
        result = generate_content(prompt=prompt_text,preset_model_group="low", file_paths=batch)
        print()


def check_sku_info(sku_info_dict, expected_filenames):
    """
    校验模型返回的 SKU 提取信息是否符合要求。
    返回: (是否至少有一项成功, {成功文件: 对象}, {失败文件: 错误原因}, 批次整体错误信息)
    """
    if not isinstance(sku_info_dict, dict):
        return False, {}, {}, "顶层结构必须是一个对象(JSON Object)"

    results, item_errors = {}, {}
    for filename in expected_filenames:
        if filename not in sku_info_dict:
            item_errors[filename] = "模型未返回该图片的提取结果"
            continue

        item = sku_info_dict[filename]
        if not isinstance(item, dict):
            item_errors[filename] = f"[{filename}] 的值必须是 JSON 对象"
            continue

        if "price" not in item or "selected_spec" not in item:
            item_errors[filename] = f"[{filename}] 缺少 'price' 或 'selected_spec' 字段"
            continue

        price = item["price"]
        if type(price) not in (int, float) or price < 0:
            item_errors[filename] = f"[{filename}].price 必须是大于等于0的数字，且不能是字符串"
            continue

        spec = item["selected_spec"]
        if not isinstance(spec, str) or not spec.strip():
            item_errors[filename] = f"[{filename}].selected_spec 必须是非空字符串"
            continue

        # 规整化数据，丢弃模型可能擅自添加的多余字段
        results[filename] = {
            "price": float(price),
            "selected_spec": spec
        }

    if not results:
        return False, {}, item_errors, "批次内所有商品图片均解析或校验失败"

    return True, results, item_errors, ""


def run_sku_round(product_manager):
    """
    SKU信息提取的核心业务逻辑，作为调度循环的一轮执行。（增加并行度：5）
    """
    started = time.monotonic()
    SKU_MAX_RETRIES = 3

    # 1. 查库: 查询【没有】sku_info 字段且未达到最大重试次数的数据
    query = {
        "sku_info": {"$exists": False},
        "$or": [
            {"sku_retry_count": {"$exists": False}},
            {"sku_retry_count": {"$lt": SKU_MAX_RETRIES}},
        ],
    }
    # ⚠️ 修复点1：projection 中增加 "platform": 1
    products = product_manager.query(query, projection={"_id": 1, "product_id": 1, "platform": 1})
    counts = {"success": 0, "failed": 0, "skipped": 0}

    if not products:
        logger.info("[SKU提取/完成] 本轮无待处理商品 | 耗时: [%.2f 秒]", time.monotonic() - started)
        return counts

    product_id_map = {str(p["product_id"]): p for p in products}

    # 2. 读取本地图片并比对寻找匹配目标
    results_success_dir = Path(r"W:\project\python_project\easy_shop\common\results_success")
    prompt_text = read_file_to_str(r"W:\project\python_project\easy_shop\prompt\商品SKU提取.txt")

    all_png_files = list(results_success_dir.glob("*.png"))
    valid_file_paths = []

    for png_path in all_png_files:
        if png_path.stem in product_id_map:
            valid_file_paths.append(png_path)

    if not valid_file_paths:
        logger.info("[SKU提取/完成] 查到需要处理的商品记录，但未在本地找到对应的 PNG 图片 | 耗时: [%.2f 秒]",
                    time.monotonic() - started)
        return counts

    # 3. 按照 5 个为一个批次进行提取
    batch_size = 5
    batches = [valid_file_paths[i:i + batch_size] for i in range(0, len(valid_file_paths), batch_size)]

    SKU_WORKERS = 5  # 设置并行度为 5
    logger.info("[SKU提取/本轮] 开始提取SKU | 匹配图片: [%d] | 批次: [%d] | 并发度: [%d]", len(valid_file_paths),
                len(batches), SKU_WORKERS)

    # 提取单次批次处理逻辑，供线程池并发调用
    def process_batch(batch, batch_counts):
        expected_filenames = [p.name for p in batch]
        try:
            pending_files = list(batch)
            final_results = {}
            final_item_errors = {}
            last_batch_error = ""

            for attempt in range(1, LLM_MAX_RETRIES + 1):
                if not pending_files:
                    break
                current_expected = [p.name for p in pending_files]
                try:
                    result = generate_content(prompt=prompt_text, preset_model_group="low", file_paths=pending_files)

                    if result.get("status") != "✅ 成功":
                        error_detail = "；".join(str(e) for e in (result.get("error_history", []) or []))
                        raise RuntimeError(error_detail or result.get("content") or "模型调用失败")

                    content = result.get("content", "")
                    parsed_obj = string_to_object(content)

                    valid, results, item_errors, batch_error = check_sku_info(parsed_obj, current_expected)

                    if results:
                        final_results.update(results)
                        for fname in results:
                            final_item_errors.pop(fname, None)

                    for fname, err in item_errors.items():
                        final_item_errors[fname] = err

                    pending_files = [p for p in pending_files if p.name not in final_results]
                    if not pending_files:
                        break

                    raise ValueError(f"{batch_error or '部分商品SKU提取或校验未通过'} | 详情: {item_errors}")

                except Exception as exc:
                    last_batch_error = f"尝试 {attempt}/{LLM_MAX_RETRIES}: {type(exc).__name__}: {exc}"
                    if attempt < LLM_MAX_RETRIES:
                        delay = 2 ** attempt
                        logger.warning(
                            "⚠️ [SKU提取/重试] 模型调用或校验未通过 | 待处理: [%d/%d] | 首个文件: [%s] "
                            "| 尝试: [%d/%d] | 等待: [%d 秒] | 原因: [%s]",
                            len(pending_files), len(batch), current_expected[0],
                            attempt, LLM_MAX_RETRIES, delay, " ".join(last_batch_error.split())[:400]
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            "❌ [SKU提取/批次失败] 已达最大重试次数 | 仍失败数量: [%d/%d] | 原因: [%s]",
                            len(pending_files), len(batch), " ".join(last_batch_error.split())[:400]
                        )

            # 4. 遍历处理结果，更新回数据库
            for file_path in batch:
                filename = file_path.name
                product_id = file_path.stem
                product = product_id_map.get(product_id)

                if not product:
                    continue

                if filename in final_results:
                    sku_info = final_results[filename]

                    # ⚠️ 修复点2：更新字典中带上底层的必填项 platform 和 product_id
                    update_data = {
                        "platform": product["platform"],
                        "product_id": product["product_id"],
                        "need_reformat": True,
                        "sku_info": sku_info,
                        "activity_price": sku_info["price"],
                        "sku_status": "success",
                        "sku_error": "",
                        "sku_updated_at": datetime.now(timezone.utc),
                    }

                    saved = product_manager.update(
                        update_data,
                        condition={"_id": product["_id"]},
                        increments={"sku_retry_count": 0},
                        upsert=False
                    )

                    if saved["update"]:
                        batch_counts["success"] += 1
                    else:
                        batch_counts["skipped"] += 1
                else:
                    error_reason = final_item_errors.get(filename) or last_batch_error or "未知SKU提取错误"
                    logger.warning("⚠️ [SKU提取/单品失败] 文件: %s | 原因: %s", filename, error_reason)

                    update_data = {
                        "platform": product["platform"],
                        "product_id": product["product_id"],
                        "sku_status": "failed",
                        "sku_error": error_reason,
                        "sku_updated_at": datetime.now(timezone.utc),
                    }

                    saved = product_manager.update(
                        update_data,
                        condition={"_id": product["_id"]},
                        increments={"sku_retry_count": 1},
                        upsert=False
                    )

                    if saved["update"]:
                        batch_counts["failed"] += 1
                    else:
                        batch_counts["skipped"] += 1

        except Exception as exc:
            logger.exception("❌ [SKU提取/批次异常] 批次执行报错 | 首个文件: [%s] | 原因: %s", expected_filenames[0],
                             exc)
            batch_counts["failed"] += len(batch)

    # 4. 使用线程池并发执行批次任务
    with ThreadPoolExecutor(max_workers=SKU_WORKERS) as executor:
        futures = {}
        for batch in batches:
            batch_counts = {"success": 0, "failed": 0, "skipped": 0}
            futures[executor.submit(process_batch, batch, batch_counts)] = (batch, batch_counts)

        # 实时捕获已完成的线程结果
        for future in as_completed(futures):
            batch, batch_counts = futures[future]
            try:
                future.result()
            except Exception as exc:
                # 兜底捕获未处理严重异常
                unfinished = len(batch) - (batch_counts["success"] + batch_counts["failed"] + batch_counts["skipped"])
                batch_counts["failed"] += unfinished
                logger.exception("❌ [SKU提取/未知异常] 并发线程执行出错 | 批次首文件: [%s] | 原因: %s", batch[0].name,
                                 exc)

            # 汇总各线程池批次的执行结果
            counts["success"] += batch_counts["success"]
            counts["failed"] += batch_counts["failed"]
            counts["skipped"] += batch_counts["skipped"]

    logger.info("[SKU提取/完成] 本轮处理结束 | 成功: [%d] | 失败: [%d] | 跳过: [%d] | 耗时: [%.2f 秒]",
                counts["success"], counts["failed"], counts["skipped"], time.monotonic() - started)

    return counts

def sku_task():
    """将 SKU 提取任务包装为无参函数，便于加入统一的任务列表管理"""
    _task_worker_loop("SKU提取", run_sku_round)

if __name__ == "__main__":
    # extract_sku()


    # 可以通过注释掉下面的某一行，非常灵活地控制启停哪个任务
    tasks = [
        # sku_task,
        format_task,
        promotion_task
    ]

    threads = []
    for task in tasks:
        thread = threading.Thread(target=_run_task, args=(task,), name=task.__name__)
        thread.daemon = True  # 设置为守护线程，这样主线程因中断退出时，所有任务也会立即中止
        thread.start()
        threads.append(thread)
        logger.info("[系统/启动] 已启动 %s 线程 (TID: %d)", task.__name__, thread.ident)

    try:
        # 使用带 timeout 的 join 轮询，避免完全阻塞主线程，使得 Ctrl+C 中断信号能够被正常捕获
        for thread in threads:
            while thread.is_alive():
                thread.join(1.0)
    except KeyboardInterrupt:
        logger.info("[系统/退出] 收到中断信号，正在停止所有后台并行任务...")