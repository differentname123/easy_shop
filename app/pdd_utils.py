# -*- coding: utf-8 -*-
""":authors:
    zhuxiaohu
:create_date:
    2026/8/22 14:27
:last_date:
    2026/8/22 14:27
:description:
    基于最新拼多多官方规范，使用 search + detail 级联查询商品完整详情
"""
import time
import hashlib
import requests
import json
from common.common_utils import get_config


def generate_pdd_sign(params, client_secret):
    """
    生成拼多多 API 请求签名 (sign)
    签名算法规则：
    1. 将所有参数按参数名的字母顺序 (ASCII 码) 升序排序。
    2. 把 client_secret 作为前后缀拼接排序后的参数键值对。
    3. 使用 MD5 进行加密，并转为大写。
    """
    # 过滤掉不需要参与签名的空值参数
    sign_params = {k: v for k, v in params.items() if v is not None}

    # 提取所有 key 并按照 ASCII 码升序排列
    keys = sorted(sign_params.keys())

    # 首部拼接 client_secret
    sign_str = client_secret
    for key in keys:
        val = sign_params[key]
        # PDD 的 bool 类型签名时通常需要转为小写字符串
        if isinstance(val, bool):
            val = "true" if val else "false"
        sign_str += f"{key}{val}"

    # 尾部拼接 client_secret
    sign_str += client_secret

    # 进行 MD5 加密并转大写
    return hashlib.md5(sign_str.encode('utf-8')).hexdigest().upper()


def get_pdd_goods_detail(client_id, client_secret, goods_sign, pid=None, custom_parameters=None, search_id=None,
                         need_sku_info=False):
    """
    查询多多进宝商品详情 (pdd.ddk.goods.detail)

    :param client_id: POP分配给应用的client_id
    :param client_secret: 对应的 client_secret，用于签名
    :param goods_sign: 商品唯一标识（已代替下线的 goodsId）
    :param pid: 推广位id (非必填)
    :param custom_parameters: 自定义参数，JSON 字符串格式 (非必填)
    :param search_id: 搜索id，用于提高收益 (非必填)
    :param need_sku_info: 是否获取 sku 信息 (非必填)
    :return: 包含商品详情的 JSON 字典 / None
    """
    url = "https://gw-api.pinduoduo.com/api/router"

    # 1. 组装请求参数（公共参数 + 业务参数）
    params = {
        "type": "pdd.ddk.goods.detail",
        "client_id": client_id,
        "timestamp": str(int(time.time())),
        "data_type": "JSON",
        "version": "V1",
        # 业务参数
        "goods_sign": goods_sign
    }

    # 追加可选业务参数
    if pid:
        params["pid"] = pid
    if custom_parameters:
        params["custom_parameters"] = custom_parameters
    if search_id:
        params["search_id"] = search_id
    if need_sku_info:
        # API文档要求 BOOLEAN，但在传参和签名时我们转为明确的布尔值
        params["need_sku_info"] = True

        # 2. 计算并附加签名
    params["sign"] = generate_pdd_sign(params, client_secret)

    # 3. 发送 POST 请求
    try:
        headers = {'Content-Type': 'application/json;charset=utf-8'}
        response = requests.post(url, json=params, headers=headers, timeout=10)
        response.raise_for_status()

        result_json = response.json()

        # 4. 判断返回是否包含错误码
        if "error_response" in result_json:
            print(f"[-] API 接口返回错误: {result_json['error_response'].get('error_msg')}")
            print(f"[-] 完整错误信息: {result_json['error_response']}")
            return None

        return result_json.get("goods_detail_response", {})

    except requests.exceptions.RequestException as e:
        print(f"[-] 请求拼多多接口异常: {e}")
        return None


if __name__ == "__main__":
    # 获取基础配置
    pdd_client_id = get_config("pdd_client_id")
    pdd_client_secret = get_config("pdd_client_secret")
    pdd_pid = get_config("pdd_pid")
    pdd_custom_parameters = get_config("pdd_custom_parameters")

    # 模拟待查询的 goods_sign (需要替换为你真实要查询的商品标识)
    test_goods_sign = "E9j2RLMbqvlgMvVVwvfAgIglclkvdd4B_JLOCUS6xv"
    test_search_id = None

    print(f"[*] 开始查询商品详情, goods_sign: {test_goods_sign}")

    # 调用函数查询
    goods_detail_res = get_pdd_goods_detail(
        client_id=pdd_client_id,
        client_secret=pdd_client_secret,
        goods_sign=test_goods_sign,
        pid=pdd_pid,
        custom_parameters=pdd_custom_parameters,
        search_id=test_search_id,
        need_sku_info=False
    )

    # 打印结果处理
    if goods_detail_res and "goods_details" in goods_detail_res:
        goods_list = goods_detail_res["goods_details"]
        if goods_list:
            goods_info = goods_list[0]
            print(f"[+] 查询成功！")
            print(f"商品名称: {goods_info.get('goods_name')}")
            print(f"商品原价(分): {goods_info.get('min_normal_price')}")
            print(f"拼团价格(分): {goods_info.get('min_group_price')}")
            print(f"佣金比例(千分比): {goods_info.get('promotion_rate')}")
            if goods_info.get('has_coupon'):
                print(f"优惠券面额(分): {goods_info.get('coupon_discount')}")
        else:
            print("[-] 未查询到对应的商品详细信息，返回列表为空。")
    else:
        print("[-] 查询失败或返回数据结构异常。")