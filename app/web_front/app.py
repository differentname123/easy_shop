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
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# 假设你的 common 模块在这个路径下可用
from common.common_utils import setup_logger
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager

logger = setup_logger(app_name="goods_search")

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


def search_product(keyword: str, min_match_score=10, hours=24, limit=0):
    # 构建搜索实体
    target_entity_info_list = [{"name": keyword, "score": 10}]

    # 从数据库获取近期数据
    raw_products = product_manager.query(
        {"format_status": "success", "updated_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=hours)}},
        sort=[("updated_at", -1)], limit=limit,
    )

    # 提取前端展示和计算所需的字段 (增加了更多字段用于增强展示)
    products = [{
        "name": product.get("name"),
        "product_id": product.get("product_id"),
        "platform": product.get("platform"),
        "image_url": product.get("image_url"),
        # 修改：优先使用 promotion_url，没有才使用 product_url
        "product_url": product.get("promotion_url") or product.get("product_url"),
        # 新增：判断是否有佣金 (存在 promotion_url 即为有佣金)
        "has_commission": bool(product.get("promotion_url")),
        "original_price": product.get("original_price"),
        "saved_price": product.get("saved_price"),
        "sales_tip": product.get("sales_tip"),
        "brand": product.get("brand"),
        "category": product.get("category"),
        "format_info": product.get("format_info"),
        "activity_price": product.get("activity_price", 999999),
        "updated_at": product.get("updated_at"),
        # 新增：来源字段，如果不存在则默认赋值为 "group"
        "_source_api": product.get("_source_api") or "group"
    } for product in raw_products]

    # 计算匹配分数
    for simple_product in products:
        core_entities = simple_product.get("format_info", {}).get("core_entities", [])
        match_score = 0
        for target_entity in target_entity_info_list:
            for core_entity in core_entities:
                core_name = core_entity.get("name", "")
                target_name = target_entity.get("name", "")
                if core_name and target_name and (
                        core_name.lower() in target_name.lower() or target_name.lower() in core_name.lower()):
                    match_score += target_entity.get("score", 0) + core_entity.get("score", 0)

        simple_product["match_score"] = match_score

    # 过滤并排序
    filtered_products = [p for p in products if p["match_score"] >= min_match_score]
    filtered_products.sort(key=lambda x: x["match_score"], reverse=True)

    logger.info(f"过滤后的商品数量: {len(filtered_products)}")

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
        # 按频次最高选取单位
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
                # 执行单位转换计算：当前值 * (原单位倍率 / 目标单位倍率)
                orig_multiplier = unit_to_multiplier[base_unit]
                target_multiplier = unit_to_multiplier[target_unit]
                total_value = total_value * (orig_multiplier / target_multiplier)
                base_unit = target_unit
                product["format_info"]["pricing_basis"]["total_value"] = total_value

        # 性价比计算：单位价格买到的量，例如 "克/元"
        price = product["activity_price"]
        product["cost_performance_score"] = total_value / price if price > 0 else 0
        product["base_unit"] = base_unit

        if base_unit not in grouped_products:
            grouped_products[base_unit] = []
        grouped_products[base_unit].append(product)

    # 按照 base_unit 分组，收集所有的商品以支持前端的多维度筛选
    final_results = []
    for base_unit, unit_products in grouped_products.items():
        unit_products.sort(key=lambda x: x["cost_performance_score"], reverse=True)
        final_results.extend(unit_products)  # 去掉了原有的 [:3] 限制

    # 整体再按性价比分值排个序返回
    final_results.sort(key=lambda x: x["cost_performance_score"], reverse=True)

    # ========== 核心修改：动态统计并生成过滤维度 ==========
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

    # 1. 过滤掉仅有1个值的属性（没有筛选意义）
    valid_attributes = []
    for attr_name, stats in attribute_stats.items():
        if len(stats["values"]) > 1:
            valid_attributes.append({
                "attribute_name": attr_name,
                "score": stats["score"],
                "options": list(stats["values"])
            })

    # 2. 根据得分降序排列，取前5个最能影响决策的关键词进行筛选
    valid_attributes.sort(key=lambda x: x["score"], reverse=True)
    top_filters = valid_attributes[:5]
    # ===================================================

    return {
        "results": final_results,
        "filters": top_filters
    }


# 1. 搜索 API 接口
# 修改 1. 搜索 API 接口
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


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)