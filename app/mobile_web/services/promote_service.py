# -*- coding: utf-8 -*-
"""
推广转链与佣金归因服务
统一调度适配器生链、口令构建与多级缓存，严格符合开闭原则。
"""
import re
from typing import Dict, Any, Optional, Tuple
from app.mobile_web.adapters import get_adapter
from app.mobile_web.repositories.product_repo import ProductRepository
from app.mobile_web.cache import PROMOTION_CACHE
from common.common_utils import setup_logger

logger = setup_logger(app_name="mobile_web")
_UID_PATTERN = re.compile(r"^u_[0-9a-f]{8}$")

# 向后兼容别名
_PROMOTION_CACHE = PROMOTION_CACHE


class PromoteService:
    def __init__(self, product_repo: Optional[ProductRepository] = None):
        self.repo = product_repo or ProductRepository()

    @staticmethod
    def build_command_text(
        title: str,
        final_link: str,
        price: Optional[float] = None,
        coupon: Optional[float] = None
    ) -> str:
        """DRY: 统一生成抢购口令文本"""
        cmd_lines = [f"【好物精选】{title or '精选高性价比好物'}"]
        if price is not None and float(price) > 0:
            p_info = f"【券后特惠】¥{price}"
            if coupon and float(coupon) > 0:
                p_info += f"（已立减 ¥{coupon}）"
            cmd_lines.append(p_info)
        cmd_lines.append("【抢购口令】长按复制本段全部信息，打开【拼多多APP】立即领券下单！")
        cmd_lines.append(f"{final_link}")
        return "\n".join(cmd_lines)

    def _resolve_goods_id_and_sign(
        self, goods_id_or_sign: str, kwargs: dict
    ) -> Tuple[Optional[str], Optional[str]]:
        """在业务服务层将 goods_id (数字) 与 goods_sign 反查解析齐备，绝不下推给外部适配器"""
        gid = kwargs.get("goods_id")
        gsign = kwargs.get("goods_sign")

        if goods_id_or_sign:
            if str(goods_id_or_sign).isdigit():
                if not gid:
                    gid = str(goods_id_or_sign)
            else:
                if not gsign:
                    gsign = str(goods_id_or_sign)

        # 若只有 goods_sign 但没有数字 goods_id，从商品主表反查
        if not gid and gsign:
            try:
                doc = self.repo.get_product_detail(gsign)
                if doc and doc.get("product_id") and str(doc.get("product_id")).isdigit():
                    gid = str(doc.get("product_id"))
            except Exception:
                pass

        return gid, gsign

    def convert_link(
        self,
        platform: str = "pdd",
        goods_id_or_sign: str = "",
        product_url: Optional[str] = None,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        统一转链生成接口
        :param platform: 平台 (pdd, taobao, jd)
        :param goods_id_or_sign: 商品标识 (goods_sign 或商品 ID)
        :param product_url: 商品原始链接
        :param uid: 推手唯一识别码 (custom_parameters)
        """
        plat = (platform or "pdd").lower()
        adapter = get_adapter(plat)
        cache_key = str(goods_id_or_sign or product_url or "").strip()

        # 校验推手 uid 格式，非法格式重置为 None
        clean_uid = str(uid).strip() if (uid and isinstance(uid, str)) else None
        if clean_uid and not _UID_PATTERN.match(clean_uid):
            clean_uid = None

        req_goods_id, req_goods_sign = self._resolve_goods_id_and_sign(goods_id_or_sign, kwargs)

        # 1. 优先查推手专属转链内存缓存 (30分钟瞬间返回)
        user_cache_key = f"{plat}_{req_goods_id or cache_key}_{clean_uid or 'default'}"
        cached_user_res = PROMOTION_CACHE.get(user_cache_key)
        if cached_user_res is not None:
            logger.debug(f"[转链内存缓存命中] user_cache_key={user_cache_key}")
            return cached_user_res

        # 2. 未登录用户（站长官方位）优先查 products 表持久化缓存
        if not clean_uid:
            cached_data = self.repo.get_promotion_link_cache(
                platform=plat,
                goods_id=req_goods_id,
                goods_sign=req_goods_sign,
                goods_key=cache_key
            )
            if cached_data:
                logger.info(f"[转链主表缓存命中] products 表命中官方推广位: goods_id={req_goods_id}")
                PROMOTION_CACHE.set(user_cache_key, cached_data)
                return cached_data

        # 3. 统一调用适配器生链 (OCP 核心契约)
        title = kwargs.get("title") or kwargs.get("name") or "精选高性价比好物"
        price = kwargs.get("price")
        coupon = kwargs.get("coupon_amount") or kwargs.get("coupon")

        target_key = goods_id_or_sign or (req_goods_id if req_goods_id else "")
        adapter_kwargs = {k: v for k, v in kwargs.items() if k not in ("goods_id", "goods_sign")}

        res = adapter.convert_promotion_url(
            goods_id_or_sign=target_key,
            uid=clean_uid,
            goods_id=req_goods_id,
            goods_sign=req_goods_sign,
            product_url=product_url,
            **adapter_kwargs
        )

        if res.get("status") == "success":
            final_link = (
                res.get("final_link")
                or res.get("mobile_short_url")
                or res.get("short_url")
                or res.get("url", "")
            )
            res["final_link"] = final_link
            if not res.get("command_text"):
                res["command_text"] = self.build_command_text(title, final_link, price, coupon)

            is_fallback = bool(res.get("is_fallback"))
            PROMOTION_CACHE.set(user_cache_key, res)

            # 仅当非降级保底链接且为站长位时，持久化写入 products 表
            if not clean_uid and not is_fallback:
                self.repo.save_promotion_link_cache(
                    platform=plat,
                    data=res,
                    goods_id=req_goods_id,
                    goods_sign=req_goods_sign,
                    goods_key=cache_key
                )

            return res

        return {
            "status": "error",
            "error_msg": res.get("error_msg", "未能生成有效推广转链，商品可能已失效或缺少必要标识")
        }
