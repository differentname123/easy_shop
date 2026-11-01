# -*- coding: utf-8 -*-
"""
用户数据仓储模块 (MongoDB: users 集合)
管理推手/返利用户的 UID、授权状态及基本信息。
"""
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from common.mongo_db.mongo_base import gen_db_object
from app.mobile_web.config import logger


class UserRepository:
    COLLECTION_NAME = "users"

    def __init__(self, db_instance=None):
        self._db = db_instance or gen_db_object()
        try:
            self._db.create_index(self.COLLECTION_NAME, [("uid", 1)], unique=True)
            self._db.create_index(self.COLLECTION_NAME, [("created_at", -1)])
        except Exception as e:
            logger.warning(f"用户集合索引创建异常 (非阻塞): {e}")

    @property
    def col(self):
        return self._db.get_collection(self.COLLECTION_NAME)

    @staticmethod
    def generate_uid() -> str:
        """生成带前缀的高可读性推手唯一标识，如 u_9b2e61a4"""
        token = uuid.uuid4().hex[:8]
        return f"u_{token}"

    def get_user_by_uid(self, uid: str) -> Optional[Dict[str, Any]]:
        """按 uid 查询用户"""
        if not uid:
            return None
        doc = self.col.find_one({"uid": str(uid).strip()})
        if doc and "_id" in doc:
            doc["_id"] = str(doc["_id"])
        return doc

    def create_user(self, uid: Optional[str] = None, nickname: Optional[str] = None) -> Dict[str, Any]:
        """创建新用户并持久化"""
        now = datetime.now(timezone.utc)
        final_uid = str(uid).strip() if uid else self.generate_uid()
        final_nickname = nickname or f"推手_{final_uid[-4:]}"

        user_doc = {
            "uid": final_uid,
            "nickname": final_nickname,
            "avatar": "https://img.pddpic.com/mms-avatar/default_avatar.png",
            "created_at": now,
            "last_active_at": now,
            "balance": 0.0,           # 可提现返利余额
            "total_rebate": 0.0,      # 累计返利收益
            "pending_rebate": 0.0,    # 待结算返利
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
        except Exception as e:
            # 兼容并发创建已存在的情形
            logger.warning(f"创建用户插入冲突，回退查询: {e}")
            existing = self.get_user_by_uid(final_uid)
            if existing:
                return existing
            raise

    def get_or_create_user(self, uid: Optional[str] = None) -> Dict[str, Any]:
        """按 uid 获取，不存在则创建"""
        if uid:
            user = self.get_user_by_uid(uid)
            if user:
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

    def touch_user_activity(self, uid: str) -> None:
        """更新最后活跃时间"""
        if not uid:
            return
        now = datetime.now(timezone.utc)
        self.col.update_one(
            {"uid": str(uid).strip()},
            {"$set": {"last_active_at": now}}
        )
