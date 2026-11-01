# -*- coding: utf-8 -*-
"""
用户中心接口路由：推手身份管理、UID生成与多平台渠道备案授权
"""
from typing import Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel
from app.mobile_web.services.user_service import UserService

router = APIRouter(prefix="/api/user", tags=["用户与授权"])
user_service = UserService()


class UserInitRequest(BaseModel):
    uid: Optional[str] = None
    nickname: Optional[str] = None


@router.post("/init")
def init_user(req: UserInitRequest):
    """
    推手用户初始化（支持前端传递已有 UID 或服务端自动生成）
    """
    return user_service.init_user(uid=req.uid)


@router.get("/info")
def get_user_info(uid: str = Query(..., description="用户唯一UID")):
    """
    获取用户信息及各平台授权状态
    """
    return user_service.get_user_info(uid=uid)


@router.get("/auth")
def check_or_get_auth(
    uid: str = Query(..., description="用户唯一UID"),
    platform: str = Query("pdd", description="平台代码: pdd, taobao, jd"),
):
    """
    查询该用户在指定平台的授权备案状态。若未授权，则自动返回专属备案授权链接。
    """
    return user_service.check_and_get_auth(uid=uid, platform=platform)
