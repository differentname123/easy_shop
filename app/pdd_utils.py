# -*- coding: utf-8 -*-
"""
=============================================================================
[顶层数据流] 拼多多电商导购开放平台 API 统一调度与工具库
=============================================================================
* [核心定位]：对外屏蔽拼多多网关复杂的签名校验、接口差异及防刷风控逻辑，
              提供通用可靠的基础接口，并组合封装为高聚合的应用级业务函数。
* [设计原则]：
    1. 纯函数设计，拒绝过度面向对象包装，注重直观易用。
    2. 参数设计支持未来业务字段自由透传（支持 **kwargs）。
    3. 全链路数据清洗标准收敛：价格统一输出为“分”或“元”，抹平各类 API 字段差异。
=============================================================================
"""

import time
import json
import hashlib
import traceback

import requests
from common.common_utils import get_config, setup_logger

logger = setup_logger(app_name="nana_pdd_utils")


# =============================================================================
# 一、核心网关与通用协议基础设施
# =============================================================================

def make_custom_parameters(uid=None, extra_custom=None):
    """
    通用参数序列化：构建拼多多标准 custom_parameters 字符串
    """
    custom_dict = {}
    if uid is not None and str(uid).strip():
        custom_dict["uid"] = str(uid).strip()
    if extra_custom and isinstance(extra_custom, dict):
        custom_dict.update(extra_custom)

    if not custom_dict:
        return None
    return json.dumps(custom_dict, ensure_ascii=False, separators=(',', ':'))


def extract_short_url(response_dict):
    """
    通用提取器：从各类转链/生链返回结果中提取最优短链接
    """
    if not isinstance(response_dict, dict):
        return None
    return (
        response_dict.get("mobile_short_url")
        or response_dict.get("short_url")
        or response_dict.get("url")
    )


def call_pdd_api(client_id, client_secret, api_type, business_params=None, timeout=10):
    """
    拼多多网关核心请求与 MD5 自动签名引擎
    """
    url = "https://gw-api.pinduoduo.com/api/router"
    business_params = business_params or {}

    # 1. 过滤并组装初始参数
    raw_params = {
        "type": api_type,
        "client_id": client_id,
        "timestamp": str(int(time.time())),
        "data_type": "JSON",
        "version": "V1"
    }
    raw_params.update({k: v for k, v in business_params.items() if v is not None})

    # 2. 参数彻底扁平化/字符串化（防止 Java 网关解析类型歧义）
    params = {}
    for key, val in raw_params.items():
        if isinstance(val, bool):
            params[key] = "true" if val else "false"
        elif isinstance(val, (list, dict)):
            params[key] = json.dumps(val, ensure_ascii=False, separators=(',', ':'))
        else:
            params[key] = str(val)

    # 3. 字典序排列后加盐计算 MD5 签名
    sign_str = client_secret
    for key in sorted(params.keys()):
        sign_str += f"{key}{params[key]}"
    sign_str += client_secret

    params["sign"] = hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()

    # 4. 发起 HTTP POST 请求
    try:
        response = requests.post(
            url,
            json=params,
            headers={"Content-Type": "application/json;charset=utf-8"},
            timeout=timeout
        )
        response.raise_for_status()
        result = response.json()
    except Exception as e:
        raise RuntimeError(f"拼多多网络请求层崩溃: {str(e)}")

    # 5. 网关级与业务级错误校验拦截
    if "error_response" in result:
        err_resp = result["error_response"]
        error_msg = err_resp.get("error_msg", "未知业务错误")
        sub_msg = err_resp.get("sub_msg", "")
        error_code = err_resp.get("error_code", "")
        sub_code = err_resp.get("sub_code", "")
        full_error = f"[{error_code}:{sub_code}] {error_msg} - {sub_msg}".strip(" -:")
        raise RuntimeError(f"PDD网关拒绝: {full_error}")

    return result


# =============================================================================
# 二、业务数据清洗归一化函数
# =============================================================================

def clean_goods_item(raw_data, source_type=""):
    """
    通用商品数据清洗：抹平单品详情、全网搜索、智能推荐接口字段差异
    """
    if not raw_data or not isinstance(raw_data, dict):
        return None

    min_group_price = raw_data.get("min_group_price", 0)
    coupon_discount = raw_data.get("coupon_discount", 0)
    promotion_rate = raw_data.get("promotion_rate", 0)

    # 计算预估佣金（分）
    estimated_commission = int((min_group_price - coupon_discount) * promotion_rate / 1000) if promotion_rate > 0 else 0

    return {
        "goods_id": raw_data.get("goods_id", 0),
        "goods_sign": raw_data.get("goods_sign", ""),
        "goods_name": raw_data.get("goods_name", ""),
        "goods_desc": raw_data.get("goods_desc", ""),
        "category_name": raw_data.get("category_name", ""),
        "brand_name": raw_data.get("brand_name", ""),
        "goods_thumbnail_url": raw_data.get("goods_thumbnail_url", ""),
        "goods_image_url": raw_data.get("goods_image_url", ""),
        "min_group_price": min_group_price,  # 拼团最低价(分)
        "min_group_price_yuan": round(min_group_price / 100.0, 2),
        "min_normal_price": raw_data.get("min_normal_price", min_group_price),
        "promotion_rate": promotion_rate,  # 千分比，如 200 代表 20%
        "estimated_commission": estimated_commission,  # 预估佣金(分)
        "estimated_commission_yuan": round(estimated_commission / 100.0, 2),
        "sales_tip": raw_data.get("sales_tip", "0"),
        "unified_tags": raw_data.get("unified_tags", []),
        "mall_name": raw_data.get("mall_name", ""),
        "merchant_type": raw_data.get("merchant_type", 1),
        "desc_txt": raw_data.get("desc_txt", "平"),
        "serv_txt": raw_data.get("serv_txt", "平"),
        "lgst_txt": raw_data.get("lgst_txt", "平"),
        "has_coupon": raw_data.get("has_coupon", False),
        "coupon_discount": coupon_discount,
        "coupon_discount_yuan": round(coupon_discount / 100.0, 2),
        "coupon_min_order_amount": raw_data.get("coupon_min_order_amount", 0),
        "coupon_remain_quantity": raw_data.get("coupon_remain_quantity", 0),
        "coupon_total_quantity": raw_data.get("coupon_total_quantity", 0),
        "coupon_start_time": raw_data.get("coupon_start_time", 0),
        "coupon_end_time": raw_data.get("coupon_end_time", 0),
        "has_mall_coupon": raw_data.get("has_mall_coupon", False),
        "search_id": raw_data.get("search_id", ""),
        "_source_api": source_type,
        "_raw_data": raw_data
    }


def clean_order_item(order):
    """
    通用订单数据清洗：提取推手归属、状态映射、分转元算费
    """
    raw_custom = order.get("custom_parameters", "")
    promoter_uid = None

    if raw_custom:
        try:
            custom_dict = json.loads(raw_custom)
            if isinstance(custom_dict, dict) and "uid" in custom_dict:
                promoter_uid = str(custom_dict["uid"])
            else:
                promoter_uid = str(raw_custom)
        except Exception:
            promoter_uid = str(raw_custom)

    status_code = order.get("order_status")
    status_desc = order.get("order_status_desc", "未知状态")
    order_amount_fen = order.get("order_amount", 0)
    promotion_amount_fen = order.get("promotion_amount", 0)
    goods_price_fen = order.get("goods_price", 0)

    return {
        "order_sn": order.get("order_sn"),
        "goods_id": order.get("goods_id"),
        "goods_name": order.get("goods_name"),
        "goods_thumbnail_url": order.get("goods_thumbnail_url"),
        "goods_quantity": order.get("goods_quantity", 1),
        "goods_price_yuan": round(goods_price_fen / 100.0, 2),
        "order_amount_yuan": round(order_amount_fen / 100.0, 2),
        "promotion_amount_yuan": round(promotion_amount_fen / 100.0, 2),
        "promotion_rate": round(order.get("promotion_rate", 0) / 10.0, 2),
        "order_status": status_code,
        "order_status_desc": status_desc,
        "is_refund": (status_code == 4),  # 审核失败/维权退款
        "is_settled": (status_code in [3, 5]),  # 已结算成功
        "promoter_uid": promoter_uid,
        "is_self_operated": not bool(promoter_uid),
        "pid": order.get("p_id"),
        "order_pay_time": order.get("order_pay_time"),
        "order_receive_time": order.get("order_receive_time"),
        "order_settle_time": order.get("order_settle_time"),
        "order_modify_at": order.get("order_modify_at"),
    }


# =============================================================================
# 三、推广位备案与授权管理模块 (专治 60001 与 99999 报错)
# =============================================================================

def check_member_authority(client_id, client_secret, pid, uid):
    """
    [基础函数] 查询指定推手/渠道是否已在拼多多完成绑定备案
    接口：pdd.ddk.member.authority.query
    """
    custom_params = make_custom_parameters(uid=uid)
    params = {"pid": pid, "custom_parameters": custom_params}

    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.member.authority.query", params)
        resp_data = res.get("authority_query_response", {})
        bind = resp_data.get("bind", 0)
        return {
            "status": "success",
            "bind": bind,
            "is_authorized": (bind == 1),
            "request_id": resp_data.get("request_id")
        }
    except Exception as e:
        logger.error(f"[备案查询异常] uid={uid}, 错误: {str(e)}")
        return {"status": "error", "bind": 0, "is_authorized": False, "error_msg": str(e)}


def generate_authority_url(client_id, client_secret, pid, uid=None, **kwargs):
    """
    [基础函数] 生成推手/渠道专属授权备案链接 (channel_type=10)
    接口：pdd.ddk.rp.prom.url.generate
    """
    business_params = {
        "p_id_list": [pid],
        "channel_type": 10,
        "generate_we_app": True,
        "generate_short_url": True,
        "generate_mobile": True,
        "custom_parameters": make_custom_parameters(uid=uid)
    }
    business_params.update(kwargs)

    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.rp.prom.url.generate", business_params)
        url_list = res.get("rp_promotion_url_generate_response", {}).get("url_list", [])
        if url_list:
            return {"status": "success", "url_info": url_list[0]}
        return {"status": "error", "error_msg": "接口调用成功但未返回可用链接"}
    except Exception as e:
        return {"status": "error", "error_msg": f"生成备案链接失败: {str(e)}"}


def ensure_promoter_authority(client_id, client_secret, pid, uid):
    """
    [复合应用函数] 授权门禁守门人：先查备案状态，未备案则自动生成专属授权链接
    """
    if not uid:
        return {"status": "error", "error_msg": "uid 不能为空，必须传入具体推手唯一标识"}

    query_res = check_member_authority(client_id, client_secret, pid, uid)
    if query_res.get("status") != "success":
        return query_res

    if query_res.get("is_authorized"):
        return {
            "status": "success",
            "is_authorized": True,
            "bind": 1,
            "msg": f"推手 [UID: {uid}] 已完成授权备案"
        }

    # 尚未绑定，生成该推手专属授权链接
    auth_gen_res = generate_authority_url(client_id, client_secret, pid, uid=uid)
    if auth_gen_res.get("status") == "success":
        url_info = auth_gen_res.get("url_info", {})
        return {
            "status": "need_auth",
            "is_authorized": False,
            "bind": 0,
            "msg": f"推手 [UID: {uid}] 尚未授权备案，需引导其点击链接授权",
            "auth_info": {
                "short_url": url_info.get("short_url"),
                "mobile_short_url": url_info.get("mobile_short_url"),
                "mobile_url": url_info.get("mobile_url"),
                "we_app_info": url_info.get("we_app_info", {})
            }
        }

    return {
        "status": "error",
        "is_authorized": False,
        "bind": 0,
        "error_msg": f"生成专属备案链接失败: {auth_gen_res.get('error_msg')}"
    }


# =============================================================================
# 四、商品转链与多策略生链模块
# =============================================================================

def convert_pdd_url(client_id, client_secret, pid, source_url, uid=None, **kwargs):
    """
    [基础函数] 外部/单条拼多多链接清洗转换 (洗出带有自己 PID 的推广链)
    接口：pdd.ddk.goods.zs.unit.url.gen
    """
    params = {
        "pid": pid,
        "source_url": source_url,
        "custom_parameters": make_custom_parameters(uid=uid)
    }
    params.update(kwargs)

    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.zs.unit.url.gen", params)
        data = res.get("goods_zs_unit_generate_response", {})
        jump_url = extract_short_url(data)
        if jump_url:
            return {"status": "success", "jump_url": jump_url, "raw_data": data}
        return {"status": "error", "error_msg": "接口返回正常但未包含可用的跳转链接"}
    except Exception as e:
        return {"status": "error", "error_msg": f"转链失败: {str(e)}"}


def batch_convert_pdd_urls(client_id, client_secret, pid, url_list, uid=None, **kwargs):
    """
    [复合应用函数] 批量外部链接清洗与转换
    """
    results = {}
    for url in url_list:
        if not url or not isinstance(url, str):
            continue
        results[url] = convert_pdd_url(client_id, client_secret, pid, source_url=url, uid=uid, **kwargs)
    return results


def generate_goods_promotion_url(client_id, client_secret, pid, goods_sign, uid=None, search_id=None, **kwargs):
    """
    [基础函数] 使用官方标准接口直接将 goods_sign 生成推广短链
    接口：pdd.ddk.goods.promotion.url.generate
    """
    params = {
        "p_id": pid,
        "goods_sign_list": [goods_sign],
        "generate_short_url": True,
        "generate_mobile": True,
        "generate_schema_url": True,
        "generate_we_app": True,
        "custom_parameters": make_custom_parameters(uid=uid),
        "search_id": search_id
    }
    params.update(kwargs)

    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.promotion.url.generate", params)
        url_list = res.get("goods_promotion_url_generate_response", {}).get("goods_promotion_url_list", [])
        if url_list:
            return {"status": "success", "data": url_list[0]}
        return {"status": "error", "error_msg": "未能生成推广链接"}
    except Exception as e:
        return {"status": "error", "error_msg": f"生成商品推广链接异常: {str(e)}"}


def generate_smart_promotion_url(client_id, client_secret, pid, goods_sign, cash_gift_id=None, uid=None, **kwargs):
    """
    [复合应用函数] 终极智能降级生链：礼金 > 官方超级红包(Channel 23) > 普通推广链接
    """
    custom_params = make_custom_parameters(uid=uid)

    # 策略 1：如果传入了礼金活动 ID，尝试生成礼金秒杀单
    if cash_gift_id:
        try:
            params = {
                "p_id": pid,
                "goods_sign_list": [goods_sign],
                "cash_gift_id": int(cash_gift_id),
                "generate_short_url": True,
                "custom_parameters": custom_params
            }
            params.update(kwargs)
            res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.promotion.url.generate", params)
            url_list = res.get("goods_promotion_url_generate_response", {}).get("goods_promotion_url_list", [])
            if url_list:
                return {"status": "success", "link_type": "多多礼金秒杀", "data": url_list[0]}
        except Exception:
            pass  # 礼金降级，平滑进入下一级

    # 策略 2：尝试获取官方超级红包卡券池链接
    try:
        hb_params = {
            "p_id_list": [pid],
            "channel_type": 23,
            "generate_short_url": True,
            "diy_sp_red_packet_param": {"goods_sign": goods_sign},
            "custom_parameters": custom_params
        }
        hb_res = call_pdd_api(client_id, client_secret, "pdd.ddk.rp.prom.url.generate", hb_params)
        hb_url_list = hb_res.get("rp_promotion_url_generate_response", {}).get("url_list", [])
        if hb_url_list:
            return {"status": "success", "link_type": "官方超级红包", "data": hb_url_list[0]}
    except Exception:
        traceback.print_exc()
        pass  # 降级进入常规普通生链

    # 策略 3：标准兜底推广
    fallback_res = generate_goods_promotion_url(client_id, client_secret, pid, goods_sign, uid=uid, **kwargs)
    if fallback_res.get("status") == "success":
        fallback_res["link_type"] = "常规推广"
    return fallback_res


# =============================================================================
# 五、商品检索与推荐模块
# =============================================================================

def get_goods_detail(client_id, client_secret, pid=None, goods_sign=None, goods_id=None, uid=None, **kwargs):
    """
    [基础函数] 统一单品详情查询器：
    - 优先根据 goods_sign 直连官方 Detail 接口；
    - 仅有 goods_id 时，自动执行 “转短链 -> 关键词截胡搜索” 的双跨策略。
    """
    if not goods_sign and not goods_id:
        return {"status": "error", "error_msg": "必须提供 goods_sign 或 goods_id 其中之一"}

    custom_params = make_custom_parameters(uid=uid)

    # 路径 A：标准 goods_sign 查询
    if goods_sign:
        try:
            params = {
                "goods_sign": goods_sign,
                "pid": pid,
                "custom_parameters": custom_params
            }
            params.update(kwargs)
            res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.detail", params)
            goods_list = res.get("goods_detail_response", {}).get("goods_details", [])
            if goods_list:
                cleaned = clean_goods_item(goods_list[0], source_type="detail_api")
                return {"status": "success", "data": cleaned}
            return {"status": "error", "error_msg": "Detail 接口未找到商品，可能已下架或无推广计划"}
        except Exception as e:
            return {"status": "error", "error_msg": f"Detail 查询失败: {str(e)}"}

    # 路径 B：goods_id 降级策略（通过短链反查）
    try:
        source_url = f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_id}"
        zs_res = convert_pdd_url(client_id, client_secret, pid=pid, source_url=source_url, uid=uid)
        short_url = zs_res.get("jump_url")
        if not short_url:
            return {"status": "error", "error_msg": "通过 goods_id 生成探针短链失败"}

        search_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", {
            "keyword": short_url,
            "pid": pid,
            "custom_parameters": custom_params,
            "page": 1,
            "page_size": 10
        })
        goods_list = search_res.get("goods_search_response", {}).get("goods_list", [])
        if goods_list:
            cleaned = clean_goods_item(goods_list[0], source_type="search_api_probe")
            return {"status": "success", "data": cleaned}
        return {"status": "error", "error_msg": "探针短链搜索未命中，商品可能不可推广"}
    except Exception as e:
        return {"status": "error", "error_msg": f"goods_id 反查中断: {str(e)}"}


def search_goods(client_id, client_secret, pid, keyword, limit_count=0, uid=None, **kwargs):
    """
    [复合应用函数] 关键词搜索多多进宝商品列表 (支持智能多页拉取与容错)
    接口：pdd.ddk.goods.search
    """
    if not keyword:
        return {"status": "error", "error_msg": "搜索关键词不能为空"}

    custom_params = make_custom_parameters(uid=uid)
    all_goods = []
    current_page = 1
    page_size = 100
    list_id = None

    while True:
        try:
            params = {
                "keyword": keyword,
                "pid": pid,
                "page": current_page,
                "page_size": page_size,
                "custom_parameters": custom_params
            }
            params.update(kwargs)
            if current_page > 1 and list_id:
                params["list_id"] = list_id

            res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", params)
            resp_data = res.get("goods_search_response", {})
            goods_list = resp_data.get("goods_list", [])

            if current_page == 1:
                list_id = resp_data.get("list_id")

            if not goods_list:
                break

            for item in goods_list:
                cleaned = clean_goods_item(item, source_type="search_api")
                if cleaned:
                    all_goods.append(cleaned)

            if limit_count > 0 and len(all_goods) >= limit_count:
                all_goods = all_goods[:limit_count]
                break

            current_page += 1
            time.sleep(0.15)  # 频控规避

        except Exception as e:
            if all_goods:
                logger.warning(f"搜索发生降级中断，已保留当前抓取的 {len(all_goods)} 条数据: {str(e)}")
                break
            return {"status": "error", "error_msg": f"关键词搜索失败: {str(e)}"}

    return {
        "status": "success",
        "total_count": len(all_goods),
        "data": all_goods
    }


def get_recommend_goods(client_id, client_secret, pid=None, channel_type=5, limit_count=0, uid=None, **kwargs):
    """
    [复合应用函数] 获取多多进宝商品推荐榜单 (如实时热销榜/大促爆款，自动多页收集)
    接口：pdd.ddk.goods.recommend.get
    """
    custom_params = make_custom_parameters(uid=uid)
    all_goods = []
    current_offset = 0
    batch_limit = 50
    list_id = None

    while True:
        try:
            params = {
                "channel_type": channel_type,
                "limit": batch_limit,
                "offset": current_offset,
                "pid": pid,
                "custom_parameters": custom_params
            }
            params.update(kwargs)
            if current_offset > 0 and list_id:
                params["list_id"] = list_id

            res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.recommend.get", params)
            resp_data = res.get("goods_basic_detail_response", {})
            goods_list = resp_data.get("list", [])

            if current_offset == 0:
                list_id = resp_data.get("list_id")

            if not goods_list:
                break

            for item in goods_list:
                cleaned = clean_goods_item(item, source_type=f"recommend_ch_{channel_type}")
                if cleaned:
                    all_goods.append(cleaned)

            if limit_count > 0 and len(all_goods) >= limit_count:
                all_goods = all_goods[:limit_count]
                break

            current_offset += batch_limit
            time.sleep(0.15)

        except Exception as e:
            if all_goods:
                logger.warning(f"推荐列表拉取中断，保留已获取的 {len(all_goods)} 条数据: {str(e)}")
                break
            return {"status": "error", "error_msg": f"推荐接口调用崩溃: {str(e)}"}

    return {
        "status": "success",
        "total_count": len(all_goods),
        "data": all_goods
    }


def verify_and_convert_goods(client_id, client_secret, pid, original_url, goods_sign=None, goods_id=None, uid=None, **kwargs):
    """
    [复合应用函数] 商品质量探针与拦截式转链：
    验证商品有库存、价格大于0且存在有效推广后，才执行实质转链。
    """
    # 1. 探针验证商品
    detail_res = get_goods_detail(
        client_id=client_id,
        client_secret=client_secret,
        pid=pid,
        goods_sign=goods_sign,
        goods_id=goods_id,
        uid=uid
    )

    if detail_res.get("status") != "success":
        msg = f"探针校验失败 | 错误: {detail_res.get('error_msg')}"
        logger.error(f"[转链拦截] ❌ {msg}")
        return {"status": "error", "error_msg": msg, "detail_response": detail_res}

    goods_info = detail_res["data"]
    min_group_price = goods_info.get("min_group_price", 0)
    goods_name = goods_info.get("goods_name", "").strip()

    # 2. 价格/风控拦截检测
    if min_group_price <= 0 or not goods_name:
        msg = f"商品异常或已下架 | 名称: {goods_name} | 价格: {min_group_price}分"
        logger.error(f"[转链拦截] ❌ {msg}")
        return {"status": "error", "error_msg": msg, "goods_info": goods_info}

    # 3. 执行洗链
    convert_res = convert_pdd_url(client_id, client_secret, pid, source_url=original_url, uid=uid, **kwargs)
    if convert_res.get("status") == "success":
        return {
            "status": "success",
            "msg": "价格验证通过且转链成功",
            "goods_info": goods_info,
            "jump_url": convert_res["jump_url"],
            "raw_convert_data": convert_res.get("raw_data")
        }

    err_msg = convert_res.get("error_msg", "未知转链错误")
    logger.error(f"[转链拦截] ❌ 探针通过但核心转链失败: {err_msg}")
    return {"status": "error", "error_msg": err_msg, "goods_info": goods_info}


# =============================================================================
# 六、订单同步与归因核算模块
# =============================================================================

def query_increment_orders(client_id, client_secret, start_time, end_time, page=1, page_size=50, **kwargs):
    """
    [订单同步] 按订单更新时间增量同步（用于佣金核算、处理退款与订单变更）
    接口：pdd.ddk.order.list.increment.get
    """
    params = {
        "start_update_time": int(start_time),
        "end_update_time": int(end_time),
        "page": int(page),
        "page_size": int(page_size),
        "return_count": True
    }
    params.update(kwargs)

    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.order.list.increment.get", params)
        resp_data = res.get("order_list_get_response", {})
        raw_orders = resp_data.get("order_list", [])
        total_count = resp_data.get("total_count", 0)

        cleaned_orders = [clean_order_item(item) for item in raw_orders]
        return {
            "status": "success",
            "total_count": total_count,
            "order_list": cleaned_orders
        }
    except Exception as e:
        return {"status": "error", "error_msg": f"增量订单查询失败: {str(e)}", "order_list": []}


def query_range_orders(client_id, client_secret, start_time_str, end_time_str, page_size=100, **kwargs):
    """
    [历史对账] 按支付时间范围查询订单 (对齐多多进宝后台报表)
    接口：pdd.ddk.order.list.range.get
    :param start_time_str: "2026-10-30 00:00:00"
    :param end_time_str: "2026-10-30 23:59:59"
    """
    params = {
        "start_time": start_time_str,
        "end_time": end_time_str,
        "page_size": int(page_size)
    }
    params.update(kwargs)

    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.order.list.range.get", params)
        raw_orders = res.get("order_list_get_response", {}).get("order_list", [])
        cleaned_orders = [clean_order_item(item) for item in raw_orders]

        return {
            "status": "success",
            "total_count": len(cleaned_orders),
            "order_list": cleaned_orders
        }
    except Exception as e:
        return {"status": "error", "error_msg": f"区间订单查询失败: {str(e)}", "order_list": []}


def query_order_detail(client_id, client_secret, order_sn):
    """
    [单笔对账] 查询单笔订单详情
    接口：pdd.ddk.order.detail.get
    """
    params = {"order_sn": str(order_sn).strip()}
    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.order.detail.get", params)
        raw_order = res.get("order_detail_response", {})
        if not raw_order:
            return {"status": "error", "error_msg": "未查询到该订单，请核对是否由当前账号产生"}
        return {"status": "success", "order": clean_order_item(raw_order)}
    except Exception as e:
        return {"status": "error", "error_msg": f"单笔订单查询失败: {str(e)}"}


# =============================================================================
# 七、各类典型应用场景与调用示例（中文注释与测试用例）
# =============================================================================

def example_recommend_goods(client_id, client_secret, pid):
    """
    【示例 1：获取榜单热门推荐商品】
    作用：用于网站首页、公众号每日爆款、实时热销商品展示。
    """
    logger.info("========== [示例 1] 开始执行推荐商品获取 (实时热销榜) ==========")
    rec_result = get_recommend_goods(
        client_id=client_id,
        client_secret=client_secret,
        pid=pid,
        channel_type=5,  # 5: 实时热销榜
        limit_count=5
    )

    if rec_result["status"] == "success":
        goods_list = rec_result.get("data", [])
        logger.info(f"✅ 成功获取推荐商品 {len(goods_list)} 条：")
        for idx, item in enumerate(goods_list, start=1):
            name = item["goods_name"][:20] + "..." if len(item["goods_name"]) > 20 else item["goods_name"]
            logger.info(
                f"  [{idx:02d}] {name} | 券后价: ¥{item['min_group_price_yuan']} | "
                f"预估佣金: ¥{item['estimated_commission_yuan']} | 销量: {item['sales_tip']}"
            )
    else:
        logger.error(f"❌ 推荐失败: {rec_result.get('error_msg')}")


def example_search_goods_by_keyword(client_id, client_secret, pid):
    """
    【示例 2：全网商品搜索与比价】
    作用：用户在你的前端输入关键字，通过此函数多页抓取高质量可推广商品。
    """
    test_keyword = "纸巾"
    logger.info(f"========== [示例 2] 开始关键词搜索测试 [{test_keyword}] ==========")
    search_res = search_goods(
        client_id=client_id,
        client_secret=client_secret,
        pid=pid,
        keyword=test_keyword,
        limit_count=5
    )

    if search_res["status"] == "success":
        goods_list = search_res.get("data", [])
        logger.info(f"✅ 搜索成功，获取 {len(goods_list)} 条记录：")
        for idx, item in enumerate(goods_list, start=1):
            name = item["goods_name"][:20] + "..." if len(item["goods_name"]) > 20 else item["goods_name"]
            logger.info(
                f"  [{idx:02d}] {name} | 拼团价: ¥{item['min_group_price_yuan']} | "
                f"有券: {'是' if item['has_coupon'] else '否'}"
            )
    else:
        logger.error(f"❌ 搜索失败: {search_res.get('error_msg')}")


def example_station_master_authority_url(client_id, client_secret, pid):
    """
    【示例 3：站长自身推广位 (PID) 激活备案】
    作用：专治 60001 (推广位未备案) 错误。运行此示例生成链接，在微信中点击同意，全站永久激活。
    """
    logger.info("========== [示例 3] 生成站长专属 PID 备案激活链接 ==========")
    auth_res = generate_authority_url(client_id, client_secret, pid, uid=None)
    if auth_res["status"] == "success":
        url_info = auth_res["url_info"]
        logger.info("✅ 备案链接已生成！请务必亲自在微信中打开一次点击同意授权：")
        logger.info(f"👉 微信直点短链: {url_info.get('mobile_short_url') or url_info.get('short_url')}")
    else:
        logger.error(f"❌ 生成备案失败: {auth_res.get('error_msg')}")


def example_goods_promotion_and_external_convert(client_id, client_secret, pid):
    """
    【示例 4：goods_sign 官方生链 与 他人链接外部清洗】
    作用：
    1. 拥有商品 goods_sign 时快速生成直达推广短链；
    2. 发现同行其他人的拼多多推广链，洗成自己能赚佣金的专属链接。
    """
    logger.info("========== [示例 4] 开始测试转链与外部洗链 ==========")
    test_goods_sign = "E9L2aS_F_49tSR-hwfDBRxSudPR7NgLPyQ_JVhvjLwXu"

    # 1. goods_sign 生链
    gen_res = generate_goods_promotion_url(client_id, client_secret, pid, goods_sign=test_goods_sign)
    if gen_res["status"] == "success":
        logger.info(f"✅ goods_sign 生链成功: {gen_res['data'].get('mobile_short_url')}")
    else:
        logger.warning(f"⚠️ goods_sign 生链提示: {gen_res.get('error_msg')}")

    # 2. 外部链接洗链
    other_link = "https://mobile.yangkeduo.com/goods.html?goods_id=498657894908"
    convert_res = convert_pdd_url(client_id, client_secret, pid, source_url=other_link)
    if convert_res["status"] == "success":
        logger.info(f"✅ 外部链接洗链成功: {convert_res.get('jump_url')}")
    else:
        logger.warning(f"⚠️ 外部洗链提示: {convert_res.get('error_msg')}")


def example_verified_conversion(client_id, client_secret, pid):
    """
    【示例 5：风控/价格探针 + 聚合安全转链】
    作用：转链前先检测商品真实性、是否被下架或被封控；验证商品售价正常才执行最终洗链。
    """
    logger.info("========== [示例 5] 执行聚合安全转链 (含商品探针) ==========")
    target_url = "https://mobile.pinduoduo.com/goods.html?goods_id=627575562243"
    test_goods_id = "627575562243"

    agg_result = verify_and_convert_goods(
        client_id=client_id,
        client_secret=client_secret,
        pid=pid,
        original_url=target_url,
        goods_id=test_goods_id
    )

    if agg_result["status"] == "success":
        goods = agg_result["goods_info"]
        logger.info(f"✅ 安全转链成功 | 商品: 【{goods['goods_name']}】 | 券后价: ¥{goods['min_group_price_yuan']} | 链接: {agg_result['jump_url']}")
    else:
        logger.warning(f"⚠️ 安全转链拦截/失败: {agg_result.get('error_msg')}")


def example_promoter_authority_guard(client_id, client_secret, pid):
    """
    【示例 6：推手代理分佣授权“守门人”】
    作用：多推手/多用户分销模式下，判断指定 uid 用户是否已授权，未授权返回授权卡片，已授权直接生链。
    """
    logger.info("========== [示例 6] 执行推手授权门禁中枢 ==========")
    promoter_uid = "nana"
    goods_sign = "E9_2RCop4ZxgMvVVwvfBWrUNTWj41qgr_JhRip1g5D"

    # 第一步：过守门人
    auth_status = ensure_promoter_authority(client_id, client_secret, pid, uid=promoter_uid)

    if not auth_status.get("is_authorized"):
        auth_url = auth_status["auth_info"]["mobile_short_url"]
        logger.info(f"⚠️ 该推手尚未备案，引导其在微信打开完成一次性授权: {auth_url}")
    else:
        # 已授权，安全调用智能生链
        prom_res = generate_smart_promotion_url(
            client_id=client_id,
            client_secret=client_secret,
            pid=pid,
            goods_sign=goods_sign,
            uid=promoter_uid
        )
        if prom_res["status"] == "success":
            logger.info(f"✅ 生成专属代理推广链 ({prom_res.get('link_type')}): {prom_res['data'].get('mobile_short_url')}")


def example_historical_orders(client_id, client_secret):
    """
    【示例 7：历史时间段订单结算与对账】
    作用：指定日期范围，对齐拼多多后台订单明细，自动归集佣金及推手 UID。
    """
    logger.info("========== [示例 7] 查询历史区间订单数据 ==========")
    res = query_range_orders(
        client_id=client_id,
        client_secret=client_secret,
        start_time_str="2026-10-30 00:00:00",
        end_time_str="2026-10-30 23:59:59"
    )

    if res["status"] == "success":
        logger.info(f"✅ 成功查询到订单共 {res['total_count']} 笔：")
        for order in res["order_list"][:5]:  # 仅展示前5笔
            logger.info(
                f"  👉 订单号: {order['order_sn']} | 金额: ¥{order['order_amount_yuan']} | "
                f"预估佣金: ¥{order['promotion_amount_yuan']} | 状态: {order['order_status_desc']}"
            )
    else:
        logger.error(f"❌ 订单查询异常: {res.get('error_msg')}")


# =============================================================================
# 八、主执行入口
# =============================================================================

if __name__ == "__main__":
    # 统一读取主配置账号
    pdd_client_id = get_config("mama_pdd_client_id")
    pdd_client_secret = get_config("mama_pdd_client_secret")
    pdd_pid = get_config("mama_pdd_pid")

    logger.info(">>> 启动拼多多 Open API 业务测试套件 <<<")

    # [可按需开启或注释调用下方示例]

    # # 1. 榜单推荐商品测试
    # example_recommend_goods(pdd_client_id, pdd_client_secret, pdd_pid)
    #
    # # 2. 关键词智能搜索测试
    # example_search_goods_by_keyword(pdd_client_id, pdd_client_secret, pdd_pid)

    # # 3. 站长自身 PID 备案链接生成 (遇 60001 时运行一次即可)
    # example_station_master_authority_url(pdd_client_id, pdd_client_secret, pdd_pid)

    # # 4. 单品生成短链与外部洗链
    # example_goods_promotion_and_external_convert(pdd_client_id, pdd_client_secret, pdd_pid)
    #
    # # 5. 聚合防风控安全转链 (验证价格 > 0 且在架才转链)
    # example_verified_conversion(pdd_client_id, pdd_client_secret, pdd_pid)

    # 6. 推手代理授权验证与智能生链
    example_promoter_authority_guard(pdd_client_id, pdd_client_secret, pdd_pid)

    # 7. 历史订单与佣金对账查询 (根据需要修改日期参数)
    # example_historical_orders(pdd_client_id, pdd_client_secret)

    logger.info(">>> 所有测试用例执行完成 <<<")