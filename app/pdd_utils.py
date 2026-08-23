# -*- coding: utf-8 -*-
""":authors:
    zhuxiaohu (Unified Version)
:description:
    拼多多商品详情大一统查询器
    - 支持 goods_sign 规范查询 (pdd.ddk.goods.detail)
    - 支持 goods_id 短链洗白截胡查询 (zs.unit.url.gen + search)
    - 统一返回字段结构，自动占位缺失数据
"""
import time
import json
import hashlib
import requests
from common.common_utils import get_config


def call_pdd_api(client_id, client_secret, api_type, business_params):
    """
    统一的拼多多 API 请求与自动签名器
    """
    url = "https://gw-api.pinduoduo.com/api/router"

    # 1. 构造基础参数
    params = {
        "type": api_type,
        "client_id": client_id,
        "timestamp": str(int(time.time())),
        "data_type": "JSON",
        "version": "V1",
    }

    # 合并业务参数并过滤 None 值
    params.update({k: v for k, v in business_params.items() if v is not None})

    # 2. 签名生成 (MD5 签名算法)
    sorted_keys = sorted(params.keys())
    sign_str = client_secret
    for key in sorted_keys:
        val = params[key]
        # PDD 的 bool 类型签名时必须转为小写字符串
        if isinstance(val, bool):
            val = "true" if val else "false"
        sign_str += f"{key}{val}"
    sign_str += client_secret

    params["sign"] = hashlib.md5(sign_str.encode('utf-8')).hexdigest().upper()

    # 3. 发起请求
    try:
        headers = {'Content-Type': 'application/json;charset=utf-8'}
        response = requests.post(url, json=params, headers=headers, timeout=10)
        response.raise_for_status()
        result = response.json()

        if "error_response" in result:
            print(f"[-] 接口 {api_type} 返回错误: {result['error_response'].get('error_msg')}")
            return None

        return result
    except Exception as e:
        print(f"[-] 接口 {api_type} 网络请求异常: {str(e)}")
        return None


def format_unified_response(raw_data, source_type):
    """
    统一数据清洗与格式化模块
    基于真实 API 返回值特征，提取核心高频字段，并进行安全占位。
    """
    if not raw_data:
        return None

    return {
        # ================= 1. 基础信息 =================
        "goods_id": raw_data.get("goods_id", 0),
        "goods_sign": raw_data.get("goods_sign", ""),
        "goods_name": raw_data.get("goods_name", ""),
        "goods_desc": raw_data.get("goods_desc", ""),
        "category_name": raw_data.get("category_name", ""),
        "brand_name": raw_data.get("brand_name", ""),

        # ================= 2. 图片信息 =================
        "goods_thumbnail_url": raw_data.get("goods_thumbnail_url", ""),  # 缩略图(适合列表页)
        "goods_image_url": raw_data.get("goods_image_url", ""),  # 高清主图(适合详情页/海报)

        # ================= 3. 价格与佣金 =================
        "min_group_price": raw_data.get("min_group_price", 0),  # 拼团价 (单位：分)
        "min_normal_price": raw_data.get("min_normal_price",
                                         raw_data.get("min_group_price", 0)),  # 单买价 (单位：分)
        "promotion_rate": raw_data.get("promotion_rate", 0),  # 佣金比例 (单位：千分之几)

        # 预估佣金收益绝对值（分） = (拼团价 - 优惠券) * 佣金比例 / 1000
        # 这里顺手算好，方便外部直接展示预估赚多少钱
        "estimated_commission": int(
            (raw_data.get("min_group_price", 0) - raw_data.get("coupon_discount", 0))
            * raw_data.get("promotion_rate", 0) / 1000
        ) if raw_data.get("promotion_rate", 0) > 0 else 0,

        # ================= 4. 销量与标签 =================
        "sales_tip": raw_data.get("sales_tip", "0"),  # 已拼件数 (如 "2732")
        "unified_tags": raw_data.get("unified_tags", []),  # 服务保障标签 (如 ['坏了包赔', '退货包运费'])

        # ================= 5. 店铺与 DSR 评分 =================
        "mall_name": raw_data.get("mall_name", ""),  # 店铺名称
        "merchant_type": raw_data.get("merchant_type", 1),  # 商家类型 (1个人,2企业,3旗舰店,4专卖店,5专营店,6普通店)
        "desc_txt": raw_data.get("desc_txt", "平"),  # 描述相符评分 (高/平/低)
        "serv_txt": raw_data.get("serv_txt", "平"),  # 服务态度评分 (高/平/低)
        "lgst_txt": raw_data.get("lgst_txt", "平"),  # 物流服务评分 (高/平/低)

        # ================= 6. 优惠券详情 =================
        "has_coupon": raw_data.get("has_coupon", False),  # 是否有平台券
        "coupon_discount": raw_data.get("coupon_discount", 0),  # 平台券面额 (单位：分)
        "coupon_min_order_amount": raw_data.get("coupon_min_order_amount", 0),  # 券使用门槛 (单位：分)
        "coupon_remain_quantity": raw_data.get("coupon_remain_quantity", 0),  # 剩余券量
        "coupon_total_quantity": raw_data.get("coupon_total_quantity", 0),  # 券总量
        "coupon_start_time": raw_data.get("coupon_start_time", 0),  # 券生效时间 (时间戳)
        "coupon_end_time": raw_data.get("coupon_end_time", 0),  # 券失效时间 (时间戳)
        "has_mall_coupon": raw_data.get("has_mall_coupon", False),  # 是否有店铺专属券

        # ================= 7. 溯源追踪 =================
        "search_id": raw_data.get("search_id", ""),  # 转链时带上此ID能提升收益或作为追踪凭证
        "_source_api": source_type,  # 标志是从 detail 还是 search 接口拿到的
        "_raw_data": raw_data  # 兜底：保留原始JSON，供未来取生僻字段使用
    }

def get_unified_pdd_goods_info(client_id, client_secret, pid, goods_sign=None, goods_id=None, uid=None):
    """
    大一统商品信息查询函数
    :param client_id: 拼多多应用 ID
    :param client_secret: 拼多多应用密钥
    :param pid: 推广位 PID
    :param goods_sign: 商品唯一标识 (优先使用)
    :param goods_id: 裸商品ID (作为备用策略)
    :param uid: 用户唯一标识 (走洗链策略时用于备案参数)
    :return: 统一格式的字典 / {"error": "..."}
    """

    # 构造自定义参数 custom_parameters
    custom_params_str = json.dumps({"uid": uid}, separators=(',', ':')) if uid else None

    # =========================================================
    # 策略 A: 拥有 goods_sign，走标准 Detail 接口
    # =========================================================
    if goods_sign:
        print(f"[*] 触发策略 A: 使用 goods_sign 进行 Detail 查询...")
        params = {
            "goods_sign": goods_sign,
            "pid": pid,
            "custom_parameters": custom_params_str
        }
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.detail", params)
        if res and "goods_detail_response" in res:
            goods_list = res["goods_detail_response"].get("goods_details", [])
            if goods_list:
                return format_unified_response(goods_list[0], source_type="detail_api")
        return {"error": "Detail 接口查询失败或商品不存在"}

    # =========================================================
    # 策略 B: 只有 goods_id，走 洗链 + Search 截胡策略
    # =========================================================
    elif goods_id:
        print(f"[*] 触发策略 B: 使用 goods_id 进行洗链 + Search 查询...")

        # 1. 裸 ID 转官方短链
        zs_params = {
            "source_url": f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_id}",
            "pid": pid,
            "custom_parameters": custom_params_str
        }
        zs_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.zs.unit.url.gen", zs_params)

        if not zs_res:
            return {"error": "转链接口洗白失败"}

        zs_data = zs_res.get("goods_zs_unit_generate_response", {})
        short_url = zs_data.get("mobile_short_url") or zs_data.get("short_url")

        if not short_url:
            return {"error": "无法获取官方短链，洗链失败"}

        print(f"    -> 洗出短链: {short_url}，正在截取 Search 核心数据...")

        # 2. 短链入 Search 接口截获数据
        search_params = {
            "keyword": short_url,
            "pid": pid,
            "custom_parameters": custom_params_str,
            "page": 1,
            "page_size": 10
        }
        search_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", search_params)

        if search_res and "goods_search_response" in search_res:
            goods_list = search_res["goods_search_response"].get("goods_list", [])
            if goods_list:
                return format_unified_response(goods_list[0], source_type="search_api")
        return {"error": "Search 接口解析短链失败或商品已下架"}

    # =========================================================
    # 异常: 参数缺失
    # =========================================================
    else:
        return {"error": "必须提供 goods_sign 或 goods_id 中的至少一个"}


if __name__ == "__main__":
    # 配置你的信息 (这里模拟读取)
    pdd_client_id = get_config("pdd_client_id")
    pdd_client_secret = get_config("pdd_client_secret")
    pdd_pid = get_config("pdd_pid")
    pdd_custom_parameters = get_config("pdd_custom_parameters")

    print("\n========= 测试场景 1: 传入 goods_sign =========")
    result_sign = get_unified_pdd_goods_info(
        client_id=pdd_client_id,
        client_secret=pdd_client_secret,
        pid=pdd_pid,
        goods_sign="E9j2RLMbqvlgMvVVwvfAgIglclkvdd4B_JLOCUS6xv"
    )
    if "error" not in result_sign:
        print(f"[成功] 获取到商品: {result_sign['goods_name']}")
        print(f"数据来源: {result_sign['_source_api']}")
        print(f"统一价格(分): {result_sign['min_group_price']}")
    else:
        print(result_sign)

    print("\n========= 测试场景 2: 传入 goods_id =========")
    result_id = get_unified_pdd_goods_info(
        client_id=pdd_client_id,
        client_secret=pdd_client_secret,
        pid=pdd_pid,
        goods_id="925334695667",
        uid=pdd_custom_parameters
    )
    if "error" not in result_id:
        print(f"[成功] 获取到商品: {result_id['goods_name']}")
        print(f"数据来源: {result_id['_source_api']}")
        print(f"统一价格(分): {result_id['min_group_price']}")
        print(f"解析出的纯净Sign: {result_id['goods_sign']}")
    else:
        print(result_id)