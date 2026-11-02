# -*- coding: utf-8 -*-
"""
订单业务服务 (多平台抽象与异步解耦)
1. 异步非阻塞后台拉取各平台增量订单并入库；
2. 0 秒阻塞立即按指定推手 UID 查询本地 MongoDB 订单明细与返利汇总。
"""
import time
import threading
from typing import Dict, Any, Optional
from app.mobile_web.adapters import get_adapter
from app.mobile_web.repositories.order_repo import OrderRepository
from app.mobile_web.config import ORDER_SYNC_INTERVAL, ORDER_SYNC_MAX_PAGES, logger

# 记录各平台上次增量拉单时间戳，并使用非阻塞互斥锁防止并发重叠执行
_LAST_SYNC_TIMESTAMP: Dict[str, float] = {}
_SYNC_LOCK = threading.Lock()


class OrderService:
    def __init__(self, order_repo: Optional[OrderRepository] = None):
        self.repo = order_repo or OrderRepository()

    def _sync_platform_orders_bg(self, platform: str, end_time: int) -> None:
        """后台独立守护线程执行订单增量拉取与幂等入库"""
        try:
            adapter = get_adapter(platform)
            start_time = end_time - 23 * 3600
            total_synced = 0
            logger.info(f"[{platform}] 开始后台拉取最近23小时增量订单: [{start_time} -> {end_time}]")

            for page in range(1, ORDER_SYNC_MAX_PAGES + 1):
                res = adapter.fetch_increment_orders(
                    start_time=start_time,
                    end_time=end_time,
                    page=page,
                    page_size=50
                )
                if res.get("status") != "success":
                    break
                orders = res.get("order_list", [])
                if not orders:
                    break

                self.repo.upsert_orders(orders, platform=platform)
                total_synced += len(orders)
                if len(orders) < 50:
                    break

            logger.info(f"[{platform}] 完成增量订单同步，共入库处理 {total_synced} 笔订单")
        except Exception as e:
            logger.error(f"[{platform}] 同步增量订单异常: {e}")
        finally:
            _SYNC_LOCK.release()

    def sync_and_query_orders(
        self,
        platform: str = "pdd",
        uid: str = "",
        days: int = 7,
        force_sync: bool = False  # 忽略外部传参，由后端安全策略严格控频
    ) -> Dict[str, Any]:
        """
        查询推手订单与返利收益明细：
        - 距离上次同步超过阈值且无后台任务在执行时，非阻塞触发单线程后台拉单；
        - 当前 HTTP 请求完全不等待网络 I/O，直接从本地数据库检索并瞬时返回。
        """
        plat = (platform or "pdd").lower()
        now = time.time()
        last_sync = _LAST_SYNC_TIMESTAMP.get(plat, 0.0)

        # ---------------- 步骤1：非阻塞尝试触发后台异步同步 ----------------
        if now - last_sync >= ORDER_SYNC_INTERVAL:
            if _SYNC_LOCK.acquire(blocking=False):
                _LAST_SYNC_TIMESTAMP[plat] = now
                threading.Thread(
                    target=self._sync_platform_orders_bg,
                    args=(plat, int(now)),
                    name=f"bg-order-sync-{plat}",
                    daemon=True
                ).start()

        # ---------------- 步骤2：纯本地查询属于该推手UID的订单 ----------------
        start_ts = int(now - days * 86400) if (days and days > 0) else None
        result = self.repo.query_user_orders(
            platform=plat,
            promoter_uid=str(uid).strip(),
            start_timestamp=start_ts,
            limit=100
        )
        result["platform"] = plat
        result["uid"] = str(uid).strip()
        result["query_days"] = days
        result["last_sync_time"] = int(_LAST_SYNC_TIMESTAMP.get(plat, now))
        return {"status": "success", "data": result}
