# -*- coding: utf-8 -*-

import logging
import urllib.parse

from pymongo import MongoClient, UpdateOne

from common.common_utils import get_config

logger = logging.getLogger(__name__)


class MongoBase:
    _instance = None
    _client = None

    def __new__(cls, *args, **kwargs):
        """单例模式：确保全局只维护一个数据库连接池。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, host="localhost", port=27017, username=None, password=None,
                 db_name="admin", auth_source="admin", max_pool_size=100,
                 timeout_ms=5000):
        if self._client is None:
            if username and password:
                username = urllib.parse.quote_plus(username)
                password = urllib.parse.quote_plus(password)
                uri = f"mongodb://{username}:{password}@{host}:{port}/{auth_source}"
            else:
                uri = f"mongodb://{host}:{port}/"

            self._client = MongoClient(
                uri, maxPoolSize=max_pool_size, connect=False, tz_aware=True,
                serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms,
                socketTimeoutMS=timeout_ms,
            )
            self.db = self._client[db_name]

    def ping(self):
        """实际检查连接与认证，失败时由调用方终止启动。"""
        self._client.admin.command("ping")
        logger.info("MongoDB 连接成功 | 数据库: [%s]", self.db.name)

    def close(self):
        """关闭连接池，允许后续重新初始化。"""
        if self._client is not None:
            self._client.close()
            self._client = None
            self.db = None
            type(self)._instance = None

    def get_collection(self, collection_name):
        return self.db[collection_name]

    def find_many(self, collection_name, query=None, projection=None, limit=0, sort=None):
        """通用查询。"""
        if query is None:
            query = {}
        cursor = self.get_collection(collection_name).find(query, projection)
        if sort:
            cursor = cursor.sort(sort)
        if limit > 0:
            cursor = cursor.limit(limit)
        return list(cursor)

    def bulk_upsert(self, collection_name, data_list, unique_key_field):
        """以单字段或联合唯一键批量 upsert，返回写入结果；失败直接抛出。"""
        if not data_list:
            return None

        unique_keys = unique_key_field if isinstance(unique_key_field, list) else [unique_key_field]
        if not unique_keys:
            raise ValueError("必须提供唯一键字段")
        operations = []
        for item in data_list:
            update_data = item.copy()
            update_data.pop("_id", None)
            query = {}
            for key in unique_keys:
                value = update_data.pop(key, None)
                if value is None or value == "":
                    raise ValueError(f"数据项缺少唯一键字段: {key}")
                query[key] = value
            operations.append(UpdateOne(query, {"$set": update_data}, upsert=True))

        return self.get_collection(collection_name).bulk_write(operations, ordered=False)

    def create_index(self, collection_name, keys, unique=False):
        """创建索引；索引冲突或连接故障不能静默忽略。"""
        return self.get_collection(collection_name).create_index(keys, unique=unique)


def gen_db_object():
    """从现有本地配置生成数据库连接实例。"""
    return MongoBase(
        host=get_config("local_mongo_host"),
        port=get_config("local_mongo_port"),
        username=get_config("local_mongo_user"),
        password=get_config("local_mongo_password"),
        db_name=get_config("local_mongo_db_name"),
        max_pool_size=50,
    )
