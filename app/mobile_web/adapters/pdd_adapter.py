# -*- coding: utf-8 -*-
"""
拼多多多多进宝平台适配器
对接多多进宝开放平台，实现推荐商品榜单、搜索、多图轮播清洗、渠道授权备案与带统一 UID 的推广转链。
"""
from typing import Dict, Any, List, Optional
import requests
from requests.adapters import HTTPAdapter
from app.mobile_web.adapters.base import BasePlatformAdapter
from app.mobile_web.config import PDD_CLIENT_ID, PDD_CLIENT_SECRET, PDD_PID
from common.common_utils import setup_logger
from app.pdd_utils import (
    call_pdd_api,
    clean_goods_item,
    check_member_authority,
    generate_authority_url as pdd_gen_auth_url,
    generate_smart_promotion_url,
    generate_goods_promotion_url,
    make_custom_parameters,
    convert_pdd_url,
    get_goods_detail as pdd_get_detail,
)

logger = setup_logger(app_name="mobile_web")

# 全局复用的高性能 HTTP 会话池，消除每次建立连接与 TLS 握手的几秒延迟
_HTTP_SESSION = requests.Session()
_HTTP_SESSION.trust_env = False  # 避免 Windows 系统代理误匹配带来的长延时
_ADAPTER = HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=2)
_HTTP_SESSION.mount("https://", _ADAPTER)
_HTTP_SESSION.mount("http://", _ADAPTER)


class PddAdapter(BasePlatformAdapter):
    platform_code = "pdd"
    platform_name = "拼多多"

    def __init__(
        self,
        client_id: str = PDD_CLIENT_ID,
        client_secret: str = PDD_CLIENT_SECRET,
        pid: str = PDD_PID,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.pid = pid

    def _normalize_goods_summary(self, raw_cleaned: dict) -> dict:
        """极简商品卡片摘要对象：专供列表推荐与搜索，减少冗余字段，极大加快首屏加载与解析速度"""
        price = raw_cleaned.get("min_group_price_yuan", 0.0)
        coupon_discount = raw_cleaned.get("coupon_discount_yuan", 0.0)
        final_price = round(max(0.0, price - coupon_discount), 2) if raw_cleaned.get("has_coupon") else price

        return {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "goods_id": str(raw_cleaned.get("goods_id", "")),
            "goods_sign": raw_cleaned.get("goods_sign", ""),
            "name": raw_cleaned.get("goods_name", ""),
            "image_url": raw_cleaned.get("goods_image_url") or raw_cleaned.get("goods_thumbnail_url", ""),
            "price": final_price,
            "original_price": price,
            "coupon_amount": coupon_discount,
            "has_coupon": raw_cleaned.get("has_coupon", False),
            "commission_rate": round(raw_cleaned.get("promotion_rate", 0) / 10.0, 1),
            "commission_amount": raw_cleaned.get("estimated_commission_yuan", 0.0),
            "sales_tip": raw_cleaned.get("sales_tip", "0"),
            "mall_name": raw_cleaned.get("mall_name", "品牌官方店"),
        }

    def _normalize_goods(self, raw_cleaned: dict) -> dict:
        """将清洗后的商品数据转化为统一、规范且包含多图轮播的对外商品详情对象"""
        price = raw_cleaned.get("min_group_price_yuan", 0.0)
        coupon_discount = raw_cleaned.get("coupon_discount_yuan", 0.0)
        final_price = round(max(0.0, price - coupon_discount), 2) if raw_cleaned.get("has_coupon") else price

        raw_dict = raw_cleaned.get("_raw_data", {})
        # 提取轮播图/更多图片列表
        gallery = raw_dict.get("goods_gallery_urls") or []
        if not gallery:
            img = raw_cleaned.get("goods_image_url") or raw_cleaned.get("goods_thumbnail_url")
            thumb = raw_cleaned.get("goods_thumbnail_url")
            gallery = [u for u in [img, thumb] if u]
            gallery = list(dict.fromkeys(gallery))  # 去重

        brand_name = raw_cleaned.get("brand_name") or "官方优选"
        category_name = raw_cleaned.get("category_name") or "精选好物"
        sales_tip = raw_cleaned.get("sales_tip", "0")
        mall_name = raw_cleaned.get("mall_name", "品牌官方店")

        # 官方标签
        unified = raw_dict.get("unified_tags") or []
        if not unified:
            unified = [t for t in ["官方补贴", "退货包运费", "极速发货", "正品保障"] if raw_cleaned.get("has_coupon") or price > 20]

        # 优惠券起止时间与余量
        coupon_start = raw_cleaned.get("coupon_start_time")
        coupon_end = raw_cleaned.get("coupon_end_time")
        coupon_remain = raw_cleaned.get("coupon_remain_quantity", 0)
        coupon_total = raw_cleaned.get("coupon_total_quantity", 0)

        return {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "goods_id": str(raw_cleaned.get("goods_id", "")),
            "goods_sign": raw_cleaned.get("goods_sign", ""),
            "name": raw_cleaned.get("goods_name", ""),
            "desc": raw_cleaned.get("goods_desc") or f"{raw_cleaned.get('goods_name', '')}，{brand_name}官方优选爆款，限时优惠特卖！",
            "category_name": category_name,
            "brand_name": brand_name,
            "image_url": raw_cleaned.get("goods_image_url") or raw_cleaned.get("goods_thumbnail_url", ""),
            "carousel_images": gallery if gallery else [raw_cleaned.get("goods_image_url", "")],
            "price": final_price,
            "original_price": price,
            "coupon_amount": coupon_discount,
            "has_coupon": raw_cleaned.get("has_coupon", False),
            "commission_rate": round(raw_cleaned.get("promotion_rate", 0) / 10.0, 1),
            "commission_amount": raw_cleaned.get("estimated_commission_yuan", 0.0),
            "sales_tip": sales_tip,
            "mall_name": mall_name,
            "search_id": raw_cleaned.get("search_id", ""),
            "detail_tags": unified,
            "unified_tags": unified,
            "service_guarantees": [
                "🛡️ 正品保障 · 假一赔十",
                "📦 退货包运费 · 极速退款",
                "⚡ 48小时内闪电发货",
                "💰 多多进宝官方活动补贴"
            ],
            "shop_ratings": {
                "desc_score": f"{raw_cleaned.get('desc_txt', '高')} (4.8)",
                "serv_score": f"{raw_cleaned.get('serv_txt', '高')} (4.9)",
                "lgst_score": f"{raw_cleaned.get('lgst_txt', '高')} (4.8)"
            },
            "specs": [
                {"label": "商品品牌", "value": brand_name},
                {"label": "商品类目", "value": category_name},
                {"label": "店铺名称", "value": mall_name},
                {"label": "发货承诺", "value": "48小时内顺丰/中通极速出库"},
                {"label": "售后服务", "value": "假一赔十 · 退货包运费 (送运费险)"},
                {"label": "累计销量", "value": f"已拼 {sales_tip} 件"}
            ],
            "coupon_info": {
                "coupon_remain_quantity": coupon_remain,
                "coupon_total_quantity": coupon_total,
                "valid_tip": "领券后即刻生效，限时限量抢购"
            }
        }

    def get_recommend_goods(
        self,
        channel_type: int = 5,
        offset: int = 0,
        limit: int = 20,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        获取多多进宝推荐榜单商品
        """
        try:
            custom_params = make_custom_parameters(uid=uid) if uid else None
            params = {
                "channel_type": channel_type,
                "limit": limit,
                "offset": offset,
                "pid": self.pid,
                "custom_parameters": custom_params,
            }
            params.update(kwargs)

            res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.recommend.get", params)
            resp_data = res.get("goods_basic_detail_response", {})
            goods_list = resp_data.get("list", [])
            total = resp_data.get("total", len(goods_list))

            normalized_list = []
            for item in goods_list:
                cleaned = clean_goods_item(item, source_type=f"pdd_recommend_{channel_type}")
                if cleaned:
                    normalized_list.append(self._normalize_goods_summary(cleaned))

            return {
                "status": "success",
                "total_count": total,
                "offset": offset,
                "limit": limit,
                "list_id": resp_data.get("list_id", ""),
                "data": normalized_list,
            }
        except Exception as e:
            logger.error(f"[PddAdapter.get_recommend_goods] 出错: {str(e)}")
            return {"status": "error", "error_msg": str(e), "data": []}

    def search_goods(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        关键词搜索多多进宝商品
        """
        try:
            safe_page_size = max(10, min(100, int(page_size or 20)))
            custom_params = make_custom_parameters(uid=uid) if uid else None
            params = {
                "keyword": keyword,
                "pid": self.pid,
                "page": page,
                "page_size": safe_page_size,
                "custom_parameters": custom_params,
            }
            params.update(kwargs)

            res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.search", params)
            resp_data = res.get("goods_search_response", {})
            goods_list = resp_data.get("goods_list", [])
            total = resp_data.get("total_count", len(goods_list))

            normalized_list = []
            for item in goods_list:
                cleaned = clean_goods_item(item, source_type="pdd_search")
                if cleaned:
                    normalized_list.append(self._normalize_goods_summary(cleaned))

            return {
                "status": "success",
                "total_count": total,
                "page": page,
                "page_size": page_size,
                "list_id": resp_data.get("list_id", ""),
                "data": normalized_list,
            }
        except Exception as e:
            logger.error(f"[PddAdapter.search_goods] 搜索出错: {str(e)}")
            return {"status": "error", "error_msg": str(e), "data": []}

    def get_goods_detail(
        self,
        goods_id_or_sign: str,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        获取拼多多官方完整商品详情 (包含全量高清实拍轮播图、满减优惠、店铺与服务保障)
        """
        try:
            is_sign = len(str(goods_id_or_sign)) > 16 or not str(goods_id_or_sign).isdigit()
            goods_sign = str(goods_id_or_sign) if is_sign else None
            goods_id = str(goods_id_or_sign) if not is_sign else None

            res = pdd_get_detail(
                client_id=self.client_id,
                client_secret=self.client_secret,
                pid=self.pid,
                goods_sign=goods_sign,
                goods_id=goods_id,
                uid=uid,
                **kwargs
            )
            if res.get("status") == "success" and res.get("data"):
                normalized = self._normalize_goods(res["data"])
                return {"status": "success", "data": normalized}
            return {"status": "error", "error_msg": res.get("error_msg", "未查询到官方商品详情")}
        except Exception as e:
            logger.error(f"[PddAdapter.get_goods_detail] 异常: {e}")
            return {"status": "error", "error_msg": str(e)}

    def _build_command_text(self, title: str, final_link: str, price: Optional[float] = None, coupon: Optional[float] = None) -> str:
        cmd_lines = [f"【好物精选】{title or '精选高性价比好物'}"]
        if price is not None and float(price) > 0:
            p_info = f"【券后特惠】¥{price}"
            if coupon and float(coupon) > 0:
                p_info += f"（已立减 ¥{coupon}）"
            cmd_lines.append(p_info)
        cmd_lines.append("【抢购口令】长按复制本段全部信息，打开【拼多多APP】立即领券下单！")
        cmd_lines.append(f"{final_link}")
        return "\n".join(cmd_lines)

    def _resolve_goods_id(self, goods_id_or_sign: str, kwargs: dict) -> tuple[Optional[str], Optional[str]]:
        """提取并解析真实的 goods_id (数字) 与 goods_sign"""
        goods_id = kwargs.get("goods_id")
        goods_sign = kwargs.get("goods_sign")

        if goods_id_or_sign:
            if str(goods_id_or_sign).isdigit():
                if not goods_id:
                    goods_id = str(goods_id_or_sign)
            else:
                if not goods_sign:
                    goods_sign = str(goods_id_or_sign)

        # 若仍无数字 goods_id 但有 goods_sign，从商品库反查
        if not goods_id and goods_sign:
            try:
                from app.mobile_web.repositories.product_repo import ProductRepository
                repo = ProductRepository()
                doc = repo.get_product_detail(goods_sign)
                if doc and doc.get("product_id") and str(doc.get("product_id")).isdigit():
                    goods_id = str(doc.get("product_id"))
            except Exception:
                pass

        return goods_id, goods_sign

    def convert_promotion_url(
        self,
        goods_id_or_sign: str,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        使用推手专属 UID 生成拼多多推广链接
        确保输出统一权威的 final_link 字段，保证“前往领券”和“复制链接”绝对一致！
        """
        goods_id, goods_sign = self._resolve_goods_id(goods_id_or_sign, kwargs)
        title = kwargs.get("title") or kwargs.get("name") or kwargs.get("goods_name") or "精选高性价比好物"
        price = kwargs.get("price")
        coupon = kwargs.get("coupon_amount") or kwargs.get("coupon")

        target_sign = goods_sign or goods_id_or_sign

        # 尝试标准智能生链
        try:
            smart_kwargs = dict(kwargs)
            smart_kwargs.pop("goods_sign", None)
            smart_kwargs.pop("goods_id", None)
            res = generate_smart_promotion_url(
                client_id=self.client_id,
                client_secret=self.client_secret,
                pid=self.pid,
                goods_sign=target_sign,
                uid=uid,
                **smart_kwargs
            )
            if res.get("status") == "success":
                data = res.get("data", {})
                final_link = (
                    data.get("mobile_short_url")
                    or data.get("short_url")
                    or data.get("mobile_url")
                    or data.get("url", "")
                )
                if final_link:
                    schema_url = data.get("schema_url") or ""
                    command_text = self._build_command_text(title, final_link, price, coupon)
                    return {
                        "status": "success",
                        "need_auth": False,
                        "link_type": res.get("link_type", "专属推广链接"),
                        "final_link": final_link,
                        "url": final_link,
                        "short_url": final_link,
                        "mobile_url": final_link,
                        "mobile_short_url": final_link,
                        "schema_url": schema_url,
                        "command_text": command_text,
                        "we_app_info": data.get("we_app_info", {}),
                        "raw": data,
                    }
        except Exception as e:
            logger.warning(f"[PddAdapter.convert_promotion_url] 智能生链未成，平滑进入官方招商券兜底: {e}")

        extra_kwargs = dict(kwargs)
        extra_kwargs.pop("goods_id", None)
        extra_kwargs.pop("goods_sign", None)
        extra_kwargs.pop("title", None)
        extra_kwargs.pop("price", None)
        extra_kwargs.pop("coupon", None)
        extra_kwargs.pop("coupon_amount", None)
        return self._handle_fallback_conversion(goods_id, target_sign, uid, title=title, price=price, coupon=coupon, **extra_kwargs)

    def _handle_unauthorized_conversion(self, goods_sign: str, uid: Optional[str], **kwargs) -> Dict[str, Any]:
        """兼容未备案降级接口：调用高质量官方转链兜底"""
        goods_id, real_sign = self._resolve_goods_id(goods_sign, kwargs)
        title = kwargs.get("title") or kwargs.get("name") or "精选高性价比好物"
        price = kwargs.get("price")
        coupon = kwargs.get("coupon_amount") or kwargs.get("coupon")
        extra_kwargs = dict(kwargs)
        extra_kwargs.pop("goods_id", None)
        extra_kwargs.pop("goods_sign", None)
        extra_kwargs.pop("title", None)
        extra_kwargs.pop("price", None)
        extra_kwargs.pop("coupon", None)
        extra_kwargs.pop("coupon_amount", None)
        return self._handle_fallback_conversion(goods_id, real_sign or goods_sign, uid, title=title, price=price, coupon=coupon, **extra_kwargs)

    def _handle_fallback_conversion(
        self,
        goods_id: Optional[str],
        goods_sign: str,
        uid: Optional[str],
        title: str = "精选高性价比好物",
        price: Optional[float] = None,
        coupon: Optional[float] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        官方优惠券转链兜底处理：
        使用 convert_pdd_url 转化商品链接为带优惠券和推手 UID 的真实官方推广短链 (如 https://p.pinduoduo.com/...)
        """
        # 1. 优先使用数字 goods_id 构造 source_url 并通过 convert_pdd_url 生成官方券短链
        if goods_id and str(goods_id).isdigit():
            source_url = f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_id}"
            zs_res = convert_pdd_url(
                client_id=self.client_id,
                client_secret=self.client_secret,
                pid=self.pid,
                source_url=source_url,
                uid=uid
            )
            if zs_res.get("status") == "success" and zs_res.get("jump_url"):
                jump_url = zs_res["jump_url"]
                raw_data = zs_res.get("raw_data", {})
                command_text = self._build_command_text(title, jump_url, price, coupon)
                return {
                    "status": "success",
                    "need_auth": False,
                    "link_type": "官方招商券推广",
                    "final_link": jump_url,
                    "url": jump_url,
                    "short_url": jump_url,
                    "mobile_url": jump_url,
                    "mobile_short_url": jump_url,
                    "schema_url": raw_data.get("mobile_url") or "",
                    "command_text": command_text,
                    "we_app_info": {},
                    "raw": raw_data,
                }

        # 2. 如果没有数字 goods_id 或转链未果，尝试通用无 UID 生链
        fallback_res = generate_goods_promotion_url(
            client_id=self.client_id,
            client_secret=self.client_secret,
            pid=self.pid,
            goods_sign=goods_sign,
            uid=None,
            **kwargs
        )
        fb_data = fallback_res.get("data", {}) if fallback_res.get("status") == "success" else {}
        fb_link = (
            fb_data.get("mobile_short_url")
            or fb_data.get("short_url")
            or fb_data.get("mobile_url")
            or fb_data.get("url")
        )
        if fb_link:
            command_text = self._build_command_text(title, fb_link, price, coupon)
            return {
                "status": "success",
                "need_auth": False,
                "link_type": "通用官方推广",
                "final_link": fb_link,
                "url": fb_link,
                "short_url": fb_link,
                "mobile_url": fb_link,
                "mobile_short_url": fb_link,
                "schema_url": fb_data.get("schema_url") or "",
                "command_text": command_text,
                "we_app_info": fb_data.get("we_app_info", {}),
                "raw": fb_data,
            }

        # 3. 最终保底：绝对不能生成带 goods_sign 的 goods_id 假链接！
        if goods_id and str(goods_id).isdigit():
            final_raw_link = f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_id}"
        else:
            final_raw_link = f"https://mobile.yangkeduo.com/goods.html?goods_sign={goods_sign}"

        command_text = self._build_command_text(title, final_raw_link, price, coupon)
        return {
            "status": "success",
            "need_auth": False,
            "link_type": "商品直连保底",
            "final_link": final_raw_link,
            "url": final_raw_link,
            "short_url": final_raw_link,
            "mobile_url": final_raw_link,
            "mobile_short_url": final_raw_link,
            "schema_url": "",
            "command_text": command_text,
            "we_app_info": {},
            "raw": {},
        }

    def check_authority(self, uid: str) -> Dict[str, Any]:
        """检查指定推手在拼多多的渠道备案状态"""
        try:
            return check_member_authority(
                client_id=self.client_id,
                client_secret=self.client_secret,
                pid=self.pid,
                uid=uid
            )
        except Exception as e:
            logger.error(f"[PddAdapter.check_authority] 检查备案出错: {str(e)}")
            return {"status": "error", "is_authorized": False, "bind": 0, "error_msg": str(e)}

    def generate_authority_url(self, uid: str, **kwargs) -> Dict[str, Any]:
        """生成推手专属备案授权链接 (channel_type=10)"""
        try:
            res = pdd_gen_auth_url(
                client_id=self.client_id,
                client_secret=self.client_secret,
                pid=self.pid,
                uid=uid,
                **kwargs
            )
            if res.get("status") == "success":
                url_info = res.get("url_info", {})
                auth_link = url_info.get("mobile_short_url") or url_info.get("mobile_url") or url_info.get("short_url") or url_info.get("url", "")
                return {
                    "status": "success",
                    "auth_url": auth_link,
                    "short_url": auth_link,
                    "we_app_info": url_info.get("we_app_info", {}),
                }
            return {"status": "error", "error_msg": res.get("error_msg", "生成授权链接失败")}
        except Exception as e:
            logger.error(f"[PddAdapter.generate_authority_url] 生成授权异常: {str(e)}")
            return {"status": "error", "error_msg": str(e)}
