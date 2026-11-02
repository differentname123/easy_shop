# -*- coding: utf-8 -*-
"""
数据仓储抽象基类 (BaseRepository)
封装 MongoDB 连接获取、集合操作与统一索引保障
"""
from typing import Optional
from common.mongo_db.mongo_base import gen_db_object
from common.common_utils import setup_logger

logger = setup_logger(app_name="mobile_web")


class BaseRepository:
    """仓储基础类：提供统一的 DB 实例和集合访问"""
    COLLECTION_NAME: str = ""
    _initialized_collections = set()

    def __init__(self, db_instance=None):
        self._db = db_instance or gen_db_object()
        # 针对真实数据库连接，单进程生命周期内只初始化一次索引，避免多次重复调用
        if db_instance is None:
            if self.COLLECTION_NAME and self.COLLECTION_NAME not in BaseRepository._initialized_collections:
                self._init_indexes()
                BaseRepository._initialized_collections.add(self.COLLECTION_NAME)
        else:
            self._init_indexes()

    @property
    def col(self):
        if not self.COLLECTION_NAME:
            raise NotImplementedError("子类仓储必须声明 COLLECTION_NAME")
        return self._db.get_collection(self.COLLECTION_NAME)

    def _init_indexes(self):
        """子类可选覆盖，用于创建集合必要索引"""
        pass
