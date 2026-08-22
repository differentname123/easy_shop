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
import json
import hashlib
import requests
from common.common_utils import get_config


def call_pdd_api(client_id, client_secret, api_type, business_params):
    """
    基础的拼多多 API 请求与自动签名器
    """
    url = "https://gw-api.pinduoduo.com/api/router"

    # 1. 构造公共参数
    params = {
        "type": api_type,
        "client_id": client_id,
        "timestamp": str(int(time.time())),
        "data_type": "JSON",
    }

    params.update(business_params)

    # 2. 签名生成 (MD5 签名算法)
    sorted_keys = sorted(params.keys())
    sign_str = client_secret
    for key in sorted_keys:
        sign_str += f"{key}{params[key]}"
    sign_str += client_secret

    params["sign"] = hashlib.md5(sign_str.encode('utf-8')).hexdigest().upper()

    # 3. 发起请求
    try:
        response = requests.post(url, json=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[请求异常] 调用接口 {api_type} 发生网络层面错误: {str(e)}")
        return None


def query_goods_info_best_practice(client_id, client_secret, pid, goods_id):
    """
    黄金流程：Search接口转码 -> Detail接口拉取详情
    """
    print(f"\n[阶段 1] 正在调用 search 接口转换 goods_id: {goods_id} ...")

    # ==============================================================
    # 步骤一：通过 search 接口获取 goods_sign 和 search_id
    # ==============================================================
    search_params = {
        "keyword": str(goods_id),
        "pid": pid,  # <--- 必须传入推广位ID进行备案校验
        "page": 1,
        "page_size": 10
    }

    search_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", search_params)

    if not search_res or "error_response" in search_res:
        return {"error": "Search 接口请求失败", "details": search_res}

    goods_list = search_res.get("goods_search_response", {}).get("goods_list", [])
    if not goods_list:
        return {"error": f"转换失败！多多进宝商品库中未找到ID为 {goods_id} 的商品(可能已下架/非分销商品)"}

    # 精确匹配（防止拼多多的模糊搜索混入无关商品）
    target_goods = None
    for item in goods_list:
        if str(item.get("goods_id")) == str(goods_id):
            target_goods = item
            break

    if not target_goods:
        target_goods = goods_list[0]

    goods_sign = target_goods.get("goods_sign")
    search_id = target_goods.get("search_id")

    if not goods_sign:
        return {"error": "在返回的数据中未能提取到 goods_sign 字段"}

    print(f"[阶段 1] 转换成功！获取到 goods_sign: {goods_sign}")
    print(f"[阶段 2] 正在带着 goods_sign 调用 detail 接口拉取完整详情...")

    # ==============================================================
    # 步骤二：带着提取到的凭证，调用 detail 获取全量信息
    # ==============================================================
    detail_params = {
        "goods_sign": goods_sign,
        "pid": pid  # 传入 pid 有助于获取你该推广位专属的精确佣金比例
    }
    if search_id:
        detail_params["search_id"] = search_id

    detail_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.detail", detail_params)

    if not detail_res or "error_response" in detail_res:
        return {"error": "Detail 接口请求失败", "details": detail_res}

    goods_detail_list = detail_res.get("goods_detail_response", {}).get("goods_details", [])

    if goods_detail_list:
        final_detail = goods_detail_list[0]
        final_detail["_search_id_trace"] = search_id
        return final_detail
    else:
        return {"error": "Detail 接口未返回有效的 goods_details 结构"}


if __name__ == "__main__":
    pdd_client_id = get_config("pdd_client_id")
    pdd_client_secret = get_config("pdd_client_secret")
    pdd_pid = get_config("pdd_pid")

    test_goods_id = "991951392065"

    result = query_goods_info_best_practice(
        client_id=pdd_client_id,
        client_secret=pdd_client_secret,
        pid=pdd_pid,
        goods_id=test_goods_id
    )

    print("\n====== 最终聚合查询结果 ======")
    print(json.dumps(result, ensure_ascii=False, indent=4))