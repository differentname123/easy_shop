# -*- coding: utf-8 -*-
"""
首页接口路由：拼多多进宝官方推荐与关键词检索
"""
from typing import Optional
from fastapi import APIRouter, Query
from app.mobile_web.services.product_service import ProductService

router = APIRouter(prefix="/api/home", tags=["首页推荐与搜索"])
product_service = ProductService()


@router.get("/channels")
def get_channels():
    """
    获取拼多多多多进宝官方推荐频道列表
    """
    return {
        "status": "success",
        "data": [
            {"channel_type": 5, "name": "实时热销", "icon": "fire"},
            {"channel_type": 1, "name": "今日爆款", "icon": "crown"},
            {"channel_type": 6, "name": "百亿补贴", "icon": "diamond"},
            {"channel_type": 3, "name": "高佣优选", "icon": "tag"},
            {"channel_type": 4, "name": "秒杀抢购", "icon": "clock"},
            {"channel_type": 7, "name": "品牌大促", "icon": "star"},
        ]
    }


@router.get("/recommend")
def get_recommend_goods(
    channel_type: int = Query(5, ge=1, le=20, description="频道代码：5-实时热销, 1-今日爆款, 6-百亿补贴"),
    offset: int = Query(0, ge=0, le=200, description="偏移量 (最大200)"),
    limit: int = Query(20, ge=1, le=50, description="条数 (1-50)"),
    uid: Optional[str] = Query(None, max_length=64, description="推手用户UID"),
    platform: str = Query("pdd", pattern="^(pdd|taobao|jd)$", description="平台标识"),
):
    """
    获取首页官方推荐流商品
    """
    return product_service.get_home_recommendations(
        channel_type=channel_type,
        offset=offset,
        limit=limit,
        uid=uid,
        platform=platform
    )


@router.get("/search")
def search_goods(
    keyword: str = Query(..., min_length=1, max_length=30, description="搜索关键词 (1-30字)"),
    page: int = Query(1, ge=1, le=20, description="页码 (最大20页)"),
    page_size: int = Query(20, ge=1, le=50, description="每页数量 (1-50)"),
    uid: Optional[str] = Query(None, max_length=64, description="推手用户UID"),
    platform: str = Query("pdd", pattern="^(pdd|taobao|jd)$", description="平台标识"),
):
    """
    首页关键词搜索联盟商品
    """
    return product_service.search_home_goods(
        keyword=keyword,
        page=page,
        page_size=page_size,
        uid=uid,
        platform=platform
    )


@router.get("/detail")
def get_goods_detail(
    goods_sign: Optional[str] = Query(None, max_length=128, description="商品Sign"),
    goods_id: Optional[str] = Query(None, max_length=64, description="商品ID"),
    uid: Optional[str] = Query(None, max_length=64, description="推手用户UID"),
    platform: str = Query("pdd", pattern="^(pdd|taobao|jd)$", description="平台标识"),
):
    """
    获取官方商品完整规范化详情 (含多张高清实拍轮播图、满减优惠、店铺与服务保障)
    """
    key = goods_sign or goods_id or ""
    return product_service.get_goods_detail(
        platform=platform,
        goods_id_or_sign=key,
        uid=uid
    )
