"""
提供查询找到最具性价比的功能

"""
from datetime import datetime, timezone, timedelta

from common.common_utils import setup_logger

logger = setup_logger(app_name="goods_search")

from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager

db_instance = gen_db_object()
db_instance.ping()
product_manager = ProductManager(db_instance)


def search_product(target_entity_info_list, min_match_score=10, hours=24, limit=0):
    products = products = product_manager.query(
        {"format_status": "success", "updated_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=hours)}},
         sort=[("updated_at", -1)], limit=limit,
    )
    products = [{
        "name": product.get("name"),
        "product_id": product.get("product_id"),

        "format_info": product.get("format_info"),
        "activity_price": product.get("activity_price")
    } for product in products]

    for simple_product in products:
        core_entities = simple_product.get("format_info", {}).get("core_entities", [])
        # 计算和目标实体的匹配分数，为 simple_product 添加一个新的字段 match_score
        match_score = 0
        for target_entity in target_entity_info_list:
            for core_entity in core_entities:
                core_name = core_entity.get("name")
                target_name = target_entity.get("name")
                if core_name and target_name and (
                        core_name.lower() in target_name.lower() or target_name.lower() in core_name.lower()):

                    match_score = match_score + target_entity.get("score", 0) + core_entity.get("score", 0)

        simple_product["match_score"] = match_score

    # 过滤出匹配分数大于等于 min_match_score 的商品并且按照 match_score 降序排序
    filtered_products = [product for product in products if product["match_score"] >= min_match_score]
    sorted(filtered_products, key=lambda x: x["match_score"], reverse=True)

    # 输出通过过滤的商品统计
    logger.info(f"过滤后的商品数量: {len(filtered_products)}")

    grouped_products = {}

    for product in filtered_products:
        total_value = product.get("format_info", {}).get("pricing_basis", {}).get("total_value", 1)
        base_unit = product.get("format_info", {}).get("pricing_basis", {}).get("base_unit", "")
        # 计算性价比 score = match_score / total_value
        product["cost_performance_score"] = total_value / product["activity_price"]
        product["base_unit"] = base_unit
        if base_unit not in grouped_products:
            grouped_products[base_unit] = []
        grouped_products[base_unit].append(product)
    # 按照 base_unit 进行 分组，然后输出每组中性价比最高的商品

    filtered_products = []
    for base_unit, products in grouped_products.items():
        best_product = max(products, key=lambda x: x["cost_performance_score"])
        filtered_products.append(best_product)
    for product in filtered_products:
        print(
            f"商品名称: {product['name']}, 价格 {product["activity_price"]}, 性价比分数: {product['cost_performance_score']}, 单位: {product['base_unit']}")

    return filtered_products


if __name__ == "__main__":
    target_entity_info_list = [
        {
            "name": "可乐",
            "score": 10
        }
    ]

    filtered_products = search_product(target_entity_info_list)
    print()
