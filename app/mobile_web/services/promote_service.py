# -*- coding: utf-8 -*-
"""推广生链编排：平台解析身份，服务管理归因及有效结果缓存。"""
import re
from copy import deepcopy
from typing import Dict, Any, Optional
from app.mobile_web.adapters import get_adapter, PlatformUnavailableError
from app.mobile_web.adapters.base import build_command_text
from app.mobile_web.models.product import ProductRef, clean_product_id, normalize_platform
from app.mobile_web.repositories.product_repo import ProductRepository
from app.mobile_web.cache import PROMOTION_CACHE
from common.common_utils import setup_logger

logger = setup_logger(app_name="mobile_web")
_UID_PATTERN = re.compile(r"^u_[0-9a-f]{8}$")
_PROMOTION_CACHE = PROMOTION_CACHE


class PromoteService:
    def __init__(self, product_repo: Optional[ProductRepository] = None):
        self.repo = product_repo or ProductRepository()

    # 保留旧调用入口，但共享层不包含具体平台名称。
    build_command_text = staticmethod(build_command_text)

    def _resolve_product_ref(self, adapter, raw: str, kwargs: dict) -> ProductRef:
        ref = adapter.resolve_product_ref(
            raw, goods_id=kwargs.get("goods_id"), goods_sign=kwargs.get("goods_sign")
        )
        if ref.goods_sign:
            # Sign 是本次主标识。另一个客户端 ID 只有经同平台仓储确认后才可用作别名/兜底。
            ref = ProductRef(ref.platform, "", ref.goods_sign)
            try:
                doc = self.repo.get_product_detail(ref.goods_sign, platform=ref.platform, key_type="sign")
                if doc and normalize_platform(doc.get("platform", ref.platform)) == ref.platform:
                    goods_id = clean_product_id(doc.get("product_id")) or clean_product_id(doc.get("goods_id"))
                    ref = ProductRef(ref.platform, goods_id, ref.goods_sign)
            except Exception as exc:
                # 商品缓存不可用不妨碍平台直接按 Sign 生链。
                logger.warning(f"推广商品身份缓存读取失败: {exc}")
        return ref

    @staticmethod
    def _final_link(result: dict) -> str:
        for field in ("final_link", "mobile_short_url", "short_url", "mobile_url", "url"):
            value = result.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @classmethod
    def _cacheable(cls, result) -> bool:
        return (isinstance(result, dict) and result.get("status") == "success"
                and not result.get("is_fallback") and bool(cls._final_link(result)))

    def convert_link(
        self,
        platform: str = "pdd",
        goods_id_or_sign: str = "",
        product_url: Optional[str] = None,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        if not adapter.active:
            raise PlatformUnavailableError(f"{adapter.platform_name}暂未接入")

        clean_uid = uid.strip() if isinstance(uid, str) else ""
        if clean_uid and not _UID_PATTERN.fullmatch(clean_uid):
            clean_uid = ""
        ref = self._resolve_product_ref(adapter, goods_id_or_sign, kwargs)
        product_url = str(product_url or "").strip()
        if not ref.key and not product_url:
            return {"status": "error", "error_msg": "缺少有效商品标识或链接"}

        # 保留 ID/Sign 的类型和请求组合，不把未经平台验证的两个输入当成缓存别名。
        user_cache_key = ("promotion", plat, ref.goods_id, ref.goods_sign, product_url, clean_uid)
        cached = PROMOTION_CACHE.get(user_cache_key)
        if self._cacheable(cached):
            return deepcopy(cached)

        if not clean_uid and ref.key:
            cached = self.repo.get_promotion_link_cache(
                platform=plat, goods_id=ref.goods_id or None,
                goods_sign=ref.goods_sign or None, goods_key=ref.key or product_url
            )
            if self._cacheable(cached):
                PROMOTION_CACHE.set(user_cache_key, deepcopy(cached))
                return deepcopy(cached)

        adapter_kwargs = {k: v for k, v in kwargs.items() if k not in ("goods_id", "goods_sign")}
        result = adapter.convert_promotion_url(
            goods_id_or_sign=ref.key, uid=clean_uid or None,
            goods_id=ref.goods_id or None, goods_sign=ref.goods_sign or None,
            product_url=product_url or None, **adapter_kwargs
        )
        if not isinstance(result, dict) or result.get("status") != "success":
            message = result.get("error_msg") if isinstance(result, dict) else None
            return {"status": "error", "error_msg": message or "未能生成有效推广转链，商品可能已失效或缺少必要标识"}

        result = deepcopy(result)
        final_link = self._final_link(result)
        if not final_link:
            return {"status": "error", "error_msg": "平台未返回有效推广链接"}
        result["final_link"] = final_link
        if not result.get("command_text"):
            coupon = kwargs.get("coupon_amount")
            if coupon is None:
                coupon = kwargs.get("coupon")
            result["command_text"] = adapter.build_command_text(
                kwargs.get("title") or kwargs.get("name") or "精选高性价比好物",
                final_link, kwargs.get("price"), coupon
            )

        # 降级链接可本次返回，但不能阻挡下一次重试真实的归因推广链接。
        if not result.get("is_fallback"):
            PROMOTION_CACHE.set(user_cache_key, deepcopy(result))
            if not clean_uid and ref.key:
                self.repo.save_promotion_link_cache(
                    platform=plat, data=deepcopy(result), goods_id=ref.goods_id or None,
                    goods_sign=ref.goods_sign or None, goods_key=ref.key or product_url
                )
        return result
