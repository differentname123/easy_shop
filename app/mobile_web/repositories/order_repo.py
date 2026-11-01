# -*- coding: utf-8 -*-
"""
订单数据仓储模块 (MongoDB: orders 集合)
管理电商多平台订单记录，支持增量幂等入库、推手UID归因筛选与返利佣金统计。
"""
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from pymongo import UpdateOne
from common.mongo_db.mongo_base import gen_db_object
from common.common_utils import setup_logger

logger = setup_logger(app_name="mobile_web")


class OrderRepository:
    COLLECTION_NAME = "orders"

    def __init__(self, db_instance=None):
        self._db = db_instance or gen_db_object()
        try:
            raw_col = self.col
            # 唯一索引：同一平台同一订单号唯一
            raw_col.create_index([("platform", 1), ("order_sn", 1)], unique=True)
            # 复合索引：按推手归属与付款时间快速查询
            raw_col.create_index([("platform", 1), ("promoter_uid", 1), ("order_pay_time", -1)])
            raw_col.create_index([("order_modify_at", -1)])
        except Exception as e:
            logger.warning(f"订单集合索引创建提示: {e}")

    @property
    def col(self):
        return self._db.get_collection(self.COLLECTION_NAME)

    def upsert_orders(self, orders: List[Dict[str, Any]], platform: str = "pdd") -> int:
        """
        批量增量更新或插入订单 (幂等写入)
        """
        if not orders:
            return 0

        now = datetime.now(timezone.utc)
        operations = []
        for o in orders:
            order_sn = str(o.get("order_sn", "")).strip()
            if not order_sn:
                continue

            doc = dict(o)
            doc["platform"] = platform
            doc["synced_at"] = now
            # 确保 promoter_uid 统一为字符串或 None
            if doc.get("promoter_uid"):
                doc["promoter_uid"] = str(doc["promoter_uid"]).strip()

            operations.append(
                UpdateOne(
                    {"platform": platform, "order_sn": order_sn},
                    {"$set": doc},
                    upsert=True
                )
            )

        if not operations:
            return 0

        try:
            res = self.col.bulk_write(operations, ordered=False)
            upserted_count = res.upserted_count + res.modified_count
            logger.info(f"成功同步入库 {len(operations)} 笔订单，新增/更新计数: {upserted_count}")
            return len(operations)
        except Exception as e:
            logger.error(f"批量写入订单异常: {e}")
            return 0

    def query_user_orders(
        self,
        platform: str = "pdd",
        promoter_uid: str = "",
        start_timestamp: Optional[int] = None,
        limit: int = 100
    ) -> Dict[str, Any]:
        """
        查询属于指定推手 UID 的订单，并统计其预估佣金、已结算佣金及维权退款明细
        """
        if not promoter_uid:
            return {
                "total_count": 0,
                "total_commission": 0.0,
                "settled_commission": 0.0,
                "refund_commission": 0.0,
                "refund_count": 0,
                "order_list": []
            }

        query: Dict[str, Any] = {
            "platform": platform,
            "promoter_uid": str(promoter_uid).strip(),
        }

        # 时间跨度过滤 (order_pay_time)
        if start_timestamp and start_timestamp > 0:
            query["order_pay_time"] = {"$gte": int(start_timestamp)}

        try:
            cursor = self.col.find(query).sort([("order_pay_time", -1)]).limit(limit)
            orders = list(cursor)

            total_commission = 0.0
            settled_commission = 0.0
            refund_commission = 0.0
            refund_count = 0
            order_list = []

            for doc in orders:
                doc.pop("_id", None)
                comm = float(doc.get("promotion_amount_yuan") or 0.0)
                is_refund = bool(doc.get("is_refund", False) or doc.get("order_status") == 4)
                is_settled = bool(doc.get("is_settled", False) or doc.get("order_status") in [3, 5])

                if is_refund:
                    refund_count += 1
                    refund_commission += comm
                else:
                    total_commission += comm
                    if is_settled:
                        settled_commission += comm

                # 格式化支付时间
                pay_time_sec = doc.get("order_pay_time")
                pay_time_str = datetime.fromtimestamp(pay_time_sec, timezone.utc).strftime("%Y-%m-%d %H:%M") if pay_time_sec else ""

                order_list.append({
                    "order_sn": doc.get("order_sn"),
                    "goods_id": doc.get("goods_id"),
                    "goods_name": doc.get("goods_name", "精选商品"),
                    "goods_thumbnail_url": doc.get("goods_thumbnail_url", ""),
                    "goods_quantity": doc.get("goods_quantity", 1),
                    "order_amount_yuan": doc.get("order_amount_yuan", 0.0),
                    "promotion_amount_yuan": comm,
                    "promotion_rate": doc.get("promotion_rate", 0),
                    "order_status": doc.get("order_status"),
                    "order_status_desc": doc.get("order_status_desc", "未知状态"),
                    "is_refund": is_refund,
                    "is_settled": is_settled,
                    "order_pay_time": pay_time_sec,
                    "order_pay_time_str": pay_time_str,
                })

            return {
                "total_count": len(order_list),
                "total_commission": round(total_commission, 2),
                "settled_commission": round(settled_commission, 2),
                "refund_commission": round(refund_commission, 2),
                "refund_count": refund_count,
                "order_list": order_list,
            }
        except Exception as e:
            logger.error(f"查询用户订单异常: {e}")
            return {
                "total_count": 0,
                "total_commission": 0.0,
                "settled_commission": 0.0,
                "refund_commission": 0.0,
                "refund_count": 0,
                "order_list": []
            }
