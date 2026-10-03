# [功能摘要] 管理一个 MongoDB 连接池，提供无业务规则的查询、集合访问与索引创建。
# [输入数据] 现有 local_mongo_* 配置；Mongo 查询条件、投影和排序参数。
# [数据流转/交互] 控制器创建并复用连接池 → ping 校验连接 → 管理器访问集合；查询游标及时关闭。
# [输出数据] 返回商品字典列表或原生集合；退出时释放连接，数据库异常交给应用层处理。

import urllib.parse

from pymongo import MongoClient

from common.common_utils import get_config


class MongoBase:
    """连接池由控制器拥有，避免可关闭的全局单例影响其他使用者。"""

    def __init__(self, host="localhost", port=27017, username=None, password=None,
                 db_name="admin", auth_source="admin", max_pool_size=100,
                 timeout_ms=5000):
        if bool(username) != bool(password):
            raise ValueError("MongoDB 用户名和密码必须同时配置")
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
        try:
            self.db = self._client[db_name]
        except BaseException:
            self._client.close()
            raise

    def ping(self):
        """启动时实际检查网络与认证；不在底层重复打印成功或失败日志。"""
        if self._client is None:
            raise RuntimeError("MongoDB 连接已关闭")
        self._client.admin.command("ping")

    def close(self):
        """释放连接池，可重复调用；新一轮重连使用新实例。"""
        client = self._client
        self._client = self.db = None
        if client is not None:
            client.close()

    def get_collection(self, collection_name):
        if self.db is None:
            raise RuntimeError("MongoDB 连接已关闭")
        return self.db[collection_name]

    def find_many(self, collection_name, query=None, projection=None, limit=0, sort=None):
        """query/projection 为 Mongo 字典；sort 为 [(字段, 方向)]；返回 list[dict]，limit=0 不限量。"""
        if type(limit) is not int or limit < 0:
            raise ValueError("limit 必须是非负整数，0 表示不限量")
        with self.get_collection(collection_name).find(
                {} if query is None else query, projection) as cursor:
            if sort:
                cursor = cursor.sort(sort)
            if limit:
                cursor = cursor.limit(limit)
            return list(cursor)

    def create_index(self, collection_name, keys, unique=False):
        """keys 为 [(字段, 方向)]；索引冲突和连接故障直接向上传播。"""
        return self.get_collection(collection_name).create_index(keys, unique=unique)


def gen_db_object():
    """保持现有配置读取方式；连接复用和关闭由调用方负责。"""
    return MongoBase(
        host=get_config("local_mongo_host"),
        port=get_config("local_mongo_port"),
        username=get_config("local_mongo_user"),
        password=get_config("local_mongo_password"),
        db_name=get_config("local_mongo_db_name"),
        max_pool_size=50,
    )
