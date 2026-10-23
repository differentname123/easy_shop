# -- coding: utf-8 --
""":authors:
    zhuxiaohu
:create_date:
    2026/10/3 13:56
:last_date:
    2026/10/22 14:00
:description:
    性价比商品搜索服务（仅保留价格、真人核实、计量单位筛选及包含/排除过滤支持）
"""
import math
import os
import re
import json
import threading
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

# 假设你的 common 模块在这个路径下可用
from common.common_utils import setup_logger
logger = setup_logger(app_name="goods_search")

from common.goods_common_utils import gen_goods_segment_info
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager


db_instance = gen_db_object()
db_instance.ping()
product_manager = ProductManager(db_instance)

app = FastAPI(title="性价比商品搜索服务")
# 开启 GZip 压缩，大幅减少网络传输体积
app.add_middleware(GZipMiddleware, minimum_size=500)

sku_unit_base_mapping = {
    "质量": {
        "mg": 1,
        "g": 1000,
        "克": 1000,
        "斤": 500000,
        "kg": 1000000,
        "Kg": 1000000,
        "KG": 1000000,
        "千克": 1000000,
        "磅": 453592.37
    },
    "容积与体积": {
        "ml": 1,
        "mL": 1,
        "ML": 1,
        "毫升": 1,
        "L": 1000,
        "升": 1000
    },
    "数据存储": {
        "G": 1,
        "GB": 1,
        "TB": 1024
    },
    "电池容量": {
        "mAh": 1,
        "Ah": 1000,
        "AH": 1000
    },
    "功率": {
        "W": 1,
        "kW": 1000
    },
    "生物活性成分": {
        "iu": 1,
        "IU": 1
    },
    "速率与排量": {
        "L/日": 1,
        "升/天": 1
    }
}

# 预构建单位到类别、以及单位到基数乘数的反向查找字典（避免每次请求重复构建）
UNIT_TO_CATEGORY = {}
UNIT_TO_MULTIPLIER = {}
for _cat, _units in sku_unit_base_mapping.items():
    for _u, _mul in _units.items():
        UNIT_TO_CATEGORY[_u] = _cat
        UNIT_TO_MULTIPLIER[_u] = _mul

# ============== 商品分词结果本地 JSON 缓存与持久化逻辑 ==============
SEGMENT_CACHE_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "segment_cache.json")
segment_cache_lock = threading.Lock()
_raw_gen_goods_segment_info = gen_goods_segment_info
_segment_memory_cache = None


def _load_segment_cache_unsafe() -> dict:
    """内部读取分词缓存 JSON 文件（调用方需持有锁）"""
    if not os.path.exists(SEGMENT_CACHE_FILE_PATH):
        return {}
    try:
        with open(SEGMENT_CACHE_FILE_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            data = json.loads(content)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"读取分词缓存文件失败: {e}")
        return {}


def _save_segment_cache_unsafe(data: dict):
    """内部原子写入分词缓存 JSON 文件（调用方需持有锁，使用临时文件替换保障安全）"""
    temp_path = f"{SEGMENT_CACHE_FILE_PATH}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, SEGMENT_CACHE_FILE_PATH)
    except Exception as e:
        logger.error(f"写入分词缓存文件失败: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise e


def gen_goods_segment_info(name: str = "", *args, **kwargs):
    """带内存与本地 JSON 双重持久化的分词包装函数：命中相同输入直接快速返回"""
    global _segment_memory_cache
    cache_key = str(name) if name is not None else ""
    if not cache_key.strip():
        return []

    with segment_cache_lock:
        if _segment_memory_cache is None:
            _segment_memory_cache = _load_segment_cache_unsafe()
        if cache_key in _segment_memory_cache:
            return list(_segment_memory_cache[cache_key])

    words = _raw_gen_goods_segment_info(name=name, *args, **kwargs)

    if isinstance(words, (list, tuple, set)):
        normalized_words = list(words)
        if normalized_words:
            with segment_cache_lock:
                if _segment_memory_cache is None:
                    _segment_memory_cache = _load_segment_cache_unsafe()
                _segment_memory_cache[cache_key] = normalized_words
                try:
                    _save_segment_cache_unsafe(_segment_memory_cache)
                except Exception:
                    pass
        return normalized_words

    return words
# =====================================================================

# ============== 商品错误反馈本地 JSON 安全维护逻辑 ==============
FEEDBACK_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback_products.json")
feedback_file_lock = threading.Lock()


class FeedbackRequest(BaseModel):
    product_id: str
    name: str
    content: str = ""


def _load_feedback_data_unsafe() -> dict:
    """内部读取反馈 JSON 文件（调用方需持有锁）"""
    if not os.path.exists(FEEDBACK_FILE_PATH):
        return {}
    try:
        with open(FEEDBACK_FILE_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            data = json.loads(content)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"读取反馈文件失败: {e}")
        return {}


def _save_feedback_data_unsafe(data: dict):
    """内部原子写入反馈 JSON 文件（调用方需持有锁，使用临时文件替换保障安全）"""
    temp_path = f"{FEEDBACK_FILE_PATH}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, FEEDBACK_FILE_PATH)
    except Exception as e:
        logger.error(f"写入反馈文件失败: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise e


@app.post("/api/feedback")
def api_submit_feedback(req: FeedbackRequest):
    pid = str(req.product_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="商品ID不能为空")

    beijing_tz = timezone(timedelta(hours=8))
    now_beijing_str = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

    with feedback_file_lock:
        data = _load_feedback_data_unsafe()
        data[pid] = {
            "feedback_time": now_beijing_str,
            "name": (req.name or "").strip(),
            "content": (req.content or "").strip()
        }
        _save_feedback_data_unsafe(data)

    logger.info(f"记录商品反馈: product_id=[{pid}], name=[{req.name}], content=[{req.content}]")
    return {"status": "success"}
# =====================================================================


def search_product(keyword: str, min_match_score=10, hours=24, limit=0):
    target_kw = (keyword or "").strip().lower()

    # 1. 调用分词工具获取分词词列表
    words = gen_goods_segment_info(name=keyword)

    # 对分词结果进行清洗与大小写统一，过滤空字符
    search_terms = []
    if isinstance(words, (list, tuple, set)):
        search_terms = [str(w).strip().lower() for w in words if str(w).strip()]

    # 兜底保障：若分词结果为空但原始关键字非空，则降级使用原始关键字
    if not search_terms and target_kw:
        search_terms = [target_kw]

    if not search_terms:
        return {"results": [], "dominant_unit": "", "unit_counts": {}}

    # 读取已反馈有误的商品列表，用于后续过滤
    with feedback_file_lock:
        feedback_data = _load_feedback_data_unsafe()

    # 构建数据库查询条件：将分词正则下推到 MongoDB，大幅减少拉取到内存的数据量
    query_cond = {
        "format_status": "success",
        "updated_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=hours)},
    }
    if len(search_terms) == 1:
        query_cond["name"] = {"$regex": re.escape(search_terms[0]), "$options": "i"}
    else:
        query_cond["$and"] = [
            {"name": {"$regex": re.escape(t), "$options": "i"}} for t in search_terms
        ]

    # 从数据库获取近期匹配数据
    raw_products = product_manager.query(
        query_cond,
        sort=[("updated_at", -1)], limit=limit,
    )

    def _is_valid_sku_price(price_val, min_limit_price):
        """
        校验规格价格有效性：
        1. 必须为大于 0 的有效浮点数/整数；
        2. 若商品存在全局 activity_price，任何规格价格均不得低于该阈值（排除浮点数精度误差）。
        """
        if type(price_val) not in (int, float) or not math.isfinite(price_val) or price_val <= 0:
            return False
        if min_limit_price is not None:
            if round(float(price_val), 2) < round(float(min_limit_price), 2):
                return False
        return True

    # 2. 提取前端展示和计算所需的字段，过滤无效商品并按 SKU 展开 format_info
    filtered_products = []
    for product in raw_products:
        product_url = product.get("promotion_url") or product.get("product_url")
        if not product_url:
            continue

        product_id = str(product.get("product_id") or product_url)
        if product_id in feedback_data:
            continue

        base_name = product.get("name") or ""
        if not base_name or not all(term in base_name.lower() for term in search_terms):
            continue

        raw_format_info = product.get("format_info")
        if not isinstance(raw_format_info, dict) or not raw_format_info:
            continue

        sku_info_list = product.get("sku_info") if isinstance(product.get("sku_info"), list) else []
        need_reformat = product.get("need_reformat")
        need_update_sku = product.get("need_update_sku")
        sku_status = product.get("sku_status")

        # 仅当已完成真实 SKU 抓取（need_update_sku 为 False 或 sku_status 为 success）且完成重洗时，才标记为已确认规格 (1)
        is_confirmed_sku = bool(
            sku_info_list
            and (need_update_sku is False or sku_status == "success")
            and (need_reformat is False or need_reformat == "false")
        )

        _source_api = product.get("_source_api", "group")
        if "group" in _source_api:
            _source_api = "group"
        else:
            _source_api = "no_group"

        base_orig_price = product.get("original_price")

        # 获取商品级别的最低活动价作为规格价格过滤基准
        raw_act_price = product.get("activity_price")
        min_act_price = None
        if isinstance(raw_act_price, (int, float)) and math.isfinite(raw_act_price) and raw_act_price > 0:
            min_act_price = float(raw_act_price)

        # 组装当前商品下的 (唯一SKU标识, 展示名称, SKU价格, 单个SKU的format_info字典) 列表
        sku_entries = []
        if "pricing_basis" in raw_format_info and isinstance(raw_format_info.get("pricing_basis"), dict):
            sku_price = raw_format_info.get("price")
            if not _is_valid_sku_price(sku_price, min_act_price):
                continue
            sku_entries.append((f"{product_id}_0", base_name, sku_price, raw_format_info))
        else:
            if sku_info_list:
                for idx, sku_item in enumerate(sku_info_list, start=1):
                    sku_id = f"{product_id}_{idx}"
                    sku_fmt = raw_format_info.get(sku_id)
                    if not isinstance(sku_fmt, dict) or not isinstance(sku_fmt.get("pricing_basis"), dict):
                        continue
                    desc_parts = [
                        f"{k}:{v}" for k, v in (sku_item or {}).items()
                        if k not in ("价格", "截图地址") and str(v).strip()
                    ]
                    display_name = f"{base_name} 【{' | '.join(desc_parts)}】" if desc_parts else base_name
                    sku_price = sku_fmt.get("price")
                    # 过滤低于 activity_price 或非法的规格
                    if not _is_valid_sku_price(sku_price, min_act_price):
                        continue
                    sku_entries.append((sku_id, display_name, sku_price, sku_fmt))

            # 兜底结果也必须使用清洗通过的价格，禁止使用商品基础价补位
            if not sku_entries:
                for fallback_idx, (sku_id, sku_fmt) in enumerate(raw_format_info.items(), start=1):
                    if isinstance(sku_fmt, dict) and isinstance(sku_fmt.get("pricing_basis"), dict):
                        sku_price = sku_fmt.get("price")
                        if not _is_valid_sku_price(sku_price, min_act_price):
                            continue
                        uid_str = str(sku_id) if sku_id else f"{product_id}_fb_{fallback_idx}"
                        sku_entries.append((uid_str, base_name, sku_price, sku_fmt))

        for sku_uid, display_name, act_price, sku_fmt in sku_entries:
            orig_price = base_orig_price
            is_suspicious = 1 if is_confirmed_sku else 0

            if orig_price is not None and act_price > 0:
                try:
                    orig_p = float(orig_price)
                    act_p = float(act_price)
                    if act_p > orig_p:
                        orig_p *= 10
                    if (orig_p / act_p) > 3 and not is_confirmed_sku:
                        is_suspicious = -1
                    orig_price = round(orig_p, 2)
                except (ValueError, TypeError):
                    pass

            # 拷贝单规格 format_info，防止后续单位换算直接修改原引用或报 KeyError
            pricing_basis_copy = dict(sku_fmt.get("pricing_basis") or {})
            format_info_copy = {
                "pricing_basis": pricing_basis_copy,
                "decision_keywords": list(sku_fmt.get("decision_keywords") or []),
            }

            filtered_products.append({
                "uid": sku_uid,
                "name": display_name,
                "product_id": product_id,
                "platform": product.get("platform"),
                "image_url": product.get("image_url"),
                "product_url": product_url,
                "original_price": orig_price,
                "saved_price": round(float(product.get("saved_price") or 0), 2),
                "brand": product.get("brand"),
                "format_info": format_info_copy,
                "activity_price": round(float(act_price), 2),
                "is_suspicious": is_suspicious,
                "_source_api": _source_api
            })

    logger.info(f"搜索词：[{keyword}]，分词项：{search_terms}，过滤展开后的商品规格数量: {len(filtered_products)}")

    # 遍历当前搜索结果，统计各个类别下不同单位的出现频率
    category_unit_counts = {}
    for product in filtered_products:
        base_unit = product.get("format_info", {}).get("pricing_basis", {}).get("base_unit", "件")
        category = UNIT_TO_CATEGORY.get(base_unit)
        if category:
            if category not in category_unit_counts:
                category_unit_counts[category] = {}
            category_unit_counts[category][base_unit] = category_unit_counts[category].get(base_unit, 0) + 1

    # 找到每个类别下，商品数量最多的单位作为统一后的目标单位
    category_target_unit = {}
    for category, counts in category_unit_counts.items():
        target_unit = max(counts.items(), key=lambda x: x[1])[0]
        category_target_unit[category] = target_unit

    grouped_products = {}
    for product in filtered_products:
        pricing_basis = product["format_info"]["pricing_basis"]
        total_value = pricing_basis.get("total_value", 1)
        base_unit = pricing_basis.get("base_unit", "件")

        # 检查是否可以进行单位转换
        category = UNIT_TO_CATEGORY.get(base_unit)
        if category and category in category_target_unit:
            target_unit = category_target_unit[category]
            if base_unit != target_unit:
                orig_multiplier = UNIT_TO_MULTIPLIER[base_unit]
                target_multiplier = UNIT_TO_MULTIPLIER[target_unit]
                total_value = total_value * (orig_multiplier / target_multiplier)
                base_unit = target_unit
                pricing_basis["total_value"] = round(total_value, 4)
        else:
            if isinstance(total_value, float):
                total_value = round(total_value, 4)
                pricing_basis["total_value"] = total_value

        # 性价比计算
        price = product["activity_price"]
        product["cost_performance_score"] = total_value / price if price and price > 0 else 0
        product["base_unit"] = base_unit

        if base_unit not in grouped_products:
            grouped_products[base_unit] = []
        grouped_products[base_unit].append(product)

    # 按照 base_unit 分组收集
    final_results = []
    for base_unit, unit_products in grouped_products.items():
        unit_products.sort(key=lambda x: x["cost_performance_score"], reverse=True)
        final_results.extend(unit_products)

    # 整体按性价比分值降序排列
    final_results.sort(key=lambda x: x["cost_performance_score"], reverse=True)

    # 精简响应结构（仅传递前端计算和展示必需字段）
    response_products = []
    for idx, product in enumerate(final_results):
        format_info = product.get("format_info") or {}
        pricing_basis = format_info.get("pricing_basis") or {}

        # 清洗并去重 decision_keywords，避免传输空值或重复项
        cleaned_keywords = []
        seen_attrs = set()
        for kw in (format_info.get("decision_keywords") or []):
            a_name = kw.get("attribute_name")
            a_val = kw.get("attribute_value")
            if a_name and a_val and a_name not in seen_attrs:
                seen_attrs.add(a_name)
                cleaned_keywords.append({
                    "attribute_name": a_name,
                    "attribute_value": a_val
                })

        cleaned_structure = [
            {"value": item.get("value"), "unit": item.get("unit")}
            for item in (pricing_basis.get("structure") or [])
            if item.get("value") is not None or item.get("unit")
        ]

        response_products.append({
            "uid": f"{product['uid']}_{idx}",
            "product_id": product["product_id"],
            "name": product["name"],
            "platform": product["platform"],
            "image_url": product["image_url"],
            "product_url": product["product_url"],
            "original_price": product["original_price"],
            "saved_price": product["saved_price"],
            "brand": product["brand"] or "",
            "activity_price": product["activity_price"],
            "_source_api": product["_source_api"],
            "is_suspicious": product["is_suspicious"],
            "base_unit": product["base_unit"],
            "format_info": {
                "pricing_basis": {
                    "total_value": pricing_basis.get("total_value", 1),
                    "structure": cleaned_structure
                },
                "decision_keywords": cleaned_keywords
            }
        })

    # 统计最终所有商品归一化后的规格单位数量，并选出数量最多的规格单位
    unit_counts = {}
    for p in response_products:
        u = p.get("base_unit") or "件"
        unit_counts[u] = unit_counts.get(u, 0) + 1

    dominant_unit = ""
    if unit_counts:
        dominant_unit = max(unit_counts.items(), key=lambda x: x[1])[0]

    return {
        "results": response_products,
        "dominant_unit": dominant_unit,
        "unit_counts": unit_counts
    }

# ============== 商品监控本地 JSON 安全维护与统计逻辑 ==============
MONITOR_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitored_products.json")
monitor_file_lock = threading.Lock()

# 监控商品展示数据 1 分钟缓存配置与状态
MONITOR_CACHE_TTL_SECONDS = 60
monitor_cache_lock = threading.Lock()
_monitor_counts_cache = {}


class MonitorRequest(BaseModel):
    keyword: str
    max_price: float = 0


def _normalize_max_price(val):
    """标准化最高限价字段：默认 0 表示不限制，正数保留最多两位小数"""
    try:
        if val is None or val == "":
            return 0
        num = float(val)
        if not math.isfinite(num) or num <= 0:
            return 0
        rounded = round(num, 2)
        return int(rounded) if rounded.is_integer() else rounded
    except (ValueError, TypeError):
        return 0


def _load_monitor_data_unsafe() -> dict:
    """内部读取监控 JSON 文件（调用方需持有锁）"""
    if not os.path.exists(MONITOR_FILE_PATH):
        return {}
    try:
        with open(MONITOR_FILE_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            data = json.loads(content)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"读取监控文件失败: {e}")
        return {}


def _save_monitor_data_unsafe(data: dict):
    """内部原子写入监控 JSON 文件（调用方需持有锁，使用临时文件替换保障安全）"""
    temp_path = f"{MONITOR_FILE_PATH}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, MONITOR_FILE_PATH)
    except Exception as e:
        logger.error(f"写入监控文件失败: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise e


def _has_valid_price_under_limit(product: dict, max_price: float) -> bool:
    """
    检查商品是否存在合法的规格价格，且（在设置了 max_price > 0 时）价格不超过 max_price
    """
    raw_format_info = product.get("format_info")
    if not isinstance(raw_format_info, dict) or not raw_format_info:
        return False

    raw_act_price = product.get("activity_price")
    min_act_price = None
    if isinstance(raw_act_price, (int, float)) and math.isfinite(raw_act_price) and raw_act_price > 0:
        min_act_price = float(raw_act_price)

    def _check_price(p_val):
        if type(p_val) not in (int, float) or not math.isfinite(p_val) or p_val <= 0:
            return False
        if min_act_price is not None and round(float(p_val), 2) < round(float(min_act_price), 2):
            return False
        if max_price > 0 and round(float(p_val), 2) > round(float(max_price), 2):
            return False
        return True

    # 单规格结构
    if "pricing_basis" in raw_format_info and isinstance(raw_format_info.get("pricing_basis"), dict):
        return _check_price(raw_format_info.get("price"))

    # 多规格结构：只要该商品下有任意一个合法规格满足限价，该商品即有效
    for sku_fmt in raw_format_info.values():
        if isinstance(sku_fmt, dict) and isinstance(sku_fmt.get("pricing_basis"), dict):
            if _check_price(sku_fmt.get("price")):
                return True

    return False


def count_monitored_products_72h(keyword_price_map: dict, min_match_score: int = 10, hours: int = 72) -> dict:
    """
    keyword_price_map: { "奶锅": 10, "抽纸": 0, ... }
    """
    counts = {kw: {"total": 0, "confirmed": 0} for kw in keyword_price_map}
    if not keyword_price_map:
        return counts

    # 读取报错黑名单，保持与搜索接口一致
    with feedback_file_lock:
        feedback_data = _load_feedback_data_unsafe()

    raw_products = product_manager.query(
        {"format_status": "success", "updated_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=hours)}},
        sort=[("updated_at", -1)], limit=0,
    )

    valid_products_info = []
    for product in raw_products:
        product_url = product.get("promotion_url") or product.get("product_url")
        if not product_url:
            continue

        product_id = str(product.get("product_id") or product_url)
        if product_id in feedback_data:
            continue

        product_name = (product.get("name") or "").lower()
        if not product_name:
            continue

        sku_info = product.get("sku_info") if isinstance(product.get("sku_info"), list) else []
        need_reformat = product.get("need_reformat")
        need_update_sku = product.get("need_update_sku")
        sku_status = product.get("sku_status")

        is_confirmed = bool(
            sku_info
            and (need_update_sku is False or sku_status == "success")
            and (need_reformat is False or need_reformat == "false")
        )

        valid_products_info.append((product_name, is_confirmed, product))

    for kw, max_price in keyword_price_map.items():
        target_lower = (kw or "").strip().lower()
        if not target_lower:
            continue

        limit_p = _normalize_max_price(max_price)
        words = gen_goods_segment_info(name=kw)
        search_terms = []
        if isinstance(words, (list, tuple, set)):
            search_terms = [str(w).strip().lower() for w in words if str(w).strip()]

        if not search_terms and target_lower:
            search_terms = [target_lower]

        matched_total = 0
        matched_confirmed = 0
        for product_name, is_confirmed, product_doc in valid_products_info:
            if all(term in product_name for term in search_terms):
                # 增加价格与合法性校验
                if not _has_valid_price_under_limit(product_doc, limit_p):
                    continue
                matched_total += 1
                if is_confirmed:
                    matched_confirmed += 1

        counts[kw] = {
            "total": matched_total,
            "confirmed": matched_confirmed
        }

    return counts

def build_monitor_response(data: dict) -> dict:
    """组装监控响应数据（包含 72 小时数据库商品总数量与已人工确定数量，绑定 max_price 缓存校验）"""
    active_kw_price_map = {
        kw: _normalize_max_price(info.get("max_price", 0))
        for kw, info in data.items()
        if info.get("is_monitoring", False)
    }
    now_ts = datetime.now().timestamp()

    counts_map = {}
    keywords_to_query = {}
    has_expired = False

    with monitor_cache_lock:
        for kw, max_p in active_kw_price_map.items():
            cached_entry = _monitor_counts_cache.get(kw)
            # 不仅校验时间未过期，还要校验 max_price 是否发生变更
            if (
                cached_entry is not None
                and (now_ts - cached_entry.get("updated_at", 0)) < MONITOR_CACHE_TTL_SECONDS
                and cached_entry.get("max_price", 0) == max_p
            ):
                counts_map[kw] = {
                    "total": cached_entry["total"],
                    "confirmed": cached_entry["confirmed"]
                }
            else:
                if cached_entry is not None:
                    has_expired = True
                keywords_to_query[kw] = max_p

    if keywords_to_query:
        target_query_map = active_kw_price_map if has_expired else keywords_to_query
        fresh_counts = count_monitored_products_72h(target_query_map, min_match_score=10, hours=72)
        update_ts = datetime.now().timestamp()
        with monitor_cache_lock:
            for kw, val in fresh_counts.items():
                _monitor_counts_cache[kw] = {
                    "total": val["total"],
                    "confirmed": val["confirmed"],
                    "max_price": target_query_map.get(kw, 0),
                    "updated_at": update_ts
                }
                counts_map[kw] = val

    active_list = []
    inactive_list = []
    for kw, info in data.items():
        is_mon = bool(info.get("is_monitoring", False))
        kw_counts = counts_map.get(kw, {"total": 0, "confirmed": 0}) if is_mon else {"total": 0, "confirmed": 0}
        item = {
            "keyword": kw,
            "is_monitoring": is_mon,
            "max_price": _normalize_max_price(info.get("max_price", 0)),
            "added_at": info.get("added_at"),
            "cancelled_at": info.get("cancelled_at"),
            "count_72h": kw_counts["total"],
            "confirmed_count_72h": kw_counts["confirmed"]
        }
        if item["is_monitoring"]:
            active_list.append(item)
        else:
            inactive_list.append(item)

    active_list.sort(key=lambda x: x.get("added_at") or "", reverse=True)
    inactive_list.sort(key=lambda x: x.get("cancelled_at") or "", reverse=True)

    return {
        "monitored": active_list,
        "history": inactive_list
    }


@app.get("/api/monitor")
def api_get_monitors():
    with monitor_file_lock:
        data = _load_monitor_data_unsafe()
    result = build_monitor_response(data)
    return {
        "status": "success",
        "data": result["monitored"],
        "history": result["history"]
    }


@app.post("/api/monitor")
def api_add_monitor(req: MonitorRequest):
    kw = (req.keyword or "").strip()
    if not kw:
        raise HTTPException(status_code=400, detail="商品名称不能为空")

    max_price = _normalize_max_price(req.max_price)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with monitor_file_lock:
        data = _load_monitor_data_unsafe()
        data[kw] = {
            "is_monitoring": True,
            "max_price": max_price,
            "added_at": now_str,
            "cancelled_at": None
        }
        _save_monitor_data_unsafe(data)

    logger.info(f"新增监控商品: [{kw}], max_price=[{max_price}]")
    result = build_monitor_response(data)
    return {
        "status": "success",
        "data": result["monitored"],
        "history": result["history"]
    }


@app.delete("/api/monitor")
def api_delete_monitor(keyword: str):
    kw = (keyword or "").strip()
    if not kw:
        raise HTTPException(status_code=400, detail="商品名称不能为空")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with monitor_file_lock:
        data = _load_monitor_data_unsafe()
        if kw in data:
            data[kw]["is_monitoring"] = False
            data[kw]["max_price"] = _normalize_max_price(data[kw].get("max_price", 0))
            data[kw]["cancelled_at"] = now_str
        else:
            data[kw] = {
                "is_monitoring": False,
                "max_price": 0,
                "added_at": None,
                "cancelled_at": now_str
            }
        _save_monitor_data_unsafe(data)

    logger.info(f"取消监控商品: [{kw}]")
    result = build_monitor_response(data)
    return {
        "status": "success",
        "data": result["monitored"],
        "history": result["history"]
    }
# =====================================================================


# 1. 搜索 API 接口
@app.get("/api/search")
def api_search(keyword: str = "方便面", hours: int = 72):
    logger.info(f"收到用户搜索请求，搜索词：[{keyword}]，时间范围：{hours}小时")

    search_data = search_product(keyword, min_match_score=10, hours=hours)
    return {
        "status": "success",
        "data": search_data["results"],
        "dominant_unit": search_data.get("dominant_unit", ""),
        "unit_counts": search_data.get("unit_counts", {})
    }

# 2. 网页路由配置
@app.get("/", response_class=HTMLResponse)
def read_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/monitor", response_class=HTMLResponse)
def read_monitor_page():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)