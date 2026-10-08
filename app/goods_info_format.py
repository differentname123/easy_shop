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
from app.pdd_utils import batch_convert_pdd_urls, verify_and_convert_pdd_goods
from common.common_utils import read_file_to_str, setup_logger, string_to_object, get_config
from common.model_api import generate_content
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager

logger = setup_logger(app_name="goods_format")

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
        selected_spec = product.get("sku_info",{}).get("selected_spec")
        inputs.append({"product_desc": name, "product_id": product_id, "selected_spec": selected_spec})
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
                    "need_reformat":False
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




def pending_promotion_query():
    """新采集商品尚无 promotion_* 相关推广链接字段，或转链失败但未达到尝试上限。"""
    return {
        "promotion_status": {"$ne": "success"},
        "$or": [
            {"promotion_retry_count": {"$exists": False}},
            {"promotion_retry_count": {"$lt": PROMOTION_MAX_RETRIES}},
        ],
    }


def run_promotion_round(product_manager):
    pdd_client_id = get_config("nana_pdd_client_id")
    pdd_client_secret = get_config("nana_pdd_client_secret")
    pdd_pid = get_config("nana_pdd_pid")
    pdd_custom_parameters = None
    """一次查询待转链商品候选，批量并行处理与 DB 更新。返回 {success, failed, skipped} 统计。"""
    started = time.monotonic()

    # ================= 核心修改点 =================
    # 动态构建包含 _source_api 约束的查询条件。
    # 为了防止与原 pending_promotion_query 里的 "$or" 发生键冲突，采用 "$and" 嵌套的方式。
    def get_strict_promotion_query():
        query = pending_promotion_query()
        original_or = query.pop("$or", [])
        # 强制要求 _source_api 为 "group" 或字段本身不存在
        query["$and"] = [
            {"$or": original_or},
            {"$or": [{"_source_api": "group"}, {"_source_api": {"$exists": False}}]}
        ]
        return query

    strict_query_condition = get_strict_promotion_query()
    # ==============================================

    # 根据要求，拉取 product_url 及必要的基础字段
    products = product_manager.query(
        strict_query_condition,
        projection={"_id": 1, "platform": 1, "product_id": 1, "product_url": 1}
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
        product_url = product.get("product_url")
        product_id = product.get("product_id")
        promotion_info = None

        # 兜底：如果没有 url 字段，直接标记失败
        if not product_url:
            status = "failed"
            error = "product_url 为空"
            short_url = ""
        else:
            # 调取洗链功能，网络请求交由各线程独立阻塞
            item_result = verify_and_convert_pdd_goods(
                client_id=pdd_client_id,
                client_secret=pdd_client_secret,
                pid=pdd_pid,
                original_url=product_url,
                goods_id=product_id,
            )

            # 判断顶层状态与转链信息(convert_info)状态
            if item_result.get("status") == "success" and item_result.get("convert_info", {}).get(
                    "status") == "success":
                status = "success"
                short_url = item_result["convert_info"].get("h5_jump_url", "")

                # 提取推广信息
                goods_info = item_result.get("goods_info", {})
                promotion_info = {
                    "promotion_rate": goods_info.get("promotion_rate"),
                    "estimated_commission": goods_info.get("estimated_commission"),
                    "has_mall_coupon": goods_info.get("has_mall_coupon")
                }

                error = ""
                # 在日志中加入进度参数，打印当前索引和总数
                logger.info("[转链调度/成功] 商品转链成功 | 进度: [%d/%d] | product_id: [%s] ", index, total_products,
                            product_id)
            else:
                status = "failed"
                short_url = ""
                # 兼容获取错误信息
                error = item_result.get("msg") or item_result.get("error_msg", "未知转链错误")

        # 构造更新的 condition（为了防止在处理期间被别人修改，带上_id约束以及更严谨的规则校验）
        condition = get_strict_promotion_query()
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
            if promotion_info is not None:
                db_update_data["promotion_info"] = promotion_info

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
                logger.exception("❌ [转链调度/单品异常] 线程执行出错 | product_id: [%s] | 原因: [%s]", product_id,
                                 error_msg)

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
        result = generate_content(prompt=prompt_text, model="gpt-5.6-max", file_paths=batch)
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
    SKU信息提取的核心业务逻辑，作为调度循环的一轮执行。
    """
    started = time.monotonic()

    # 1. 查库: 查询【没有】sku_info 字段的数据
    query = {
        "sku_info": {"$exists": False}
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

    logger.info("[SKU提取/本轮] 开始提取SKU | 匹配图片: [%d] | 批次: [%d]", len(valid_file_paths), len(batches))

    for batch in batches:
        expected_filenames = [p.name for p in batch]

        try:
            result = generate_content(prompt=prompt_text, model="gpt-5.6-max", file_paths=batch)

            if result.get("status") != "✅ 成功":
                error_detail = "；".join(str(e) for e in (result.get("error_history", []) or []))
                logger.error("❌ [SKU提取/批次失败] 模型调用失败 | 原因: %s", error_detail)
                counts["failed"] += len(batch)
                continue

            content = result.get("content", "")
            parsed_obj = string_to_object(content)

            valid, results, item_errors, batch_error = check_sku_info(parsed_obj, expected_filenames)

            if not valid and not results:
                logger.error("❌ [SKU提取/批次失败] %s | 详情: %s", batch_error, item_errors)
                counts["failed"] += len(batch)
                continue

            # 4. 遍历处理结果，更新回数据库
            for file_path in batch:
                filename = file_path.name
                product_id = file_path.stem
                product = product_id_map.get(product_id)

                if not product:
                    continue

                if filename in results:
                    sku_info = results[filename]

                    # ⚠️ 修复点2：更新字典中带上底层的必填项 platform 和 product_id
                    update_data = {
                        "platform": product["platform"],
                        "product_id": product["product_id"],
                        "need_reformat":True,
                        "sku_info": sku_info,
                        "activity_price": sku_info["price"]
                    }

                    saved = product_manager.update(
                        update_data,
                        condition={"_id": product["_id"]},
                        upsert=False
                    )

                    if saved["update"]:
                        counts["success"] += 1
                    else:
                        counts["skipped"] += 1
                else:
                    counts["failed"] += 1
                    logger.warning("⚠️ [SKU提取/单品失败] 文件: %s | 原因: %s", filename, item_errors.get(filename))

        except Exception as exc:
            logger.exception("❌ [SKU提取/批次异常] 批次执行报错 | 首个文件: [%s] | 原因: %s", expected_filenames[0],
                             exc)
            counts["failed"] += len(batch)

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
        sku_task,
        # format_task,
        # promotion_task
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