# -*- coding: utf-8 -*-
"""
用户数据仓储模块 (MongoDB: users 集合)
管理推手/返利用户的 手机号认证、密码哈希、专属 UID、邀请码及渠道授权状态。
"""
import uuid
import hashlib
import random
import string
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from common.mongo_db.mongo_base import gen_db_object
from common.common_utils import setup_logger
from app.mobile_web.auth import hash_password, verify_password

logger = setup_logger(app_name="mobile_web")


class UserRepository:
    COLLECTION_NAME = "users"

    def __init__(self, db_instance=None):
        self._db = db_instance or gen_db_object()
        try:
            raw_col = self.col
            raw_col.create_index([("uid", 1)], unique=True)
            raw_col.create_index([("phone", 1)], unique=True, sparse=True)
            raw_col.create_index([("invite_code", 1)], unique=True, sparse=True)
            raw_col.create_index([("created_at", -1)])
        except Exception as e:
            logger.warning(f"用户集合索引创建提示: {e}")

    @property
    def col(self):
        return self._db.get_collection(self.COLLECTION_NAME)

    @staticmethod
    def generate_uid() -> str:
        """生成带前缀的高可读性推手唯一标识，如 u_9b2e61a4"""
        token = uuid.uuid4().hex[:8]
        return f"u_{token}"

    def generate_unique_invite_code(self) -> str:
        """生成唯一的 6 位大写字母/数字邀请码"""
        chars = string.ascii_uppercase + "23456789"  # 去除易混淆的 0, 1, I, O
        for _ in range(10):
            code = "INV_" + "".join(random.choices(chars, k=6))
            if not self.col.find_one({"invite_code": code}):
                return code
        # 兜底
        return f"INV_{uuid.uuid4().hex[:6].upper()}"

    def get_user_by_uid(self, uid: str) -> Optional[Dict[str, Any]]:
        """按 uid 查询用户"""
        if not uid:
            return None
        doc = self.col.find_one({"uid": str(uid).strip()})
        if doc and "_id" in doc:
            doc["_id"] = str(doc["_id"])
        return doc

    def get_user_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        """按手机号查询用户"""
        if not phone:
            return None
        doc = self.col.find_one({"phone": str(phone).strip()})
        if doc and "_id" in doc:
            doc["_id"] = str(doc["_id"])
        return doc

    def get_user_by_invite_code(self, invite_code: str) -> Optional[Dict[str, Any]]:
        """按邀请码查询用户"""
        if not invite_code:
            return None
        doc = self.col.find_one({"invite_code": str(invite_code).strip().upper()})
        if doc and "_id" in doc:
            doc["_id"] = str(doc["_id"])
        return doc

    def register_or_login(
        self,
        phone: str,
        password: str,
        nickname: Optional[str] = None,
        invite_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        手机号注册登录一体化：
        - 若手机号已存在：校验密码，成功则登录并平滑升级哈希；
        - 若手机号未存在：自动注册新账号，绑定邀请码，直接登录。
        """
        phone = str(phone).strip()
        now = datetime.now(timezone.utc)

        existing = self.get_user_by_phone(phone)
        if existing:
            # 已注册用户：校验密码（兼容旧加盐 SHA256 与 PBKDF2）
            stored_hash = existing.get("password_hash")
            if not verify_password(password, stored_hash):
                raise ValueError("手机号已注册，输入的密码不正确")

            update_fields: Dict[str, Any] = {"last_login_at": now, "last_active_at": now}
            # 若仍为旧单次 SHA256 哈希，顺带静默平滑升级为 PBKDF2
            if not str(stored_hash or "").startswith("pbkdf2_sha256$"):
                update_fields["password_hash"] = hash_password(password)

            self.col.update_one(
                {"phone": phone},
                {"$set": update_fields}
            )
            existing["last_login_at"] = now
            existing["last_active_at"] = now
            existing.pop("password_hash", None)
            return existing

        # 新用户：注册
        pwd_hash = hash_password(password)
        new_uid = self.generate_uid()
        my_invite_code = self.generate_unique_invite_code()
        final_nickname = nickname.strip() if (nickname and nickname.strip()) else f"推手_{phone[-4:]}"

        # 检查上级邀请人
        invited_by = None
        if invite_code and invite_code.strip():
            inviter = self.get_user_by_invite_code(invite_code.strip())
            if inviter:
                invited_by = inviter.get("uid")

        user_doc = {
            "uid": new_uid,
            "phone": phone,
            "password_hash": pwd_hash,
            "nickname": final_nickname,
            "avatar": "https://img.pddpic.com/mms-avatar/default_avatar.png",
            "invite_code": my_invite_code,
            "invited_by": invited_by,
            "created_at": now,
            "last_login_at": now,
            "last_active_at": now,
            "balance": 0.0,
            "total_rebate": 0.0,
            "pending_rebate": 0.0,
            "auth_status": {
                "pdd": {"is_authorized": False, "bind": 0, "updated_at": None},
                "taobao": {"is_authorized": False, "bind": 0, "updated_at": None},
                "jd": {"is_authorized": False, "bind": 0, "updated_at": None},
            },
            "meta": {}
        }

        self.col.insert_one(user_doc)
        user_doc.pop("_id", None)
        user_doc.pop("password_hash", None)
        logger.info(f"新用户注册成功: phone={phone}, uid={new_uid}, invite_code={my_invite_code}, invited_by={invited_by}")
        return user_doc

    def create_user(self, uid: Optional[str] = None, nickname: Optional[str] = None) -> Dict[str, Any]:
        """匿名/访客用户初始化"""
        now = datetime.now(timezone.utc)
        final_uid = str(uid).strip() if uid else self.generate_uid()
        final_nickname = nickname or f"推手_{final_uid[-4:]}"
        my_invite_code = self.generate_unique_invite_code()

        user_doc = {
            "uid": final_uid,
            "phone": None,
            "password_hash": None,
            "nickname": final_nickname,
            "avatar": "https://img.pddpic.com/mms-avatar/default_avatar.png",
            "invite_code": my_invite_code,
            "invited_by": None,
            "created_at": now,
            "last_active_at": now,
            "balance": 0.0,
            "total_rebate": 0.0,
            "pending_rebate": 0.0,
            "auth_status": {
                "pdd": {"is_authorized": False, "bind": 0, "updated_at": None},
                "taobao": {"is_authorized": False, "bind": 0, "updated_at": None},
                "jd": {"is_authorized": False, "bind": 0, "updated_at": None},
            },
            "meta": {}
        }

        try:
            self.col.insert_one(user_doc)
            user_doc.pop("_id", None)
            return user_doc
        except Exception:
            existing = self.get_user_by_uid(final_uid)
            if existing:
                existing.pop("password_hash", None)
                return existing
            raise

    def get_or_create_user(self, uid: Optional[str] = None) -> Dict[str, Any]:
        """按 uid 获取，不存在则创建"""
        if uid:
            user = self.get_user_by_uid(uid)
            if user:
                user.pop("password_hash", None)
                return user
        return self.create_user(uid=uid)

    def update_auth_status(
        self,
        uid: str,
        platform: str,
        is_authorized: bool,
        bind: int = 0,
        extra: Optional[dict] = None
    ) -> bool:
        """更新推手在指定平台的渠道备案授权状态"""
        now = datetime.now(timezone.utc)
        plat = (platform or "pdd").lower()
        field_prefix = f"auth_status.{plat}"

        update_fields = {
            f"{field_prefix}.is_authorized": bool(is_authorized),
            f"{field_prefix}.bind": int(bind),
            f"{field_prefix}.updated_at": now,
            "last_active_at": now
        }
        if extra and isinstance(extra, dict):
            for k, v in extra.items():
                update_fields[f"{field_prefix}.{k}"] = v

        res = self.col.update_one(
            {"uid": str(uid).strip()},
            {"$set": update_fields}
        )
        return res.modified_count > 0 or res.matched_count > 0
