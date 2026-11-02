# -*- coding: utf-8 -*-
"""
推广转链与佣金归因服务
确保用户在点击商品跳转购买时，自动带上当前推手的专属 uid，以便后续佣金准确归因。
统一输出 final_link 字段，确保跳转与复制链接 100% 绝对一致。
"""
import re
from typing import Dict, Any, Optional
from app.mobile_web.adapters import get_adapter
from app.mobile_web.repositories.product_repo import ProductRepository
from app.mobile_web.config import PDD_CLIENT_ID, PDD_CLIENT_SECRET, PDD_PID, CACHE_MAX_PROMOTION
from app.mobile_web.cache_utils import ThreadSafeTTLCache
from common.common_utils import setup_logger
from app.pdd_utils import convert_pdd_url

logger = setup_logger(app_name="mobile_web")

# 缓存推手个性化转链结果（有效 30 分钟），避免社群爆品裂变时高频轰炸外部 API
_PROMOTION_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_PROMOTION, ttl=1800)
_UID_PATTERN = re.compile(r"^u_[0-9a-f]{8}$")


class PromoteService:
    def __init__(self, product_repo: Optional[ProductRepository] = None):
        self.repo = product_repo or ProductRepository()

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
        :param goods_id_or_sign: 商品标识 (拼多多 goods_sign 或商品 ID)
        :param product_url: 商品原始链接
        :param uid: 当前推手唯一识别码 (custom_parameters)
        """
        plat = (platform or "pdd").lower()
        adapter = get_adapter(plat)
        cache_key = str(goods_id_or_sign or product_url or "").strip()

        # 校验推手 uid 格式，非法格式直接重置为 None 走站长默认位，防止非法字符穿透
        clean_uid = str(uid).strip() if (uid and isinstance(uid, str)) else None
        if clean_uid and not _UID_PATTERN.match(clean_uid):
            clean_uid = None

        # 解析真实唯一的 goods_id (数字) 与 goods_sign
        req_goods_id = kwargs.get("goods_id")
        req_goods_sign = kwargs.get("goods_sign")

        if goods_id_or_sign:
            if str(goods_id_or_sign).isdigit():
                if not req_goods_id:
                    req_goods_id = str(goods_id_or_sign)
            else:
                if not req_goods_sign:
                    req_goods_sign = str(goods_id_or_sign)

        # 若只有 goods_sign 但没有数字 goods_id，从商品主表反查
        if not req_goods_id and req_goods_sign:
            doc = self.repo.get_product_detail(req_goods_sign)
            if doc and doc.get("product_id") and str(doc.get("product_id")).isdigit():
                req_goods_id = str(doc.get("product_id"))

        # 1. 优先查推手专属转链内存缓存 (30分钟内瞬间返回)
        user_cache_key = f"{plat}_{req_goods_id or cache_key}_{clean_uid or 'default'}"
        cached_user_res = _PROMOTION_CACHE.get(user_cache_key)
        if cached_user_res is not None:
            logger.debug(f"[转链内存缓存命中] user_cache_key={user_cache_key}")
            return cached_user_res

        # 2. 如果是无登录状态的用户（使用站长自身推广位），优先查 products 表持久化缓存
        if not clean_uid:
            cached_data = self.repo.get_promotion_link_cache(
                platform=plat,
                goods_id=req_goods_id,
                goods_sign=req_goods_sign,
                goods_key=cache_key
            )
            if cached_data:
                logger.info(f"[转链主表缓存命中] products表命中官方推广位: goods_id={req_goods_id}")
                _PROMOTION_CACHE.set(user_cache_key, cached_data)
                return cached_data

        # 3. 优先调用平台适配器生链
        if goods_id_or_sign:
            adapter_kwargs = dict(kwargs)
            adapter_kwargs.pop("goods_id", None)
            adapter_kwargs.pop("goods_sign", None)
            res = adapter.convert_promotion_url(
                goods_id_or_sign,
                uid=clean_uid,
                goods_id=req_goods_id,
                goods_sign=req_goods_sign,
                **adapter_kwargs
            )
            if res.get("status") == "success":
                if not res.get("final_link"):
                    res["final_link"] = res.get("mobile_short_url") or res.get("short_url") or res.get("url", "")

                is_fallback = bool(res.get("is_fallback"))
                # 写入内存缓存
                _PROMOTION_CACHE.set(user_cache_key, res)

                # 仅当非降级保底链接且为站长位时，才持久化写入 products 表，防止假商品入库
                if not clean_uid and not is_fallback:
                    self.repo.save_promotion_link_cache(
                        platform=plat,
                        data=res,
                        goods_id=req_goods_id,
                        goods_sign=req_goods_sign,
                        goods_key=cache_key
                    )

                return res

        # 4. 如果是拼多多且通过原始链接/商品ID转链
        if plat == "pdd":
            real_goods_id = kwargs.get("goods_id")
            if not real_goods_id and goods_id_or_sign and str(goods_id_or_sign).isdigit():
                real_goods_id = str(goods_id_or_sign)
            if not real_goods_id and goods_id_or_sign:
                doc = self.repo.get_product_detail(goods_id_or_sign)
                if doc and doc.get("product_id") and str(doc.get("product_id")).isdigit():
                    real_goods_id = str(doc.get("product_id"))

            target_url = product_url or (f"https://mobile.pinduoduo.com/goods.html?goods_id={real_goods_id}" if real_goods_id else "")
            if target_url:
                zs_res = convert_pdd_url(
                    client_id=PDD_CLIENT_ID,
                    client_secret=PDD_CLIENT_SECRET,
                    pid=PDD_PID,
                    source_url=target_url,
                    uid=clean_uid
                )
                if zs_res.get("status") == "success":
                    jump_url = zs_res.get("jump_url")
                    title = kwargs.get("title") or kwargs.get("name") or "精选高性价比好物"
                    price = kwargs.get("price")
                    coupon = kwargs.get("coupon_amount") or kwargs.get("coupon")
                    cmd_lines = [f"【好物精选】{title}"]
                    if price and float(price) > 0:
                        p_info = f"【券后特惠】¥{price}"
                        if coupon and float(coupon) > 0:
                            p_info += f"（已立减 ¥{coupon}）"
                        cmd_lines.append(p_info)
                    cmd_lines.append("【抢购口令】长按复制本段全部信息，打开【拼多多APP】立即领券下单！")
                    cmd_lines.append(f"{jump_url}")
                    ret = {
                        "status": "success",
                        "need_auth": False,
                        "link_type": "单品外部转链",
                        "final_link": jump_url,
                        "url": jump_url,
                        "short_url": jump_url,
                        "mobile_url": jump_url,
                        "mobile_short_url": jump_url,
                        "command_text": "\n".join(cmd_lines),
                        "we_app_info": {},
                        "raw": zs_res.get("raw_data", {})
                    }
                    _PROMOTION_CACHE.set(user_cache_key, ret)
                    if not clean_uid:
                        self.repo.save_promotion_link_cache(
                            platform=plat,
                            data=ret,
                            goods_id=real_goods_id,
                            goods_key=cache_key
                        )
                    return ret

        # 5. 兜底返回错误
        return {
            "status": "error",
            "error_msg": "未能生成有效推广转链，商品可能已失效或缺少必要标识"
        }
