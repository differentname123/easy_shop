# -*- coding: utf-8 -*-
"""
订单业务服务
实现两步走订单同步：
1. 增量拉取官方23小时内订单并幂等入库；
2. 按照指定推手UID和时间跨度汇总计算返利佣金与退款异常并输出。
"""
import time
from typing import Dict, Any, Optional
from app.mobile_web.repositories.order_repo import OrderRepository
from app.mobile_web.config import PDD_CLIENT_ID, PDD_CLIENT_SECRET
from common.common_utils import setup_logger
from app.pdd_utils import query_increment_orders

logger = setup_logger(app_name="mobile_web")

# 上次官方订单同步时间记录，避免 60 秒内无节制轰炸官方网关
_LAST_SYNC_TIMESTAMP: Dict[str, float] = {}
SYNC_INTERVAL = 60


class OrderService:
    def __init__(self, order_repo: Optional[OrderRepository] = None):
        self.repo = order_repo or OrderRepository()

    def sync_and_query_orders(
        self,
        platform: str = "pdd",
        uid: str = "",
        days: int = 7,
        force_sync: bool = False
    ) -> Dict[str, Any]:
        """
        两步执行：
        第1步：拉取最近23小时内全量增量订单并入库更新；
        第2步：按指定时间跨度查询属于该推手UID的订单明细并汇总统计。
        """
        plat = (platform or "pdd").lower()
        now = time.time()
        last_sync = _LAST_SYNC_TIMESTAMP.get(plat, 0.0)

        # ---------------- 步骤1：增量同步全量23小时订单入库 ----------------
        should_sync = force_sync or (now - last_sync >= SYNC_INTERVAL)
        if should_sync and plat == "pdd":
            try:
                # 拼多多单次查询最大跨度为24小时，此处严格取23小时保证在安全范围内
                end_time = int(now)
                start_time = end_time - 23 * 3600
                logger.info(f"开始拉取拼多多最近23小时增量订单: [{start_time} -> {end_time}]")

                page = 1
                total_synced = 0
                while True:
                    res = query_increment_orders(
                        client_id=PDD_CLIENT_ID,
                        client_secret=PDD_CLIENT_SECRET,
                        start_time=start_time,
                        end_time=end_time,
                        page=page,
                        page_size=50
                    )
                    orders = res.get("order_list", [])
                    if not orders:
                        break

                    self.repo.upsert_orders(orders, platform="pdd")
                    total_synced += len(orders)
                    if len(orders) < 50 or page >= 10:  # 保护上限
                        break
                    page += 1

                _LAST_SYNC_TIMESTAMP[plat] = now
                logger.info(f"完成23小时增量订单同步，共拉取处理 {total_synced} 笔订单")
            except Exception as e:
                logger.error(f"同步官方增量订单失败: {e}")

        # ---------------- 步骤2：按用户UID和时间跨度查询订单 ----------------
        start_ts = None
        if days and days > 0:
            start_ts = int(now - days * 86400)

        result = self.repo.query_user_orders(
            platform=plat,
            promoter_uid=uid,
            start_timestamp=start_ts,
            limit=100
        )
        result["platform"] = plat
        result["uid"] = uid
        result["query_days"] = days
        result["last_sync_time"] = int(_LAST_SYNC_TIMESTAMP.get(plat, now))
        return {"status": "success", "data": result}
