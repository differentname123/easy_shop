# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _required_string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"必须提供非空字符串字段: {field}")
    return value.strip()


def _platform(value):
    """平台代码使用小写，允许后续接入新平台而不修改存储层。"""
    return _required_string(value, "platform").lower()


def _product_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("product_id 必须是非空字符串或整数")
    return _required_string(str(value), "product_id")


class ProductManager:
    """多平台商品最新信息；联合身份为平台代码和平台内商品 ID。"""

    COLLECTION_NAME = "products"
    UNIQUE_KEYS = ["platform", "product_id"]

    def __init__(self, db_instance):
        if db_instance is None:
            raise ValueError("必须提供有效的 MongoBase 实例")
        self.db = db_instance
        self.collection_name = self.COLLECTION_NAME
        self.db.create_index(self.collection_name, [("platform", 1), ("product_id", 1)], unique=True)
        self.db.create_index(self.collection_name, [("platform", 1), ("updated_at", -1)])

    def upsert_products(self, records):
        """完整校验后批量入库；同批相同身份保留最后一条，不修改输入。"""
        batch = {}
        now = datetime.now(timezone.utc)
        for record in records:
            item = record.copy()
            item.pop("_id", None)
            item["platform"] = _platform(item.get("platform"))
            item["product_id"] = _product_id(item.get("product_id"))
            item["updated_at"] = now
            batch[(item["platform"], item["product_id"])] = item

        if not batch:
            return {"new": 0, "update": 0}
        result = self.db.bulk_upsert(self.collection_name, list(batch.values()), self.UNIQUE_KEYS)
        counts = {"new": result.upserted_count, "update": result.matched_count}
        logger.info("商品批量入库完成 | 新增: [%d] 更新: [%d]", counts["new"], counts["update"])
        return counts

    def find_products(self, platform, limit=100):
        """按平台查询最新商品；limit=0 表示不限制数量。"""
        return self.db.find_many(
            self.collection_name, query={"platform": _platform(platform)},
            sort=[("updated_at", -1)], limit=limit,
        )

    def find_products_by_ids(self, platform, product_ids):
        """按指定平台和商品 ID 查询，禁止跨平台混查同名 ID。"""
        platform = _platform(platform)
        ids = [_product_id(value) for value in product_ids]
        if not ids:
            return []
        return self.db.find_many(
            self.collection_name, query={"platform": platform, "product_id": {"$in": ids}},
        )

    def count_products(self, platform):
        return self.db.get_collection(self.collection_name).count_documents({"platform": _platform(platform)})


class AccountStatusManager:
    """平台账号的上次使用时间；账号键为完整浏览器用户目录路径。"""

    COLLECTION_NAME = "crawler_account_status"
    UNIQUE_KEYS = ["platform", "account"]

    def __init__(self, db_instance):
        if db_instance is None:
            raise ValueError("必须提供有效的 MongoBase 实例")
        self.db = db_instance
        self.collection_name = self.COLLECTION_NAME
        self.db.create_index(self.collection_name, [("platform", 1), ("account", 1)], unique=True)

    def get_last_used_times(self, platform, accounts):
        platform = _platform(platform)
        accounts = [_required_string(account, "account") for account in accounts]
        if not accounts:
            return {}
        records = self.db.find_many(
            self.collection_name,
            query={"platform": platform, "account": {"$in": accounts}},
            projection={"_id": 0, "account": 1, "last_used_at": 1},
        )
        return {record["account"]: record.get("last_used_at") for record in records}

    def touch_account(self, platform, account):
        """在启动账号探测/采集前原子更新使用时间。"""
        record = {
            "platform": _platform(platform),
            "account": _required_string(account, "account"),
            "last_used_at": datetime.now(timezone.utc),
        }
        return self.db.bulk_upsert(self.collection_name, [record], self.UNIQUE_KEYS)
