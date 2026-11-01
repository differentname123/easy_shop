# -*- coding: utf-8 -*-
"""
推广转链与佣金归因服务
确保用户在点击商品跳转购买时，自动带上当前推手的专属 uid，以便后续佣金准确归因。
"""
from typing import Dict, Any, Optional
from app.mobile_web.adapters import get_adapter
from app.mobile_web.config import PDD_CLIENT_ID, PDD_CLIENT_SECRET, PDD_PID, logger
from app.pdd_utils import convert_pdd_url


class PromoteService:
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

        # 1. 优先使用 goods_sign 生链
        if goods_id_or_sign:
            res = adapter.convert_promotion_url(goods_id_or_sign, uid=uid, **kwargs)
            if res.get("status") == "success":
                return res

        # 2. 如果是拼多多且通过原始链接/商品ID转链
        if plat == "pdd":
            target_url = product_url or (f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_id_or_sign}" if goods_id_or_sign else "")
            if target_url:
                zs_res = convert_pdd_url(
                    client_id=PDD_CLIENT_ID,
                    client_secret=PDD_CLIENT_SECRET,
                    pid=PDD_PID,
                    source_url=target_url,
                    uid=uid
                )
                if zs_res.get("status") == "success":
                    jump_url = zs_res.get("jump_url")
                    return {
                        "status": "success",
                        "link_type": "单品外部转链",
                        "url": jump_url,
                        "short_url": jump_url,
                        "mobile_url": jump_url,
                        "mobile_short_url": jump_url,
                        "we_app_info": {},
                        "raw": zs_res.get("raw_data", {})
                    }

        # 3. 兜底返回错误
        return {
            "status": "error",
            "error_msg": "未能生成有效推广转链，商品可能已失效或缺少必要标识"
        }
