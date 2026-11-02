# -*- coding: utf-8 -*-
"""商品仓储确定性回归测试：无外部 API、无真实 MongoDB。"""
import datetime
import unittest
from copy import deepcopy
from types import SimpleNamespace

from app.mobile_web.repositories.product_repo import ProductRepository


class MemoryCollection:
    """覆盖本仓储使用的 Mongo 更新子集，并实际检查旧复合唯一索引。"""

    def __init__(self):
        self.docs = []
        self.indexes = []
        self.queries = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    @staticmethod
    def matches(doc, query):
        return all(
            any(MemoryCollection.matches(doc, branch) for branch in value)
            if key == "$or" else doc.get(key) == value
            for key, value in query.items()
        )

    def find_one(self, query):
        self.queries.append(deepcopy(query))
        return next((deepcopy(doc) for doc in self.docs if self.matches(doc, query)), None)

    def update_one(self, query, update, upsert=False):
        if set(update.get("$set", {})) & set(update.get("$setOnInsert", {})):
            raise ValueError("MongoDB rejects conflicting update paths")
        index = next((i for i, doc in enumerate(self.docs) if self.matches(doc, query)), None)
        inserted = index is None
        if inserted and not upsert:
            return SimpleNamespace(upserted_count=0, modified_count=0)
        if inserted:
            doc = {key: value for key, value in query.items() if not key.startswith("$")}
            doc.update(deepcopy(update.get("$setOnInsert", {})))
        else:
            doc = deepcopy(self.docs[index])
        doc.update(deepcopy(update.get("$set", {})))
        identity = (doc.get("platform"), doc.get("product_id"))
        for i, other in enumerate(self.docs):
            if i != index and (other.get("platform"), other.get("product_id")) == identity:
                raise ValueError("duplicate unique(platform, product_id)")
        if inserted:
            self.docs.append(doc)
        else:
            self.docs[index] = doc
        return SimpleNamespace(upserted_count=int(inserted), modified_count=int(not inserted))

    def bulk_write(self, operations, ordered=False):
        inserted = modified = 0
        for operation in operations:
            result = self.update_one(operation._filter, operation._doc, upsert=operation._upsert)
            inserted += result.upserted_count
            modified += result.modified_count
        return SimpleNamespace(upserted_count=inserted, modified_count=modified)


class TestProductRepository(unittest.TestCase):
    def setUp(self):
        self.col = MemoryCollection()
        db = SimpleNamespace(get_collection=lambda name: self.col)
        self.repo = ProductRepository(db_instance=db)

    def test_known_zero_values_do_not_fall_back_to_positive_fields(self):
        doc = {
            "platform": "pdd", "product_id": "100", "activity_price": 0,
            "price": 99, "original_price": 0, "saved_price": 0,
            "coupon_amount": 0, "has_coupon": False, "commission_amount": 0,
        }
        for normalize in (self.repo._normalize_doc_summary, self.repo._normalize_doc):
            result = normalize(doc)
            for key in ("price", "original_price", "saved_price", "coupon_amount", "commission_amount", "commission_rate"):
                self.assertEqual(result[key], 0, key)
            self.assertFalse(result["has_coupon"])

    def test_zero_saved_price_and_false_coupon_are_not_inferred_from_discount(self):
        result = self.repo._normalize_doc_summary({
            "product_id": "1", "activity_price": 80, "original_price": 100,
            "saved_price": 0, "coupon_amount": 0, "has_coupon": False,
        })
        self.assertEqual(result["saved_price"], 0)
        self.assertEqual(result["coupon_amount"], 0)
        self.assertFalse(result["has_coupon"])

    def test_zero_or_missing_commission_does_not_create_a_payout(self):
        for commission in (0, None):
            self.assertEqual(self.repo._calc_commission(100, commission), (0.0, 0.0))
        self.assertEqual(self.repo._calc_commission(50, 5), (10.0, 5.0))
        self.assertEqual(self.repo._calc_commission(0, 5), (0.0, 5.0))

    def test_explicit_commission_rate_is_preserved(self):
        result = self.repo._normalize_doc_summary({
            "product_id": "1", "activity_price": 10, "commission_amount": 0,
            "commission_rate": 0,
        })
        self.assertEqual(result["commission_rate"], 0)

    def test_summary_and_detail_share_money_and_identity_mapping(self):
        payload = {
            "platform": "tb", "goods_id": "AB12", "goods_sign": "SIGN",
            "name": "真实商品", "price": 0, "min_group_price_yuan": 99,
            "original_price": 0, "coupon_amount": 0, "coupon_discount_yuan": 10,
            "has_coupon": False, "commission_amount": 0, "estimated_commission_yuan": 10,
            "brand_name": "真实品牌", "category_name": "食品", "sales_tip": 0,
        }
        summary_filter, summary = self.repo._build_product_update(payload, detail=False)
        detail_filter, detail = self.repo._build_product_update(payload, detail=True)
        self.assertEqual(summary_filter, detail_filter)
        for key in ("platform", "product_id", "goods_sign", "name", "activity_price", "price",
                    "original_price", "saved_price", "coupon_amount", "has_coupon",
                    "commission_amount", "brand", "category", "sales_tip"):
            self.assertEqual(summary["$set"][key], detail["$set"][key], key)
        self.assertEqual(summary["$set"]["activity_price"], 0)
        self.assertEqual(summary["$set"]["commission_amount"], 0)
        self.assertEqual(summary["$set"]["platform"], "taobao")
        self.assertEqual(summary["$set"]["source"], "taobao_recommend_stream")

    def test_detail_roundtrip_preserves_real_provider_fields(self):
        detail = {
            "platform": "pdd", "goods_id": "100", "goods_sign": "SIGN100",
            "name": "商品", "price": 20, "original_price": 30,
            "coupon_amount": 10, "has_coupon": True, "commission_amount": 0,
            "commission_rate": 0, "brand_name": "真实品牌", "category_name": "食品",
            "mall_name": "真实店铺", "desc": "来源描述", "product_url": "https://example.com/100",
            "image_url": "https://img.example.com/1", "carousel_images": ["https://img.example.com/1", "https://img.example.com/2"],
            "detail_tags": ["来源标签"], "unified_tags": ["平台标签"],
            "service_guarantees": ["仅来源明确承诺"], "shop_ratings": {"desc_score": "3.2"},
            "specs": [{"label": "净重", "value": "500g"}],
            "coupon_info": {"coupon_remain_quantity": 0, "coupon_total_quantity": 7, "valid_tip": "已过期"},
            "search_id": "SEARCH-1",
        }
        self.assertTrue(self.repo.upsert_product_detail(detail))
        result = self.repo.get_product_detail("SIGN100", platform="pdd", key_type="sign")
        for key in ("desc", "product_url", "carousel_images", "detail_tags", "unified_tags",
                    "service_guarantees", "shop_ratings", "specs", "coupon_info", "search_id", "mall_name"):
            self.assertEqual(result[key], detail[key], key)
        self.assertEqual(result["goods_id"], "100")
        self.assertEqual(result["product_id"], "100")
        self.assertEqual(result["brand_name"], "真实品牌")
        self.assertEqual(result["category_name"], "食品")
        self.assertIsInstance(result["detail_updated_at"], datetime.datetime)
        self.assertNotIn("promotion_url", result)
        self.assertNotIn("_id", result)
        result["coupon_info"]["coupon_remain_quantity"] = 999
        self.assertEqual(self.col.docs[0]["coupon_info"]["coupon_remain_quantity"], 0)

    def test_unknown_detail_fields_are_empty_not_fixed_commercial_claims(self):
        result = self.repo._normalize_doc({"product_id": "1", "activity_price": 100})
        for key in ("detail_tags", "service_guarantees", "specs"):
            self.assertEqual(result[key], [])
        for key in ("shop_ratings", "coupon_info"):
            self.assertEqual(result[key], {})
        self.assertEqual(result["desc"], "")
        self.assertIsNone(result["detail_updated_at"])
        self.assertEqual(result["commission_amount"], 0)

    def test_summary_does_not_overwrite_missing_curated_or_rich_fields(self):
        self.repo.upsert_product_detail({
            "goods_id": "100", "goods_sign": "SIGN100", "name": "商品", "price": 10,
            "brand": "真实品牌", "category": "食品", "mall_name": "真实店铺",
            "desc": "来源详情", "product_url": "https://example.com/100",
            "carousel_images": ["https://img.example.com/a"],
            "detail_tags": ["真实标签"], "coupon_info": {"coupon_remain_quantity": 0},
        })
        before = deepcopy(self.col.docs[0])
        self.repo.upsert_home_products([{
            "goods_id": "100", "name": "新标题", "price": 9,
            "brand_name": "", "category": None,
            "desc": "摘要不得写入", "coupon_info": {"coupon_remain_quantity": 8900},
        }], channel_type=5)
        stored = self.col.docs[0]
        for key in ("brand", "category", "mall_name", "desc", "product_url", "goods_sign",
                    "carousel_images", "detail_tags", "coupon_info", "detail_updated_at"):
            self.assertEqual(stored[key], before[key], key)
        self.assertEqual(stored["name"], "新标题")
        self.assertEqual(stored["activity_price"], 9)
        self.assertEqual(stored["channel_type"], 5)
        self.assertFalse(stored["need_reformat"])
        self.assertEqual(stored["format_status"], "success")
        self.assertEqual(stored["format_retry_count"], 999)

    def test_summary_can_update_explicit_brand_and_category(self):
        self.repo.upsert_home_products([{"goods_id": "100", "brand": "旧", "category": "旧类"}])
        self.repo.upsert_home_products([{"goods_id": "100", "brand_name": "新", "category_name": "新类"}])
        self.assertEqual(self.col.docs[0]["brand"], "新")
        self.assertEqual(self.col.docs[0]["category"], "新类")

    def test_partial_price_update_preserves_known_original_price(self):
        self.repo.upsert_home_products([{"goods_id": "100", "price": 80, "original_price": 100}])
        self.repo.upsert_home_products([{"goods_id": "100", "price": 0}])
        self.assertEqual(self.col.docs[0]["activity_price"], 0)
        self.assertEqual(self.col.docs[0]["price"], 100)
        self.assertEqual(self.col.docs[0]["original_price"], 100)

    def test_only_details_refresh_detail_updated_at(self):
        self.repo.upsert_home_products([{"goods_id": "100", "price": 10}])
        self.assertNotIn("detail_updated_at", self.col.docs[0])
        self.repo.upsert_product_detail({"goods_id": "100", "desc": "来源"})
        old = datetime.datetime(2020, 1, 1)
        self.col.docs[0]["detail_updated_at"] = old
        self.repo.upsert_home_products([{"goods_id": "100", "price": 9}])
        self.assertEqual(self.col.docs[0]["detail_updated_at"], old)
        self.assertGreater(self.col.docs[0]["updated_at"], old)
        self.assertEqual(self.repo.get_product_detail("100")["detail_updated_at"], old)
        self.repo.upsert_product_detail({"goods_id": "100", "desc": "新来源"})
        self.assertGreater(self.col.docs[0]["detail_updated_at"], old)

    def test_sign_only_products_do_not_collide_under_existing_unique_index(self):
        count = self.repo.upsert_home_products([
            {"goods_id": 0, "goods_sign": "SIGN_A", "name": "A", "price": 10},
            {"goods_id": "0", "goods_sign": "SIGN_B", "name": "B", "price": 20},
            {"goods_id": None, "goods_sign": "SIGN_C", "name": "C", "price": 30},
        ])
        self.assertEqual(count, 3)
        self.assertEqual(len(self.col.docs), 3)
        self.assertEqual(len({doc["product_id"] for doc in self.col.docs}), 3)
        self.assertIn(([('platform', 1), ('product_id', 1)], {'unique': True, 'background': True}), self.col.indexes)
        for sign in ("SIGN_A", "SIGN_B", "SIGN_C"):
            result = self.repo.get_product_detail(sign, key_type="sign")
            self.assertEqual(result["goods_id"], "")
            self.assertEqual(result["product_id"], "")
            self.assertEqual(result["goods_sign"], sign)

    def test_real_id_upgrades_sign_only_record_without_new_document(self):
        self.repo.upsert_home_products([{"goods_id": "0", "goods_sign": "SIGN_A", "name": "A"}])
        self.repo.upsert_product_detail({"goods_id": "123", "goods_sign": "SIGN_A", "price": 10})
        self.assertEqual(len(self.col.docs), 1)
        self.assertEqual(self.col.docs[0]["product_id"], "123")
        self.assertEqual(self.repo.get_product_detail("SIGN_A", key_type="sign")["goods_id"], "123")

    def test_sign_only_summary_never_replaces_existing_real_id(self):
        self.repo.upsert_product_detail({"goods_id": "123", "goods_sign": "SIGN_A", "price": 10})
        self.repo.upsert_home_products([{"goods_id": 0, "goods_sign": "SIGN_A", "price": 9}])
        self.assertEqual(len(self.col.docs), 1)
        self.assertEqual(self.col.docs[0]["product_id"], "123")

    def test_detail_lookup_is_always_platform_scoped(self):
        for platform in ("pdd", "jd", "taobao"):
            self.repo.upsert_product_detail({"platform": platform, "goods_id": "123", "goods_sign": "SAME_SIGN", "name": platform})
        self.assertEqual(self.repo.get_product_detail("123")["name"], "pdd")
        self.assertEqual(self.repo.get_product_detail("123", platform="jd", key_type="id")["name"], "jd")
        self.assertEqual(self.repo.get_product_detail("SAME_SIGN", platform="tb", key_type="sign")["name"], "taobao")
        self.assertEqual(self.col.queries[-1], {"platform": "taobao", "goods_sign": "SAME_SIGN"})
        self.repo.get_product_detail("123", platform="jd")
        self.assertEqual(self.col.queries[-1], {"platform": "jd", "$or": [{"product_id": "123"}, {"goods_sign": "123"}]})

    def test_explicit_key_type_prevents_id_and_sign_ambiguity(self):
        self.repo.upsert_product_detail({"goods_id": "A", "goods_sign": "B", "name": "by-sign"})
        self.repo.upsert_product_detail({"goods_id": "B", "goods_sign": "C", "name": "by-id"})
        self.assertEqual(self.repo.get_product_detail("B", key_type="sign")["name"], "by-sign")
        self.assertEqual(self.repo.get_product_detail("B", key_type="id")["name"], "by-id")

    def test_invalid_identity_does_not_write_or_query_zero_as_id(self):
        self.assertEqual(self.repo.upsert_home_products([{"goods_id": 0}, {"goods_id": "0"}, {}, None]), 0)
        self.assertFalse(self.repo.upsert_product_detail({"goods_id": "0"}))
        self.assertIsNone(self.repo.get_product_detail("0", key_type="id"))
        self.assertIsNone(self.repo.get_product_detail("sign:internal", key_type="id"))
        self.assertEqual(self.col.docs, [])
        self.assertEqual(self.col.queries, [])

    def test_sign_only_promotion_cache_uses_same_unique_identity_strategy(self):
        for sign in ("S1", "S2"):
            self.assertTrue(self.repo.save_promotion_link_cache(
                "pdd", {"final_link": f"https://example.com/{sign}", "from_cache": True}, goods_sign=sign,
            ))
            result = self.repo.get_promotion_link_cache("pdd", goods_sign=sign)
            self.assertEqual(result["final_link"], f"https://example.com/{sign}")
            self.assertTrue(result["from_cache"])
        self.assertEqual(len(self.col.docs), 2)
        self.assertNotIn("from_cache", self.col.docs[0]["default_promotion"])
        self.assertNotIn("detail_updated_at", self.col.docs[0])
        self.repo.upsert_product_detail({"goods_id": "123", "goods_sign": "S1", "price": 10})
        self.assertEqual(len(self.col.docs), 2)
        self.assertEqual(self.repo.get_promotion_link_cache("pdd", goods_id="123")["final_link"], "https://example.com/S1")

    def test_non_numeric_platform_ids_work_for_explicit_promotion_identity(self):
        self.assertTrue(self.repo.save_promotion_link_cache("tb", {"final_link": "https://example.com/AB"}, goods_id="AB"))
        self.assertEqual(self.repo.get_promotion_link_cache("taobao", goods_id="AB")["final_link"], "https://example.com/AB")
        self.assertEqual(self.col.docs[0]["platform"], "taobao")


if __name__ == "__main__":
    unittest.main()
