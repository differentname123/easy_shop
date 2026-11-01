# -*- coding: utf-8 -*-
"""
商品数据仓储模块 (MongoDB: products 集合)
用于爆单推广库商品的多维度筛选、聚合排序与分页查询。
"""
import datetime
from typing import Dict, Any, List, Optional
from pymongo import UpdateOne
from common.mongo_db.mongo_base import gen_db_object
from app.mobile_web.config import logger


class ProductRepository:
    COLLECTION_NAME = "products"

    def __init__(self, db_instance=None):
        self._db = db_instance or gen_db_object()
        try:
            self.col.create_index([("platform", 1), ("product_id", 1)], background=True)
            self.col.create_index("goods_sign", background=True)
        except Exception:
            pass

    @property
    def col(self):
        return self._db.get_collection(self.COLLECTION_NAME)

    def query_boom_products(
        self,
        platform: str = "all",
        category: Optional[str] = None,
        keyword: Optional[str] = None,
        sort_by: str = "latest",
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        分页查询爆单库商品
        :param platform: 平台代码 (all, pdd, taobao, jd)
        :param category: 商品类目/Tab
        :param keyword: 标题或品牌模糊搜索
        :param sort_by: latest(最新) | save_desc(省最多) | price_asc(价格低) | price_desc(价格高)
        :param page: 页码
        :param page_size: 每页数量
        """
        page = max(1, int(page or 1))
        page_size = max(1, min(100, int(page_size or 20)))
        skip = (page - 1) * page_size

        filter_query: Dict[str, Any] = {}

        # 平台筛选
        if platform and platform.lower() != "all":
            filter_query["platform"] = platform.lower()

        # 分类筛选
        if category and category.strip() and category != "全部":
            filter_query["category"] = category.strip()

        # 关键词模糊搜索
        if keyword and keyword.strip():
            filter_query["name"] = {"$regex": keyword.strip(), "$options": "i"}

        # 排序规则
        sort_mapping = {
            "latest": [("updated_at", -1)],
            "save_desc": [("saved_price", -1), ("updated_at", -1)],
            "price_asc": [("activity_price", 1)],
            "price_desc": [("activity_price", -1)],
        }
        sort_spec = sort_mapping.get(sort_by, [("updated_at", -1)])

        try:
            total_count = self.col.count_documents(filter_query)
            cursor = self.col.find(filter_query).sort(sort_spec).skip(skip).limit(page_size)
            items = []
            for doc in cursor:
                items.append(self._normalize_doc_summary(doc))

            return {
                "status": "success",
                "total_count": total_count,
                "page": page,
                "page_size": page_size,
                "has_more": (skip + len(items)) < total_count,
                "data": items,
            }
        except Exception as e:
            logger.error(f"[ProductRepository.query_boom_products] 查询异常: {e}")
            return {
                "status": "error",
                "error_msg": str(e),
                "total_count": 0,
                "page": page,
                "page_size": page_size,
                "has_more": False,
                "data": [],
            }

    def _normalize_doc_summary(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """极简爆品卡片摘要对象：仅提取列表所需字段，大幅提升查询与网络传输速度"""
        activity_price = doc.get("activity_price") or doc.get("price") or 0.0
        original_price = doc.get("original_price") or activity_price
        saved_price = doc.get("saved_price") or max(0.0, round(original_price - activity_price, 2))

        # 真实返利佣金与比例计算（优先读取库内真实的 commission_amount 字段）
        commission = doc.get("commission_amount")
        if commission is None:
            commission = doc.get("commission")
        if commission is None:
            commission = doc.get("estimated_commission_yuan")

        act_price = float(activity_price or 0.0)
        rate = doc.get("promotion_rate") or doc.get("commission_rate")
        if rate is not None:
            r_val = float(rate)
            commission_rate = round(r_val / 10.0, 1) if r_val > 50 else round(r_val, 1)
        elif commission is not None and act_price > 0:
            commission_rate = round((float(commission) / act_price) * 100, 1)
        else:
            commission_rate = 10.0

        if commission is not None:
            commission_amount = round(float(commission), 2)
        else:
            commission_amount = round(act_price * (commission_rate / 100.0), 2)
            if commission_amount < 0.1 and act_price > 0:
                commission_amount = 0.50

        updated_at_dt = doc.get("updated_at")
        updated_at_str = updated_at_dt.strftime("%Y-%m-%d %H:%M") if hasattr(updated_at_dt, "strftime") else str(updated_at_dt or "")

        return {
            "platform": doc.get("platform", "pdd"),
            "platform_name": "拼多多" if doc.get("platform") == "pdd" else ("淘宝" if doc.get("platform") == "taobao" else "京东"),
            "product_id": str(doc.get("product_id", "")),
            "goods_id": str(doc.get("product_id", "")),
            "goods_sign": doc.get("goods_sign") or "",
            "name": doc.get("name", "未命名商品"),
            "brand": doc.get("brand") or "官方甄选",
            "category": doc.get("category") or "精选爆品",
            "price": round(float(activity_price), 2),
            "original_price": round(float(original_price), 2),
            "saved_price": round(float(saved_price), 2),
            "coupon_amount": round(float(saved_price), 2),
            "has_coupon": saved_price > 0,
            "commission_rate": commission_rate,
            "commission_amount": commission_amount,
            "sales_tip": doc.get("sales_tip") or "热销中",
            "image_url": doc.get("image_url") or "",
            "updated_at": updated_at_str,
        }

    def _normalize_doc(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """将 Mongo 原生商品文档转化为统一、规范且富含详情的对象"""
        import urllib.parse
        doc.pop("_id", None)
        activity_price = doc.get("activity_price") or doc.get("price") or 0.0
        original_price = doc.get("original_price") or activity_price
        saved_price = doc.get("saved_price") or max(0.0, round(original_price - activity_price, 2))

        # 提取轮播图片：优先读取已沉淀入库的高清多图列表
        gallery = []
        stored_carousel = doc.get("carousel_images")
        if stored_carousel and isinstance(stored_carousel, list):
            for u in stored_carousel:
                if u and isinstance(u, str) and u.startswith("http"):
                    gallery.append(u)

        main_img = doc.get("image_url")
        if main_img and main_img not in gallery:
            gallery.append(main_img)

        # 解析 product_url 中的 _oak_gallery
        purl = doc.get("product_url", "")
        if "_oak_gallery=" in purl:
            try:
                parsed = urllib.parse.urlparse(purl)
                qs = urllib.parse.parse_qs(parsed.query)
                for g_val in qs.get("_oak_gallery", []):
                    if g_val and g_val.startswith("http"):
                        gallery.append(g_val)
            except Exception:
                pass

        # 解析 sku_info 中的小图
        sku_list = doc.get("sku_info") or []
        if isinstance(sku_list, list):
            for sku in sku_list:
                if isinstance(sku, dict):
                    s_img = sku.get("小图地址") or sku.get("image_url") or ""
                    if s_img and s_img.startswith("http"):
                        gallery.append(s_img)

        # 去重保持顺序
        clean_gallery = list(dict.fromkeys(gallery))
        if not clean_gallery and main_img:
            clean_gallery = [main_img]

        # 真实返利佣金与比例计算（优先读取库内真实的 commission_amount 字段）
        commission = doc.get("commission_amount")
        if commission is None:
            commission = doc.get("commission")
        if commission is None:
            commission = doc.get("estimated_commission_yuan")

        act_price = float(activity_price or 0.0)
        rate = doc.get("promotion_rate") or doc.get("commission_rate")
        if rate is not None:
            r_val = float(rate)
            commission_rate = round(r_val / 10.0, 1) if r_val > 50 else round(r_val, 1)
        elif commission is not None and act_price > 0:
            commission_rate = round((float(commission) / act_price) * 100, 1)
        else:
            commission_rate = 10.0

        if commission is not None:
            commission_amount = round(float(commission), 2)
        else:
            commission_amount = round(act_price * (commission_rate / 100.0), 2)
            if commission_amount < 0.1 and act_price > 0:
                commission_amount = 0.50

        # 处理更新时间
        updated_at_dt = doc.get("updated_at")
        updated_at_str = updated_at_dt.strftime("%Y-%m-%d %H:%M") if hasattr(updated_at_dt, "strftime") else str(updated_at_dt or "")

        brand_name = doc.get("brand") or "官方甄选"
        category_name = doc.get("category") or "爆款精选"
        name = doc.get("name", "未命名商品")

        return {
            "platform": doc.get("platform", "pdd"),
            "platform_name": "拼多多" if doc.get("platform") == "pdd" else ("淘宝" if doc.get("platform") == "taobao" else "京东"),
            "product_id": str(doc.get("product_id", "")),
            "goods_id": str(doc.get("product_id", "")),
            "goods_sign": doc.get("goods_sign") or "",
            "name": name,
            "desc": doc.get("desc") or f"{name}，{brand_name}品牌好物推荐，全网低价直购，已享专属活动立减！",
            "brand": brand_name,
            "brand_name": brand_name,
            "category": category_name,
            "category_name": category_name,
            "price": round(float(activity_price), 2),
            "original_price": round(float(original_price), 2),
            "saved_price": round(float(saved_price), 2),
            "coupon_amount": round(float(saved_price), 2),
            "has_coupon": saved_price > 0,
            "commission_rate": commission_rate,
            "commission_amount": commission_amount,
            "sales_tip": doc.get("sales_tip") or "热销中",
            "image_url": main_img or "",
            "carousel_images": clean_gallery,
            "product_url": purl,
            "promotion_url": doc.get("promotion_url") or purl,
            "updated_at": updated_at_str,
            "detail_tags": ["官方正品", "退货包运费", "极速发货", "假一赔十"],
            "service_guarantees": [
                "🛡️ 正品保障 · 假一赔十",
                "📦 退货包运费 · 极速退款",
                "⚡ 48小时闪电极速发货",
                "💰 全网爆款官方活动补贴"
            ],
            "shop_ratings": {
                "desc_score": "4.8 (高)",
                "serv_score": "4.9 (高)",
                "lgst_score": "4.8 (高)"
            },
            "specs": [
                {"label": "商品品牌", "value": brand_name},
                {"label": "所属品类", "value": category_name},
                {"label": "发货承诺", "value": "48小时内极速出库"},
                {"label": "售后服务", "value": "支持7天无理由退换 (赠送运费险)"},
                {"label": "当前销量", "value": doc.get("sales_tip") or "超10万件热销"}
            ],
            "coupon_info": {
                "coupon_remain_quantity": 8900,
                "coupon_total_quantity": 10000,
                "valid_tip": "优惠券今日有效，先到先得"
            }
        }

    def get_product_detail(self, goods_id_or_sign: str) -> Optional[Dict[str, Any]]:
        """按 product_id 或 goods_sign 从数据库直接检索并返回规范化商品详情"""
        if not goods_id_or_sign:
            return None
        key = str(goods_id_or_sign).strip()
        conds = [{"product_id": key}, {"goods_sign": key}]
        if key.isdigit():
            conds.append({"product_id": int(key)})
        try:
            doc = self.col.find_one({"$or": conds})
            if doc:
                return self._normalize_doc(doc)
        except Exception as e:
            logger.warning(f"从 Mongo 反查商品详情失败: {e}")
        return None

    def get_categories(self, platform: str = "all") -> List[str]:
        """获取已有商品的类目列表，用于分类快速导航"""
        filter_query = {}
        if platform and platform.lower() != "all":
            filter_query["platform"] = platform.lower()
        try:
            cats = self.col.distinct("category", filter_query)
            return [c for c in cats if c and str(c).strip()]
        except Exception as e:
            logger.warning(f"获取类目失败: {e}")
            return ["全部", "食品", "美妆", "百货", "数码", "服饰"]

    def upsert_home_products(
        self,
        goods_list: List[Dict[str, Any]],
        channel_type: Optional[int] = None
    ) -> int:
        """
        批量更新/插入首页官方推荐流商品到 MongoDB products 集合
        每次用户拉取推荐商品时自动执行，确保商品最新价格、优惠与销量持久化
        """
        if not goods_list or not isinstance(goods_list, list):
            return 0

        now = datetime.datetime.now()
        operations = []

        for item in goods_list:
            goods_sign = str(item.get("goods_sign") or "").strip()
            goods_id = str(item.get("goods_id") or item.get("product_id") or "").strip()
            if not goods_sign and not goods_id:
                continue

            platform = item.get("platform") or "pdd"
            # 严格对齐 MongoDB 集合已有的 (platform, product_id) 唯一索引
            if goods_id:
                filter_spec = {"platform": platform, "product_id": goods_id}
            else:
                filter_spec = {"platform": platform, "goods_sign": goods_sign}

            price = float(item.get("price") or item.get("min_group_price_yuan") or 0.0)
            orig_price = float(item.get("original_price") or price)
            coupon = float(item.get("coupon_amount") or item.get("coupon_discount_yuan") or 0.0)
            commission = float(item.get("commission_amount") or item.get("estimated_commission_yuan") or 0.0)

            update_data = {
                "name": item.get("name") or item.get("goods_name") or "",
                "platform": platform,
                "goods_sign": goods_sign,
                "product_id": goods_id,
                "price": orig_price,
                "activity_price": price,
                "original_price": orig_price,
                "saved_price": coupon,
                "has_coupon": coupon > 0,
                "coupon_amount": coupon,
                "commission_amount": commission,
                "sales_tip": str(item.get("sales_tip") or "0"),
                "image_url": item.get("image_url") or item.get("goods_image_url") or item.get("goods_thumbnail_url") or "",
                "brand": item.get("brand_name") or item.get("brand") or "官方优选",
                "category": item.get("category_name") or item.get("category") or "精选好物",
                "updated_at": now,
                "source": "pdd_recommend_stream",
            }
            if channel_type is not None:
                update_data["channel_type"] = channel_type

            # 构建原子操作
            operations.append(
                UpdateOne(filter_spec, {"$set": update_data, "$setOnInsert": {"created_at": now}}, upsert=True)
            )

        if not operations:
            return 0

        try:
            result = self.col.bulk_write(operations, ordered=False)
            upserted_count = result.upserted_count + result.modified_count
            logger.info(f"[ProductRepository.upsert_home_products] 成功入库/更新 {upserted_count} 件首页推荐商品")
            return upserted_count
        except Exception as e:
            logger.warning(f"[ProductRepository.upsert_home_products] 批量入库异常: {e}")
            return 0

    def upsert_product_detail(self, detail: Dict[str, Any]) -> bool:
        """将完整深度商品详情写入或更新到数据库，用于后续详情反查秒开"""
        if not detail or not isinstance(detail, dict):
            return False
        goods_sign = str(detail.get("goods_sign") or "").strip()
        goods_id = str(detail.get("goods_id") or detail.get("product_id") or "").strip()
        if not goods_sign and not goods_id:
            return False

        platform = detail.get("platform") or "pdd"
        if goods_id:
            filter_spec = {"platform": platform, "product_id": goods_id}
        else:
            filter_spec = {"platform": platform, "goods_sign": goods_sign}
        now = datetime.datetime.now()

        update_doc = {
            "name": detail.get("name") or "",
            "desc": detail.get("desc") or "",
            "platform": detail.get("platform") or "pdd",
            "goods_sign": goods_sign,
            "product_id": goods_id,
            "price": float(detail.get("original_price") or detail.get("price") or 0.0),
            "activity_price": float(detail.get("price") or 0.0),
            "original_price": float(detail.get("original_price") or detail.get("price") or 0.0),
            "saved_price": float(detail.get("coupon_amount") or 0.0),
            "coupon_amount": float(detail.get("coupon_amount") or 0.0),
            "has_coupon": bool(detail.get("has_coupon")),
            "commission_amount": float(detail.get("commission_amount") or 0.0),
            "sales_tip": str(detail.get("sales_tip") or "0"),
            "image_url": detail.get("image_url") or "",
            "carousel_images": detail.get("carousel_images") or [],
            "brand": detail.get("brand_name") or detail.get("brand") or "官方优选",
            "category": detail.get("category_name") or detail.get("category") or "精选好物",
            "mall_name": detail.get("mall_name") or "品牌官方店",
            "specs": detail.get("specs") or [],
            "service_guarantees": detail.get("service_guarantees") or [],
            "shop_ratings": detail.get("shop_ratings") or {},
            "coupon_info": detail.get("coupon_info") or {},
            "updated_at": now,
        }

        try:
            self.col.update_one(filter_spec, {"$set": update_doc, "$setOnInsert": {"created_at": now}}, upsert=True)
            return True
        except Exception as e:
            logger.warning(f"[ProductRepository.upsert_product_detail] 详情入库异常: {e}")
            return False

    def get_promotion_link_cache(
        self,
        platform: str,
        goods_id: Optional[str] = None,
        goods_sign: Optional[str] = None,
        goods_key: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        从 products 集合中直接读取站长官方推广链接与抢购口令。
        无需额外建立新表，直接复用已有的商品主表数据，0ms 瞬间响应。
        """
        filter_spec = None
        # 优先使用唯一的 goods_id (product_id) 检索
        gid = goods_id or (goods_key if goods_key and str(goods_key).isdigit() else None)
        gsign = goods_sign or (goods_key if goods_key and not str(goods_key).isdigit() else None)

        if gid and str(gid).isdigit():
            filter_spec = {"platform": platform, "product_id": str(gid)}
        elif gsign:
            filter_spec = {"platform": platform, "goods_sign": str(gsign)}

        if not filter_spec:
            return None

        try:
            doc = self.col.find_one(filter_spec)
            if doc:
                promo_data = doc.get("default_promotion")
                if isinstance(promo_data, dict) and promo_data.get("final_link"):
                    # 检查缓存有效期（默认 3 天）
                    promo_ts = doc.get("default_promotion_updated_at")
                    if promo_ts:
                        try:
                            import time
                            doc_ts = promo_ts.timestamp() if hasattr(promo_ts, "timestamp") else 0
                            if (time.time() - doc_ts) > 3 * 86400:
                                return None
                        except Exception:
                            pass
                    result = dict(promo_data)
                    result["from_cache"] = True
                    return result

                # 兼容底层 products 表原生已存储的 promotion_url
                purl = doc.get("promotion_url")
                if purl and isinstance(purl, str) and ("p.pinduoduo.com" in purl or "yangkeduo.com" in purl or purl.startswith("http")):
                    name = doc.get("name") or "精选高性价比好物"
                    price = doc.get("activity_price") or doc.get("price")
                    coupon = doc.get("coupon_amount") or doc.get("saved_price")
                    cmd_lines = [f"【好物精选】{name}"]
                    if price and float(price) > 0:
                        p_info = f"【券后特惠】¥{price}"
                        if coupon and float(coupon) > 0:
                            p_info += f"（已立减 ¥{coupon}）"
                        cmd_lines.append(p_info)
                    cmd_lines.append("【抢购口令】长按复制本段全部信息，打开【拼多多APP】立即领券下单！")
                    cmd_lines.append(f"{purl}")
                    return {
                        "status": "success",
                        "need_auth": False,
                        "link_type": "官方专属转链",
                        "final_link": purl,
                        "url": purl,
                        "short_url": purl,
                        "mobile_url": purl,
                        "mobile_short_url": purl,
                        "command_text": "\n".join(cmd_lines),
                        "we_app_info": {},
                        "from_cache": True,
                    }
        except Exception as e:
            logger.warning(f"读取 products 表官方推广缓存异常: {e}")
        return None

    def save_promotion_link_cache(
        self,
        platform: str,
        data: Dict[str, Any],
        goods_id: Optional[str] = None,
        goods_sign: Optional[str] = None,
        goods_key: Optional[str] = None
    ) -> bool:
        """
        持久化保存未登录用户（站长官方位）的转链结果到 products 商品主表中。
        拼多多平台严格以唯一的 goods_id (product_id) 为核心主键。
        """
        if not data or not isinstance(data, dict):
            return False

        now = datetime.datetime.now()
        cache_data = dict(data)
        cache_data.pop("from_cache", None)

        gid = goods_id or (goods_key if goods_key and str(goods_key).isdigit() else None)
        gsign = goods_sign or (goods_key if goods_key and not str(goods_key).isdigit() else None)

        filter_spec = None
        if gid and str(gid).isdigit():
            filter_spec = {"platform": platform, "product_id": str(gid)}
        elif gsign:
            filter_spec = {"platform": platform, "goods_sign": str(gsign)}

        if not filter_spec:
            return False

        final_link = (
            cache_data.get("final_link")
            or cache_data.get("mobile_short_url")
            or cache_data.get("short_url")
            or cache_data.get("url")
            or ""
        )

        update_set = {
            "default_promotion": cache_data,
            "default_promotion_updated_at": now,
        }
        if final_link:
            update_set["promotion_url"] = final_link
            update_set["promotion_status"] = "success"
            update_set["promotion_updated_at"] = now
        if gsign:
            update_set["goods_sign"] = str(gsign)
        if gid:
            update_set["product_id"] = str(gid)
        update_set["platform"] = platform

        try:
            self.col.update_one(
                filter_spec,
                {"$set": update_set, "$setOnInsert": {"created_at": now}},
                upsert=True
            )
            return True
        except Exception as e:
            logger.warning(f"保存 products 表官方推广缓存异常: {e}")
            return False

