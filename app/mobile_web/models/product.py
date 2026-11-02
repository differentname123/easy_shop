# -*- coding: utf-8 -*-
"""
商品领域统一数据模型 (DTO)
"""
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class ProductSummary(BaseModel):
    """商品卡片极简摘要（专供列表推荐与搜索秒开）"""
    platform: str = "pdd"
    platform_name: str = "拼多多"
    goods_id: str
    goods_sign: str = ""
    name: str
    image_url: str
    price: float
    original_price: float
    coupon_amount: float = 0.0
    has_coupon: bool = False
    commission_rate: float = 0.0
    commission_amount: float = 0.0
    sales_tip: str = "0"
    mall_name: str = "品牌官方店"
    brand: str = "官方甄选"
    category: str = "精选好物"
    updated_at: Optional[str] = None


class ProductDetail(ProductSummary):
    """全屏深度商品详情（包含全量轮播图、参数、店铺评分与服务保障）"""
    desc: str = ""
    carousel_images: List[str] = Field(default_factory=list)
    product_url: str = ""
    detail_tags: List[str] = Field(default_factory=list)
    service_guarantees: List[str] = Field(default_factory=list)
    shop_ratings: Dict[str, str] = Field(default_factory=dict)
    specs: List[Dict[str, str]] = Field(default_factory=list)
    coupon_info: Dict[str, Any] = Field(default_factory=dict)
