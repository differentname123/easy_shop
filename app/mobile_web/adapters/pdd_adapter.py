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

        return {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "goods_id": str(raw_cleaned.get("goods_id", "")),
            "goods_sign": raw_cleaned.get("goods_sign", ""),
            "name": raw_cleaned.get("goods_name", ""),
            "desc": raw_cleaned.get("goods_desc") or f"{raw_cleaned.get('goods_name', '')}，官方优选好物爆款",
            "category_name": raw_cleaned.get("category_name", ""),
            "brand_name": raw_cleaned.get("brand_name", ""),
            "image_url": raw_cleaned.get("goods_image_url") or raw_cleaned.get("goods_thumbnail_url", ""),
            "carousel_images": gallery if gallery else [raw_cleaned.get("goods_image_url", "")],
            "price": final_price,
            "original_price": price,
            "coupon_amount": coupon_discount,
            "has_coupon": raw_cleaned.get("has_coupon", False),
            "commission_rate": round(raw_cleaned.get("promotion_rate", 0) / 10.0, 1),
            "commission_amount": raw_cleaned.get("estimated_commission_yuan", 0.0),
            "sales_tip": raw_cleaned.get("sales_tip", "0"),
            "mall_name": raw_cleaned.get("mall_name", "品牌官方店"),
            "search_id": raw_cleaned.get("search_id", ""),
            "detail_tags": [t for t in ["官方补贴", "退货包运费", "极速发货"] if raw_cleaned.get("has_coupon") or price > 20],
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
                    normalized_list.append(self._normalize_goods(cleaned))

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
            custom_params = make_custom_parameters(uid=uid) if uid else None
            params = {
                "keyword": keyword,
                "pid": self.pid,
                "page": page,
                "page_size": page_size,
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
                    normalized_list.append(self._normalize_goods(cleaned))

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
        try:
            res = generate_smart_promotion_url(
                client_id=self.client_id,
                client_secret=self.client_secret,
                pid=self.pid,
                goods_sign=goods_id_or_sign,
                uid=uid,
                **kwargs
            )
            if res.get("status") == "success":
                data = res.get("data", {})
                # 统一取最优的短链作为权威链接
                final_link = (
                    data.get("mobile_short_url")
                    or data.get("short_url")
                    or data.get("mobile_url")
                    or data.get("url", "")
                )
                return {
                    "status": "success",
                    "need_auth": False,
                    "link_type": res.get("link_type", "专属推广链接"),
                    "final_link": final_link,
                    "url": final_link,
                    "short_url": final_link,
                    "mobile_url": final_link,
                    "mobile_short_url": final_link,
                    "we_app_info": data.get("we_app_info", {}),
                    "raw": data,
                }

            error_msg = res.get("error_msg", "")
            if "60001" in error_msg or "授权备案" in error_msg:
                return self._handle_unauthorized_conversion(goods_id_or_sign, uid, **kwargs)
            return {"status": "error", "error_msg": error_msg or "转链失败"}
        except Exception as e:
            err_str = str(e)
            if "60001" in err_str or "授权备案" in err_str:
                return self._handle_unauthorized_conversion(goods_id_or_sign, uid, **kwargs)
            logger.error(f"[PddAdapter.convert_promotion_url] 异常: {err_str}")
            return {"status": "error", "error_msg": err_str}

    def _handle_unauthorized_conversion(self, goods_sign: str, uid: Optional[str], **kwargs) -> Dict[str, Any]:
        """未备案降级处理：生成推手专属备案授权链接与统一兜底链接"""
        auth_info = self.generate_authority_url(uid=uid) if uid else {}
        auth_url = auth_info.get("auth_url") or auth_info.get("short_url", "")

        fallback_res = generate_goods_promotion_url(
            client_id=self.client_id,
            client_secret=self.client_secret,
            pid=self.pid,
            goods_sign=goods_sign,
            uid=None,
            **kwargs
        )
        fb_data = fallback_res.get("data", {}) if fallback_res.get("status") == "success" else {}

        final_fallback_link = (
            fb_data.get("mobile_short_url")
            or fb_data.get("short_url")
            or fb_data.get("mobile_url")
            or fb_data.get("url")
            or f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_sign}"
        )

        return {
            "status": "success",
            "need_auth": True,
            "link_type": "未备案引导授权",
            "auth_url": auth_url,
            "final_link": final_fallback_link,
            "url": final_fallback_link,
            "short_url": final_fallback_link,
            "mobile_url": final_fallback_link,
            "mobile_short_url": final_fallback_link,
            "msg": f"推手 [{uid}] 尚未在拼多多完成渠道授权备案，请先点击授权以保障返利归属",
            "we_app_info": fb_data.get("we_app_info", {}),
            "raw": fb_data,
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
