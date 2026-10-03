# [功能摘要] 对 products 只提供查询和局部更新两个业务接口。
# [输入数据] Mongo 查询字典；单个商品补丁或补丁列表，每项必含 platform/product_id。
# [数据流转/交互] 统一商品身份 → 合并同批字段补丁 → 完整校验 → 使用 $set/$inc 批量写入；
#                 筛选规则、时间戳、格式化结果和重试次数均由应用层显式传入。
# [输出数据] 返回商品字典列表或 {new, update, modified} 计数；未传字段保持原值，异常上抛。

import math

from pymongo import UpdateOne


class ProductManager:
    """唯一身份为 (platform, product_id)，不填默认业务字段，也不替换整条商品。"""

    COLLECTION_NAME = "products"

    def __init__(self, db_instance):
        if db_instance is None:
            raise ValueError("必须提供有效的 MongoBase 实例")
        self._db = db_instance
        self._db.create_index(self.COLLECTION_NAME, [("platform", 1), ("product_id", 1)], unique=True)
        self._db.create_index(self.COLLECTION_NAME, [("platform", 1), ("updated_at", -1)])

    def query(self, query=None, projection=None, sort=None, limit=0):
        """入参沿用 Mongo 条件/投影/排序形貌；返回 list[dict]，业务过滤条件由应用层构造。"""
        return self._db.find_many(
            self.COLLECTION_NAME, query=query, projection=projection, sort=sort, limit=limit,
        )

    @staticmethod
    def _prepare_updates(records, condition, increments, upsert):
        """把补丁完整校验为 [(身份条件, 更新字典)]，确保非法批次在任何数据库写入前失败。"""
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, (list, tuple)):
            raise TypeError("records 必须是商品字典或商品字典列表")
        if type(upsert) is not bool:
            raise TypeError("upsert 必须是布尔值")
        if condition is not None and not isinstance(condition, dict):
            raise TypeError("condition 必须是 Mongo 查询字典")
        if condition and upsert:
            raise ValueError("带附加条件的更新必须设置 upsert=False，避免条件不符时误插入")
        if increments is not None and not isinstance(increments, dict):
            raise TypeError("increments 必须是 {字段: 增量} 字典")
        increments = {} if increments is None else increments.copy()
        for field, value in increments.items():
            if not isinstance(field, str) or field.split(".")[0] in {"_id", "platform", "product_id"}:
                raise ValueError("不能递增商品身份字段，增量字段名必须是字符串")
            valid_int = type(value) is int and -(2 ** 63) <= value < 2 ** 63
            valid_float = type(value) is float and math.isfinite(value)
            if not (valid_int or valid_float):
                raise ValueError(f"增量必须是可存储的有限数值: {field}")

        batch = {}
        for record in records:
            if not isinstance(record, dict):
                raise TypeError("每个商品补丁必须是字典")
            item = record.copy()
            item.pop("_id", None)
            platform, product_id = item.get("platform"), item.get("product_id")
            if not isinstance(platform, str) or not platform.strip():
                raise ValueError("platform 必须是非空字符串")
            if type(product_id) not in (str, int) or not str(product_id).strip():
                raise ValueError("product_id 必须是非空字符串或整数，不能是布尔值")
            item.update(platform=platform.strip().lower(), product_id=str(product_id).strip())
            identity = (item["platform"], item["product_id"])
            batch.setdefault(identity, {}).update(item)

        prepared = []
        for (platform, product_id), fields in batch.items():
            paths = list(fields) + list(increments)
            if any(not isinstance(path, str) or "\x00" in path or
                   any(not part or part.startswith("$") for part in path.split(".")) for path in paths):
                raise ValueError("更新字段名不能含空路径、空字符或以 $ 开头的路径片段")
            if any(path.split(".")[0] in {"_id", "platform", "product_id"} and "." in path for path in paths):
                raise ValueError("不能更新商品身份的子字段")
            if len(set(paths)) != len(paths):
                raise ValueError("同一字段不能同时赋值和递增")
            path_set = set(paths)
            if any(".".join(path.split(".")[:index]) in path_set
                   for path in paths for index in range(1, len(path.split(".")))):
                raise ValueError("同一次更新不能同时提交父字段和它的子字段")
            identity = {"platform": platform, "product_id": product_id}
            query = {"$and": [identity, condition]} if condition else identity
            update = {"$set": fields}
            if increments:
                update["$inc"] = increments
            prepared.append((query, update))
        return prepared

    def update(self, records, *, condition=None, increments=None, upsert=True):
        """records: {platform, product_id, 需修改字段} 或其列表；返回 {new, update, modified}。
        只赋值显式字段；None/空值是显式赋值，嵌套局部更新使用 '父字段.子字段' 点路径。
        condition 是附加匹配条件，increments 是原子增量；带条件时禁止插入。
        字典/列表字段作为整体赋值；同批同身份按字段合并，重复字段以后一次为准，增量执行一次。
        """
        prepared = self._prepare_updates(records, condition, increments, upsert)
        if not prepared:
            return {"new": 0, "update": 0, "modified": 0}
        operations = [UpdateOne(query, update, upsert=upsert) for query, update in prepared]
        result = self._db.get_collection(self.COLLECTION_NAME).bulk_write(operations, ordered=False)
        if not result.acknowledged:
            raise RuntimeError("商品写入未获得数据库确认，请检查 write concern 配置")
        return {"new": result.upserted_count, "update": result.matched_count, "modified": result.modified_count}
