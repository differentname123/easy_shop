# -*- coding: utf-8 -*-
"""
订单接口路由：支持23小时增量订单同步与推手返利收益明细查询
"""
from typing import Optional
from fastapi import APIRouter, Query
from app.mobile_web.services.order_service import OrderService

router = APIRouter(prefix="/api/order", tags=["订单与收益"])
order_service = OrderService()


@router.post("/sync_and_query")
def sync_and_query_orders(
    uid: str = Query(..., description="推手用户UID"),
    platform: str = Query("pdd", description="平台: pdd, taobao, jd"),
    days: int = Query(7, description="查询时间跨度(天)，默认7天"),
    force: bool = Query(False, description="是否强制立即同步官方接口"),
):
    """
    触发23小时全量增量订单同步入库，并查询当前推手在指定时间范围内的订单列表及返利总计。
    """
    return order_service.sync_and_query_orders(
        platform=platform,
        uid=uid,
        days=days,
        force_sync=force
    )
