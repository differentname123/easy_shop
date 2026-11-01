# -*- coding: utf-8 -*-
"""
用户与返利授权业务服务
处理推手唯一身份识别、渠道备案状态校验与专属授权链接生成。
"""
from typing import Dict, Any, Optional
from app.mobile_web.repositories.user_repo import UserRepository
from app.mobile_web.adapters import get_adapter
from app.mobile_web.config import logger


class UserService:
    def __init__(self, user_repo: Optional[UserRepository] = None):
        self.repo = user_repo or UserRepository()

    def init_user(self, uid: Optional[str] = None) -> Dict[str, Any]:
        """初始化或读取推手用户信息"""
        user = self.repo.get_or_create_user(uid=uid)
        return {"status": "success", "user": user}

    def get_user_info(self, uid: str) -> Dict[str, Any]:
        """获取用户信息及各平台授权状态"""
        if not uid:
            return {"status": "error", "error_msg": "uid 不能为空"}
        user = self.repo.get_user_by_uid(uid)
        if not user:
            user = self.repo.create_user(uid=uid)
        return {"status": "success", "user": user}

    def check_and_get_auth(self, uid: str, platform: str = "pdd") -> Dict[str, Any]:
        """
        检查指定用户在指定平台的授权备案状态。
        若未授权，则实时生成专属备案授权链接；若已授权，则直接返回已授权状态。
        """
        plat = (platform or "pdd").lower()
        if not uid:
            return {"status": "error", "error_msg": "uid 不能为空"}

        # 确保用户记录存在
        user = self.repo.get_or_create_user(uid=uid)
        adapter = get_adapter(plat)

        # 1. 向平台联盟查询真实备案状态
        auth_res = adapter.check_authority(uid=uid)
        is_authorized = auth_res.get("is_authorized", False)
        bind = auth_res.get("bind", 0)

        # 2. 同步并持久化最新状态到本地数据库
        self.repo.update_auth_status(uid=uid, platform=plat, is_authorized=is_authorized, bind=bind)

        # 3. 如果已经授权，直接返回授权成功
        if is_authorized:
            return {
                "status": "success",
                "platform": plat,
                "platform_name": adapter.platform_name,
                "is_authorized": True,
                "bind": bind,
                "msg": f"用户 [{uid}] 已成功完成 {adapter.platform_name} 渠道授权备案",
                "auth_url": "",
                "short_url": ""
            }

        # 4. 未授权：实时生成带该推手 uid 的专属备案授权链接
        url_gen_res = adapter.generate_authority_url(uid=uid)
        auth_url = url_gen_res.get("auth_url") or url_gen_res.get("short_url") or ""
        short_url = url_gen_res.get("short_url") or auth_url

        return {
            "status": "success",
            "platform": plat,
            "platform_name": adapter.platform_name,
            "is_authorized": False,
            "bind": 0,
            "msg": f"用户 [{uid}] 尚未在 {adapter.platform_name} 完成授权，请点击链接前往授权",
            "auth_url": auth_url,
            "short_url": short_url,
            "we_app_info": url_gen_res.get("we_app_info", {})
        }
