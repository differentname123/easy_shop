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

def search_product(keyword: str, min_match_score=10, hours=24, limit=0):
    # 构建搜索实体
    target_entity_info_list = [{"name": keyword, "score": 10}]

    # 从数据库获取近期数据
    raw_products = product_manager.query(
        {"format_status": "success", "updated_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=hours)}},
        sort=[("updated_at", -1)], limit=limit,
    )

    # 提取前端展示和计算所需的字段 (新增了 image_url, product_url, platform)
    products = [{
        "name": product.get("name"),
        "product_id": product.get("product_id"),
        "platform": product.get("platform"),
        "image_url": product.get("image_url"),
        "product_url": product.get("product_url"),
        "format_info": product.get("format_info"),
        "activity_price": product.get("activity_price", 999999) # 避免除以0或空值
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

    # 过滤并排序 (修复了原代码 sorted 无赋值的 bug)
    filtered_products = [p for p in products if p["match_score"] >= min_match_score]
    filtered_products.sort(key=lambda x: x["match_score"], reverse=True)

    logger.info(f"过滤后的商品数量: {len(filtered_products)}")

    grouped_products = {}
    for product in filtered_products:
        total_value = product.get("format_info", {}).get("pricing_basis", {}).get("total_value", 1)
        base_unit = product.get("format_info", {}).get("pricing_basis", {}).get("base_unit", "件")

        # 性价比计算：单位价格买到的量，例如 "克/元"
        price = product["activity_price"]
        product["cost_performance_score"] = total_value / price if price > 0 else 0
        product["base_unit"] = base_unit

        if base_unit not in grouped_products:
            grouped_products[base_unit] = []
        grouped_products[base_unit].append(product)

    # 按照 base_unit 分组，收集每组性价比排名前 3 的商品，而不仅仅是 1 个，方便用户有更多选择
    final_results = []
    for base_unit, unit_products in grouped_products.items():
        # 按照性价比降序排序
        unit_products.sort(key=lambda x: x["cost_performance_score"], reverse=True)
        # 取前 3 名
        final_results.extend(unit_products[:3])

    # 整体再按性价比分值排个序返回
    final_results.sort(key=lambda x: x["cost_performance_score"], reverse=True)
    return final_results

# 1. 搜索 API 接口
@app.get("/api/search")
def api_search(keyword: str = "方便面", hours: int = 72):
    results = search_product(keyword, min_match_score=10, hours=hours)
    return {"status": "success", "data": results}

# 2. 网页路由配置
@app.get("/", response_class=HTMLResponse)
def read_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    # 启动本地服务，运行在 8000 端口
    uvicorn.run(app, host="127.0.0.1", port=8000)