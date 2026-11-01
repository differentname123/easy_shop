# -*- coding: utf-8 -*-
"""
用户与返利授权业务服务
处理推手手机号合规注册/登录、密码校验、邀请码绑定与渠道备案授权。
"""
import re
from typing import Dict, Any, Optional
from app.mobile_web.repositories.user_repo import UserRepository
from app.mobile_web.adapters import get_adapter
from common.common_utils import setup_logger

logger = setup_logger(app_name="mobile_web")

# 中国大陆 11 位合法手机号正则
PHONE_REGEX = re.compile(r"^1[3-9]\d{9}$")


class UserService:
    def __init__(self, user_repo: Optional[UserRepository] = None):
        self.repo = user_repo or UserRepository()

    @staticmethod
    def validate_phone(phone: str) -> str:
        """严格校验并清洗手机号，防止输入奇怪内容"""
        if not phone or not isinstance(phone, str):
            raise ValueError("手机号不能为空")
        cleaned = phone.strip()
        if not PHONE_REGEX.match(cleaned):
            raise ValueError("手机号格式不正确，请输入有效的11位中国大陆手机号")
        return cleaned

    @staticmethod
    def validate_password(password: str) -> str:
        """校验密码有效性"""
        if not password or not isinstance(password, str):
            raise ValueError("密码不能为空")
        cleaned = password.strip()
        if len(cleaned) < 6:
            raise ValueError("密码长度不能少于6位")
        if len(cleaned) > 32:
            raise ValueError("密码长度不能超过32位")
        return cleaned

    def register_or_login(
        self,
        phone: str,
        password: str,
        nickname: Optional[str] = None,
        invite_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        手机号注册或登录
        """
        try:
            valid_phone = self.validate_phone(phone)
            valid_pwd = self.validate_password(password)
            user = self.repo.register_or_login(
                phone=valid_phone,
                password=valid_pwd,
                nickname=nickname,
                invite_code=invite_code
            )
            return {"status": "success", "user": user}
        except ValueError as e:
            return {"status": "error", "error_msg": str(e)}
        except Exception as e:
            logger.error(f"注册登录服务异常: {e}")
            return {"status": "error", "error_msg": f"系统异常: {str(e)}"}

    def init_user(self, uid: Optional[str] = None) -> Dict[str, Any]:
        """初始化或读取推手用户信息"""
        user = self.repo.get_or_create_user(uid=uid)
        return {"status": "success", "user": user}

    def get_user_info(self, uid: Optional[str] = None, phone: Optional[str] = None) -> Dict[str, Any]:
        """获取用户信息及各平台授权状态"""
        user = None
        if phone:
            user = self.repo.get_user_by_phone(phone)
        elif uid:
            user = self.repo.get_user_by_uid(uid)

        if not user:
            return {"status": "error", "error_msg": "用户未找到"}
        
        user.pop("password_hash", None)
        return {"status": "success", "user": user}

    def check_and_get_auth(self, uid: str, platform: str = "pdd") -> Dict[str, Any]:
        """
        检查指定用户在指定平台的授权备案状态。
        若未授权，则实时生成专属备案授权链接；若已授权，则直接返回已授权状态。
        """
        plat = (platform or "pdd").lower()
        if not uid:
            return {"status": "error", "error_msg": "uid 不能为空"}

        user = self.repo.get_user_by_uid(uid)
        if not user:
            return {"status": "error", "error_msg": "推手用户不存在"}

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
