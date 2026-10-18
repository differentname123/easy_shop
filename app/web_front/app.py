# -- coding: utf-8 --
""":authors:
    zhuxiaohu
:create_date:
    2026/10/3 13:56
:last_date:
    2026/10/3 13:56
:description:

"""
import os
import json
import threading
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
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

# ============== 新增：商品错误反馈本地 JSON 安全维护逻辑 ==============
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

    # 读取已反馈有误的商品列表，用于后续过滤
    with feedback_file_lock:
        feedback_data = _load_feedback_data_unsafe()

    # 从数据库获取近期数据
    raw_products = product_manager.query(
        {"format_status": "success", "updated_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=hours)}},
        sort=[("updated_at", -1)], limit=limit,
    )

    # 提取前端展示和计算所需的字段 (过滤 product_url 为空的数据及已反馈商品)
    products = []
    for product in raw_products:
        product_url = product.get("promotion_url") or product.get("product_url")

        if not product_url:
            continue

        product_id = str(product.get("product_id") or product_url)
        if product_id in feedback_data:
            continue

        original_price = product.get("original_price")
        activity_price = product.get("activity_price", 999999)
        is_suspicious = 0

        # 计算比值并标记可疑商品
        if original_price is not None and activity_price != 999999 and activity_price > 0:
            try:
                orig_p = float(original_price)
                act_p = float(activity_price)

                if act_p > orig_p:
                    orig_p *= 10

                ratio = orig_p / act_p
                if ratio > 3:
                    is_suspicious = -1

                original_price = orig_p
            except (ValueError, TypeError):
                pass

        sku_info = product.get("sku_info")
        need_reformat = product.get("need_reformat")
        if sku_info and (need_reformat is False or need_reformat == "false"):
            is_suspicious = 1

        _source_api = product.get("_source_api", "group")
        if "group" in _source_api:
            _source_api = "group"
        else:
            _source_api = "no_group"

        products.append({
            "name": product.get("name"),
            "product_id": product_id,
            "platform": product.get("platform"),
            "image_url": product.get("image_url"),
            "product_url": product_url,
            "has_commission": bool(product.get("promotion_url")),
            "original_price": original_price,
            "saved_price": product.get("saved_price") or 0,
            "brand": product.get("brand"),
            "format_info": product.get("format_info"),
            "activity_price": activity_price,
            "is_suspicious": is_suspicious,
            "_source_api": _source_api
        })

    # 2. 搜索过滤：商品名称必须包含分词列表中的每一个元素 (AND 关系)
    if search_terms:
        filtered_products = [
            p for p in products
            if (p_name := (p.get("name") or "").lower()) and all(term in p_name for term in search_terms)
        ]
    else:
        filtered_products = []

    logger.info(f"搜索词：[{keyword}]，分词项：{search_terms}，过滤后的商品数量: {len(filtered_products)}")

    # 构建单位到类别、以及单位到基数乘数的反向查找字典
    unit_to_category = {}
    unit_to_multiplier = {}
    for category, units in sku_unit_base_mapping.items():
        for unit, multiplier in units.items():
            unit_to_category[unit] = category
            unit_to_multiplier[unit] = multiplier

    # 遍历当前搜索结果，统计各个类别下不同单位的出现频率
    category_unit_counts = {}
    for product in filtered_products:
        base_unit = product.get("format_info", {}).get("pricing_basis", {}).get("base_unit", "件")
        category = unit_to_category.get(base_unit)
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
        total_value = product.get("format_info", {}).get("pricing_basis", {}).get("total_value", 1)
        base_unit = product.get("format_info", {}).get("pricing_basis", {}).get("base_unit", "件")

        # 检查是否可以进行单位转换
        category = unit_to_category.get(base_unit)
        if category and category in category_target_unit:
            target_unit = category_target_unit[category]
            if base_unit != target_unit:
                orig_multiplier = unit_to_multiplier[base_unit]
                target_multiplier = unit_to_multiplier[target_unit]
                total_value = total_value * (orig_multiplier / target_multiplier)
                base_unit = target_unit
                product["format_info"]["pricing_basis"]["total_value"] = total_value

        # 性价比计算
        price = product["activity_price"]
        product["cost_performance_score"] = total_value / price if price > 0 else 0
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

    # 动态统计并生成过滤维度
    attribute_stats = {}
    for product in final_results:
        keywords = product.get("format_info", {}).get("decision_keywords", [])
        for kw in keywords:
            attr_name = kw.get("attribute_name")
            attr_val = kw.get("attribute_value")
            score = kw.get("score", 0)

            if not attr_name or not attr_val:
                continue

            if attr_name not in attribute_stats:
                attribute_stats[attr_name] = {"score": 0, "values": set()}

            attribute_stats[attr_name]["score"] += score
            attribute_stats[attr_name]["values"].add(attr_val)

    # 过滤掉仅有1个值的属性
    valid_attributes = []
    for attr_name, stats in attribute_stats.items():
        if len(stats["values"]) > 1:
            valid_attributes.append({
                "attribute_name": attr_name,
                "score": stats["score"],
                "options": list(stats["values"])
            })

    # 根据得分降序排列，取前5个筛选维度
    valid_attributes.sort(key=lambda x: x["score"], reverse=True)
    top_filters = valid_attributes[:5]

    # 精简响应结构
    response_products = []
    for product in final_results:
        format_info = product.get("format_info") or {}
        pricing_basis = format_info.get("pricing_basis") or {}
        response_products.append({
            "product_id": product["product_id"],
            "name": product["name"],
            "platform": product["platform"],
            "image_url": product["image_url"],
            "product_url": product["product_url"],
            "has_commission": product["has_commission"],
            "original_price": product["original_price"],
            "saved_price": product["saved_price"],
            "brand": product["brand"],
            "activity_price": product["activity_price"],
            "_source_api": product["_source_api"],
            "is_suspicious": product["is_suspicious"],
            "base_unit": product["base_unit"],
            "format_info": {
                "pricing_basis": {
                    "total_value": pricing_basis.get("total_value", 1),
                    "structure": [
                        {"value": item.get("value"), "unit": item.get("unit")}
                        for item in (pricing_basis.get("structure") or [])
                    ]
                },
                "decision_keywords": [
                    {
                        "attribute_name": kw.get("attribute_name"),
                        "attribute_value": kw.get("attribute_value")
                    }
                    for kw in (format_info.get("decision_keywords") or [])
                ]
            }
        })

    return {
        "results": response_products,
        "filters": [
            {"attribute_name": item["attribute_name"], "options": item["options"]}
            for item in top_filters
        ]
    }

# ============== 新增：商品监控本地 JSON 安全维护与统计逻辑 ==============
MONITOR_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitored_products.json")
monitor_file_lock = threading.Lock()


class MonitorRequest(BaseModel):
    keyword: str


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


def count_monitored_products_72h(keywords: list, min_match_score: int = 10, hours: int = 72) -> dict:
    """一次性查询 72 小时内更新的有效商品，通过商品名直接匹配，统计总数量及已人工确定的数量"""
    counts = {kw: {"total": 0, "confirmed": 0} for kw in keywords}
    if not keywords:
        return counts

    raw_products = product_manager.query(
        {"format_status": "success", "updated_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=hours)}},
        sort=[("updated_at", -1)], limit=0,
    )

    valid_products_info = []
    for product in raw_products:
        product_url = product.get("promotion_url") or product.get("product_url")
        if not product_url:
            continue
        product_name = (product.get("name") or "").lower()
        if not product_name:
            continue

        sku_info = product.get("sku_info")
        need_reformat = product.get("need_reformat")
        is_confirmed = bool(sku_info and (need_reformat is False or need_reformat == "false"))

        valid_products_info.append((product_name, is_confirmed))

    for kw in keywords:
        target_lower = (kw or "").strip().lower()
        if not target_lower:
            continue

        # 1. 调用分词工具获取分词词列表
        words = gen_goods_segment_info(name=kw)

        # 对分词结果进行清洗与大小写统一，过滤空字符
        search_terms = []
        if isinstance(words, (list, tuple, set)):
            search_terms = [str(w).strip().lower() for w in words if str(w).strip()]

        # 兜底保障：若分词结果为空但原始关键字非空，则降级使用原始关键字
        if not search_terms and target_lower:
            search_terms = [target_lower]

        matched_total = 0
        matched_confirmed = 0
        for product_name, is_confirmed in valid_products_info:
            if all(term in product_name for term in search_terms):
                matched_total += 1
                if is_confirmed:
                    matched_confirmed += 1
        counts[kw] = {
            "total": matched_total,
            "confirmed": matched_confirmed
        }

    return counts


def build_monitor_response(data: dict) -> dict:
    """组装监控响应数据（包含 72 小时数据库商品总数量与已人工确定数量）"""
    active_keywords = [kw for kw, info in data.items() if info.get("is_monitoring", False)]
    counts_map = count_monitored_products_72h(active_keywords, min_match_score=10, hours=72)

    active_list = []
    inactive_list = []
    for kw, info in data.items():
        is_mon = bool(info.get("is_monitoring", False))
        kw_counts = counts_map.get(kw, {"total": 0, "confirmed": 0}) if is_mon else {"total": 0, "confirmed": 0}
        item = {
            "keyword": kw,
            "is_monitoring": is_mon,
            "added_at": info.get("added_at"),
            "cancelled_at": info.get("cancelled_at"),
            "count_72h": kw_counts["total"],
            "confirmed_count_72h": kw_counts["confirmed"]
        }
        if item["is_monitoring"]:
            active_list.append(item)
        else:
            inactive_list.append(item)

    # 按时间倒序排列
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
    raw_input = (req.keyword or "").strip()
    if not raw_input:
        raise HTTPException(status_code=400, detail="商品名称不能为空")

    # 支持单个商品或用逗号、顿号、空格、换行分隔的批量商品上传
    import re
    keywords = [k.strip() for k in re.split(r"[\s,，、;；\n]+", raw_input) if k.strip()]
    if not keywords:
        raise HTTPException(status_code=400, detail="未解析到有效的商品名称")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with monitor_file_lock:
        data = _load_monitor_data_unsafe()
        for kw in keywords:
            data[kw] = {
                "is_monitoring": True,
                "added_at": now_str,
                "cancelled_at": None
            }
        _save_monitor_data_unsafe(data)

    logger.info(f"新增监控商品: {keywords}")
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
            data[kw]["cancelled_at"] = now_str
        else:
            data[kw] = {
                "is_monitoring": False,
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
# 修改搜索 API 接口
@app.get("/api/search")
def api_search(keyword: str = "方便面", hours: int = 72):
    # 【新增这一行】：此时 keyword 已经是解码后的中文 "可乐"
    logger.info(f"收到用户搜索请求，搜索词：[{keyword}]，时间范围：{hours}小时")

    search_data = search_product(keyword, min_match_score=10, hours=hours)
    return {
        "status": "success",
        "data": search_data["results"],
        "filters": search_data["filters"]
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