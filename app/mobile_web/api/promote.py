# -*- coding: utf-8 -*-
"""
转链接口路由：根据用户 UID 动态生成带返利归因的推广链接
"""
from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel
from app.mobile_web.services.promote_service import PromoteService

router = APIRouter(prefix="/api/promote", tags=["推广生链与归因"])
promote_service = PromoteService()


class ConvertRequest(BaseModel):
    platform: str = "pdd"
    goods_sign: Optional[str] = None
    goods_id: Optional[str] = None
    product_url: Optional[str] = None
    uid: Optional[str] = None


@router.post("/convert")
def convert_link(req: ConvertRequest):
    """
    点击商品时生成推手专属转链
    """
    goods_key = req.goods_sign or req.goods_id or ""
    return promote_service.convert_link(
        platform=req.platform,
        goods_id_or_sign=goods_key,
        product_url=req.product_url,
        uid=req.uid,
    )
