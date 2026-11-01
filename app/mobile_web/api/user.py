# -*- coding: utf-8 -*-
"""
用户中心接口路由：推手手机号合规注册/登录、UID管理与多平台渠道备案授权
"""
from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field
from app.mobile_web.services.user_service import UserService

router = APIRouter(prefix="/api/user", tags=["用户与授权"])
user_service = UserService()


class RegisterOrLoginRequest(BaseModel):
    phone: str = Field(..., description="11位中国大陆手机号")
    password: str = Field(..., min_length=6, max_length=32, description="登录密码")
    nickname: Optional[str] = Field(None, description="用户昵称")
    invite_code: Optional[str] = Field(None, description="上级邀请码 (选填)")


class UserInitRequest(BaseModel):
    uid: Optional[str] = None
    nickname: Optional[str] = None


@router.post("/register_or_login")
def register_or_login(req: RegisterOrLoginRequest):
    """
    推手手机号注册与登录一体化接口
    - 手机号不存在：自动注册新账号，绑定邀请码，生成专属 UID 与邀请码；
    - 手机号已存在：校验密码并登录，返回该账号对应的推手 UID 与授权记录。
    """
    res = user_service.register_or_login(
        phone=req.phone,
        password=req.password,
        nickname=req.nickname,
        invite_code=req.invite_code,
    )
    if res.get("status") != "success":
        raise HTTPException(status_code=400, detail=res.get("error_msg", "操作失败"))
    return res


@router.post("/init")
def init_user(req: UserInitRequest):
    """
    推手用户初始化（支持前端传递已有 UID 或服务端自动生成）
    """
    return user_service.init_user(uid=req.uid)


@router.get("/info")
def get_user_info(
    uid: Optional[str] = Query(None, description="用户唯一UID"),
    phone: Optional[str] = Query(None, description="手机号"),
):
    """
    获取用户信息及各平台授权状态
    """
    if not uid and not phone:
        raise HTTPException(status_code=400, detail="必须提供 uid 或 phone 参数")
    res = user_service.get_user_info(uid=uid, phone=phone)
    if res.get("status") != "success":
        raise HTTPException(status_code=404, detail=res.get("error_msg", "用户不存在"))
    return res


@router.get("/auth")
def check_or_get_auth(
    uid: str = Query(..., description="用户唯一UID"),
    platform: str = Query("pdd", description="平台代码: pdd, taobao, jd"),
):
    """
    查询该用户在指定平台的授权备案状态。若未授权，则自动返回专属备案授权链接。
    """
    return user_service.check_and_get_auth(uid=uid, platform=platform)
