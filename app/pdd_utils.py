# -*- coding: utf-8 -*-
"""
=============================================================================
[顶层数据流] 拼多多商品详情大一统查询器 & 批量洗链工具
=============================================================================
* [功能摘要]：对外屏蔽拼多多复杂的接口鉴权与多链路查询差异，提供统一的商品信息获取与转链（CPS佣金链接生成）能力。
* [输入数据]：PDD应用凭证(client_id/secret)、推广位标识[pid]、商品唯一标识[goods_sign]或[goods_id]、原始商品链接。
* [数据流转/交互]：
    1. 接收标识参数 -> 剔除空值 -> 字典排序后附带secret进行MD5签名 -> 包装为JSON POST至PDD网关。
    2. [查询链路]：优先使用 goods_sign 走官方 Detail 接口；若只有 goods_id，则先过 url.gen 接口洗出短链，再用短链过 search 接口“截胡”商品信息。
    3. [转链链路]：接收外部原始链接，注入自定义追踪参数(uid)，请求 url.gen 接口生成带个人佣金标识的全新短链。
* [输出数据]：经过标准化清洗、字段对齐的统一商品信息字典 (包含预估佣金、DSR评分等) 或 转链结果字典。遇到异常抛出包含明确 error 信息的结构体。
=============================================================================
"""

import time
import json
import hashlib
import requests
from common.common_utils import get_config, setup_logger

logger = setup_logger(app_name="nana_pdd_utils")


def call_pdd_api(client_id, client_secret, api_type, business_params):
    """
    统一的拼多多 API 请求与自动签名器
    """
    url = "https://gw-api.pinduoduo.com/api/router"

    # 1. 组装初始参数（过滤空值）
    raw_params = {
        "type": api_type,
        "client_id": client_id,
        "timestamp": str(int(time.time())),
        "data_type": "JSON",
        "version": "V1"
    }
    raw_params.update({k: v for k, v in business_params.items() if v is not None})

    # 2. [核心修复2.0]：全参数严格字符串化
    # 为了防止 requests.post 自动发送 Python 原生 List/Bool 导致拼多多 Java 网关解析差异，
    # 必须把所有要发送的参数彻底“固化”为拼多多标准格式的字符串！
    params = {}
    for key, val in raw_params.items():
        if isinstance(val, bool):
            params[key] = "true" if val else "false"
        elif isinstance(val, (list, dict)):
            params[key] = json.dumps(val, ensure_ascii=False, separators=(',', ':'))
        else:
            # 即使是数字 (int)，也统一转为字符串发送
            params[key] = str(val)

    # 3. 按最终的纯字符串字典计算签名
    sign_str = client_secret
    for key in sorted(params.keys()):
        sign_str += f"{key}{params[key]}"
    sign_str += client_secret

    params["sign"] = hashlib.md5(sign_str.encode('utf-8')).hexdigest().upper()

    # 4. 发起请求 (此时 params 里全是彻头彻尾的纯 String，网关接收零歧义)
    try:
        response = requests.post(
            url,
            json=params,
            headers={'Content-Type': 'application/json;charset=utf-8'},
            timeout=10
        )
        response.raise_for_status()
        result = response.json()
    except Exception as e:
        raise RuntimeError(f"网络层崩溃: {str(e)}")

    # 5. 错误捕获透传
    if "error_response" in result:
        err_resp = result['error_response']
        error_msg = err_resp.get('error_msg', '未知业务错误')
        sub_msg = err_resp.get('sub_msg', '')
        error_code = err_resp.get('error_code', '')
        sub_code = err_resp.get('sub_code', '')

        full_error = f"[{error_code}:{sub_code}] {error_msg} - {sub_msg}".strip(" -:")
        raise RuntimeError(f"PDD接口拒接: {full_error}")

    return result
def format_unified_response(raw_data, source_type):
    """
    统一数据清洗与格式化模块
    """
    if not raw_data:
        return None

    min_group_price = raw_data.get("min_group_price", 0)
    coupon_discount = raw_data.get("coupon_discount", 0)
    promotion_rate = raw_data.get("promotion_rate", 0)

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
        "min_group_price": min_group_price,
        "min_normal_price": raw_data.get("min_normal_price", min_group_price),
        "promotion_rate": promotion_rate,
        "estimated_commission": estimated_commission,
        "sales_tip": raw_data.get("sales_tip", "0"),
        "unified_tags": raw_data.get("unified_tags", []),
        "mall_name": raw_data.get("mall_name", ""),
        "merchant_type": raw_data.get("merchant_type", 1),
        "desc_txt": raw_data.get("desc_txt", "平"),
        "serv_txt": raw_data.get("serv_txt", "平"),
        "lgst_txt": raw_data.get("lgst_txt", "平"),
        "has_coupon": raw_data.get("has_coupon", False),
        "coupon_discount": coupon_discount,
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


def get_unified_pdd_goods_info(client_id, client_secret, pid, goods_sign=None, goods_id=None, uid=None):
    """
    大一统商品信息查询函数
    """
    if not goods_sign and not goods_id:
        return {"error": "缺乏核心参数: 必须提供 [goods_sign] 或 [goods_id] 至少一项"}

    custom_params_str = json.dumps({"uid": str(uid)}, separators=(',', ':')) if uid else None

    if goods_sign:
        try:
            res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.detail", {
                "goods_sign": goods_sign,
                "pid": pid,
                "custom_parameters": custom_params_str
            })
            goods_list = res.get("goods_detail_response", {}).get("goods_details", [])
            if goods_list:
                return format_unified_response(goods_list[0], source_type="detail_api")
            return {"error": "Detail接口查询为空，可能商品已下架或无推广计划"}
        except Exception as e:
            return {"error": f"标准Detail查询失败: {str(e)}"}

    try:
        zs_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.zs.unit.url.gen", {
            "source_url": f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_id}",
            "pid": pid,
            "custom_parameters": custom_params_str
        })

        zs_data = zs_res.get("goods_zs_unit_generate_response", {})
        short_url = zs_data.get("mobile_short_url") or zs_data.get("short_url")
        if not short_url:
            return {"error": "洗链提取短链失败(无短链返回)"}

        search_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", {
            "keyword": short_url,
            "pid": pid,
            "custom_parameters": custom_params_str,
            "page": 1,
            "page_size": 10
        })

        goods_list = search_res.get("goods_search_response", {}).get("goods_list", [])
        if goods_list:
            return format_unified_response(goods_list[0], source_type="search_api")
        return {"error": "Search接口未命中有效商品(可能已下架或被风控过滤)"}

    except Exception as e:
        return {"error": f"洗链截胡策略(Strategy B)执行中断: {str(e)}"}


def batch_convert_pdd_urls(client_id, client_secret, pid, url_list, uid=None, generate_short_link=False):
    """
    批量洗链（转链）函数
    """
    custom_params_str = json.dumps({"uid": str(uid)}, separators=(',', ':')) if uid else None
    result_dict = {}

    for original_url in url_list:
        if not original_url or not isinstance(original_url, str):
            continue

        try:
            business_params = {
                "pid": pid,
                "source_url": original_url,
                "custom_parameters": custom_params_str,
                "generate_short_link": True if generate_short_link else None
            }

            res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.zs.unit.url.gen", business_params)
            response_data = res.get("goods_zs_unit_generate_response", {})
            best_h5_url = response_data.get("mobile_short_url") or response_data.get("short_url")

            if best_h5_url:
                result_dict[original_url] = {
                    "status": "success",
                    "h5_jump_url": best_h5_url,
                    "raw_data": response_data
                }
            else:
                result_dict[original_url] = {
                    "status": "error",
                    "error_msg": "接口返回正常但未包含可用的 short_url"
                }

        except Exception as e:
            # [修改点] 移除零碎日志，包裹进返回字典
            result_dict[original_url] = {
                "status": "error",
                "error_msg": f"转链API交互崩溃: {str(e)}"
            }

    return result_dict


def verify_and_convert_pdd_goods(client_id, client_secret, pid, original_url, goods_sign=None, goods_id=None, uid=None,
                                 generate_short_link=False):
    """
    聚合功能：拦截低价/无用商品，验证统一价格大于0后才进行实质转链。
    [核心优化] 此处收束所有错误日志输出，确保多进程下每次调用最多只产出“唯一的一行日志”。
    """
    # 1. 探针：获取商品详细信息
    goods_info = get_unified_pdd_goods_info(client_id, client_secret, pid, goods_sign, goods_id, uid)

    if "error" in goods_info:
        msg = f"探针查询失败 | 商品ID: {goods_id} | 错误详情: {goods_info['error']}"
        logger.error(f"[聚合转链-失败] ❌ {msg}")
        return {"status": "error", "error_msg": msg, "goods_info": goods_info}

    # 2. 拦截器：验证价格与商品真实性（识别风控假数据）
    min_group_price = goods_info.get("min_group_price", 0)
    goods_name = goods_info.get('goods_name', '').strip()

    # [核心修改点]：如果名字为空且价格为0，极大概率是遇到了API返回空数据的风控拦截（防爬机制）
    if min_group_price <= 0 or not goods_name:
        msg = f"价格/风控异常拦截 | 商品: {goods_id} 【{goods_name}】 | 当前价格: [{min_group_price}分] (疑遭风控脱敏或商品下架)"
        logger.error(f"[聚合转链-拦截] ❌ {msg}")
        return {"status": "error", "error_msg": msg, "goods_info": goods_info}

    # 3. 执行核心转链
    convert_result_dict = batch_convert_pdd_urls(
        client_id, client_secret, pid,
        url_list=[original_url],
        uid=uid,
        generate_short_link=generate_short_link
    )
    url_convert_info = convert_result_dict.get(original_url, {})

    # 4. 结果组装
    if url_convert_info.get("status") == "success":
        # 成功时，由上游主流程去打日志，这里不打，保持整洁
        return {
            "status": "success",
            "msg": "价格验证通过且转链成功",
            "goods_info": goods_info,
            "convert_info": url_convert_info
        }

    # 转链失败记录日志
    err_msg = url_convert_info.get('error_msg', '未知错误')
    msg = f"探针通过但核心转链失败 | 原始链接: <{original_url[:30]}...> | 排查: {err_msg}"
    logger.error(f"[聚合转链-失败] ❌ {msg}")

    return {
        "status": "error",
        "error_msg": msg,
        "goods_info": goods_info,
        "convert_info": url_convert_info
    }


def search_pdd_goods_by_keyword(client_id, client_secret, pid, search_key, limit_count=0, uid=None):
    """
    根据指定关键词搜索多多进宝商品列表，支持数量限制与自动翻页获取。

    :param search_key: 搜索关键词 (对应API的 keyword)
    :param limit_count: 限制获取的数量。0 表示一直翻页直到没有数据，大于0表示达到该数量即停止。
    :param uid: 自定义参数，用于转链追踪
    :return: 包含格式化商品信息的列表，或包含 error 信息的字典
    """
    if not search_key:
        return {"error": "搜索关键词不能为空"}

    custom_params_str = json.dumps({"uid": str(uid)}, separators=(',', ':')) if uid else None

    all_formatted_goods = []
    current_page = 1
    # 官方默认是100，这里我们每次请求100条以最大化单次请求效率，减少API交互次数
    page_size = 100
    list_id = None

    while True:
        business_params = {
            "keyword": search_key,
            "pid": pid,
            "page": current_page,
            "page_size": page_size,
            "with_coupon": True  # 默认只查有券商品，可根据实际业务修改为 False
        }

        if custom_params_str:
            business_params["custom_parameters"] = custom_params_str

        # 根据官方文档，请求商品分页数>1时，list_id 必填
        if current_page > 1 and list_id:
            business_params["list_id"] = list_id

        try:
            search_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", business_params)
        except Exception as e:
            # 如果是第一页报错，直接上抛错误；如果是翻页过程报错，保留已获取的数据并中断
            if current_page == 1:
                return {"error": f"关键词搜索崩溃: {str(e)}"}
            else:
                logger.warning(f"搜索翻页中断(已获取{len(all_formatted_goods)}条): {str(e)}")
                break

        resp_data = search_res.get("goods_search_response", {})
        goods_list = resp_data.get("goods_list", [])

        # 提取并保存第一页返回的 list_id，用于后续翻页锁定上下文
        if current_page == 1:
            list_id = resp_data.get("list_id")

        # 若当前页没有数据了，说明已经遍历完所有商品，退出循环
        if not goods_list:
            break

        # 数据清洗并加入总集合
        for goods in goods_list:
            formatted_item = format_unified_response(goods, source_type="keyword_search")
            if formatted_item:
                all_formatted_goods.append(formatted_item)

        # 数量超限检测 (limit_count 为 0 时不限制)
        if limit_count > 0 and len(all_formatted_goods) >= limit_count:
            # 切片截取到精确限制的数量
            all_formatted_goods = all_formatted_goods[:limit_count]
            break

        current_page += 1

        # 增加微小的睡眠防止翻页过快触发 API 频控 (70031: 调用过于频繁)
        time.sleep(0.2)

    return {
        "status": "success",
        "msg": f"成功搜索到 {len(all_formatted_goods)} 条商品",
        "data": all_formatted_goods
    }


def generate_pdd_authority_url(client_id, client_secret, pid, uid=None):
    """
    [新增] 生成用于账号/PID授权备案的专属链接 (专治 60001 报错)
    """
    business_params = {
        "p_id_list": [pid],
        "channel_type": 10,  # 魔法数字：10 代表渠道备案授权链接
        "generate_we_app": True,  # 生成小程序链接，方便你在微信里一键点开
        "generate_short_url": True
    }

    # 如果你想把当前配置文件里的 UID 也一起合法化，就把它带上
    if uid:
        business_params["custom_parameters"] = json.dumps({"uid": str(uid)}, separators=(',', ':'))

    try:
        # 调用的是 rp.prom.url.generate（营销工具推广链接生成）
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.rp.prom.url.generate", business_params)
        url_list = res.get("rp_promotion_url_generate_response", {}).get("url_list", [])

        if url_list:
            return {"status": "success", "url_info": url_list[0]}
        return {"status": "error", "error_msg": "接口调用成功，但未返回可用链接"}
    except Exception as e:
        return {"status": "error", "error_msg": f"生成备案链接失败: {str(e)}"}


def get_pdd_recommend_goods(client_id, client_secret, channel_type=5, limit_count=0,
                            cat_id=None, goods_sign_list=None, activity_tags=None, goods_img_type=None, uid=None):
    """
    自动翻页获取多多进宝商品推荐列表 (API: pdd.ddk.goods.recommend.get)

    :param limit_count: 限制获取的数量。0 表示一直翻页直到没有数据，大于0表示达到该数量即停止。
    :param channel_type: 进宝频道推广商品: 1-今日销量榜, 3-相似推荐, 4-猜你喜欢, 5-实时热销榜(默认), 6-实时收益榜
    :param cat_id: 猜你喜欢场景的商品类目ID
    :param goods_sign_list: 商品goodsSign列表，相似商品推荐场景(channel_type=3)时必传
    :param activity_tags: 活动商品标记数组，例：[4,7] (4-秒杀，7-百亿补贴)
    :return: 包含统一格式化后商品列表的字典
    """
    custom_params_str = json.dumps({"uid": str(uid)}, separators=(',', ':')) if uid else None

    all_formatted_goods = []
    current_offset = 0
    # 推荐接口单次请求的数据量。适度拉大可以减少网络交互次数
    batch_limit = 50
    list_id = None

    while True:
        business_params = {
            "channel_type": channel_type,
            "limit": batch_limit,
            "offset": current_offset,
            "cat_id": cat_id,
            "goods_sign_list": goods_sign_list,
            "activity_tags": activity_tags,
            "goods_img_type": goods_img_type,
            "custom_parameters": custom_params_str
        }

        # 翻页时带上前一页返回的 list_id 以保证上下文不重复
        if current_offset > 0 and list_id:
            business_params["list_id"] = list_id

        try:
            res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.recommend.get", business_params)
        except Exception as e:
            if current_offset == 0:
                return {"error": f"商品推荐接口调用崩溃: {str(e)}"}
            else:
                logger.warning(f"推荐翻页中断(已获取{len(all_formatted_goods)}条): {str(e)}")
                break

        resp_data = res.get("goods_basic_detail_response", {})
        goods_list = resp_data.get("list", [])

        # 提取并保存第一页返回的 list_id
        if current_offset == 0:
            list_id = resp_data.get("list_id")

        # 数据拉空，跳出循环
        if not goods_list:
            break

        # 清洗数据
        for goods in goods_list:
            formatted_item = format_unified_response(goods, source_type=f"recommend_api_ch{channel_type}")
            if formatted_item:
                all_formatted_goods.append(formatted_item)

        # 数量超限检测
        if limit_count > 0 and len(all_formatted_goods) >= limit_count:
            all_formatted_goods = all_formatted_goods[:limit_count]
            break

        # 累加偏移量，准备拉取下一页
        current_offset += batch_limit
        time.sleep(0.2)  # 防封控短时休眠

    return {
        "status": "success",
        "msg": f"成功获取 {len(all_formatted_goods)} 条推荐商品",
        "data": all_formatted_goods
    }


if __name__ == "__main__":
    # 配置信息读取
    pdd_client_id = get_config("nana_pdd_client_id")
    pdd_client_secret = get_config("nana_pdd_client_secret")
    pdd_pid = get_config("nana_pdd_pid")

    # ==================================================================================================
    # 🚀 商品推荐 API 测试
    # ==================================================================================================
    logger.info("======== 🚀 开始测试多多进宝商品推荐 (实时热销榜) ========")

    # 测试参数：获取实时热销榜 (channel_type=5) 的前 5 个商品
    test_channel = 5
    limit_count = 5

    recommend_result = get_pdd_recommend_goods(
        client_id=pdd_client_id,
        client_secret=pdd_client_secret,
        channel_type=test_channel,
        limit_count=limit_count,
    )

    if "error" in recommend_result:
        logger.error(f"❌ 推荐测试失败: {recommend_result['error']}")
    else:
        rec_goods_list = recommend_result.get("data", [])
        total_count = recommend_result.get("total", 0)
        returned_list_id = recommend_result.get("list_id", "")
        returned_search_id = recommend_result.get("search_id", "")

        logger.info(f"✅ 推荐测试成功: 成功获取 {len(rec_goods_list)} 条商品 (该榜单总量约: {total_count})。")
        logger.info(f"   [翻页凭证] list_id: {returned_list_id} | search_id: {returned_search_id}")

        for idx, item in enumerate(rec_goods_list, start=1):
            # 获取统一清洗后的关键字段
            goods_name = item.get("goods_name", "未知商品")
            price_yuan = item.get("min_group_price", 0) / 100.0  # 拼多多价格单位是分，转为元
            commission_yuan = item.get("estimated_commission", 0) / 100.0  # 佣金单位是分，转为元
            sales_tip = item.get("sales_tip", "0")
            has_coupon = "是" if item.get("has_coupon") else "否"
            coupon_amount = item.get("coupon_discount", 0) / 100.0

            # 为了控制台输出整洁，商品名称截断到最长25个字符
            display_name = goods_name if len(goods_name) <= 25 else goods_name[:25] + "..."

            logger.info(
                f"  [{idx:02d}] {display_name} \n"
                f"       ├─ 拼团价: {price_yuan:.2f}元 | 预估佣金: {commission_yuan:.2f}元\n"
                f"       └─ 销量: {sales_tip} | 有券: {has_coupon} (券面额: {coupon_amount:.2f}元)"
            )

    logger.info("================ 推荐测试结束 ================")

    # # ==================================================================================================
    # # 🚨🚨🚨 【血泪教训：极其重要的 PDD 风控参数与授权说明】 🚨🚨🚨
    # # 业务场景：本系统用于【公开网站导购橱窗】，访客点击直接跳转拼多多购买，系统不负责给单兵访客返现。
    # # 核心铁律：所有接口（搜索、转链、生成备案）的 uid 参数必须保持为 None！绝不可传入固定值！
    # #
    # # 【风控踩坑复盘总结】：
    # # ❌ 封禁大坑 (报 99999)：若传入固定 uid，PDD 会判定为【私域定制专属链接】。一个号后台批量生成几千个
    # #                         专属链接给各种不同的人买，直接被 AI 判定为违规代购/黑产机器人，账号封禁！
    # # ❌ 裸奔大坑 (报 60001)：删掉 uid 后变为【公域网站链接】，但如果该 PID(推广位) 未经站长本人实名登记，
    # #                         PDD 会判定为无主黑户，直接拦截并报“未授权备案”。
    # # ✅ 终极完美闭环：
    # #    1. 代码里强行设 uid = None。
    # #    2. 运行下面这段代码，生成一条属于站长的【渠道授权链接】。
    # #    3. 站长自己去微信里点开，点击一次“同意授权”（这辈子只需点这一次，相当于给 PID 开光登记）。
    # #    4. 完成后，网站访客再点击我们用 uid=None 生成的商品链接时，0阻力丝滑直达商品页，绝不会弹授权！
    # # ==================================================================================================
    #
    # uid_to_auth = None  # 🛡️ 护身符：强制置空，宣告我们是合法合规的公开网站媒体
    #
    # logger.info("======== 🚀 第一步：开始生成站长专属的 PID 渠道授权备案链接 ========")
    # # 这里调用 pdd.ddk.rp.prom.url.generate (channel_type=10) 专用于给 PID 备案登记
    # auth_res = generate_pdd_authority_url(pdd_client_id, pdd_client_secret, pdd_pid, uid=uid_to_auth)
    #
    # if auth_res["status"] == "success":
    #     url_info = auth_res["url_info"]
    #     logger.info("✅ 备案链接生成成功！请站长【务必】亲自执行以下操作以彻底解除 60001 拦截：")
    #     logger.info(
    #         f"👉 H5 短链接 (复制发给微信文件传输助手): {url_info.get('mobile_short_url') or url_info.get('short_url')}")
    #     logger.info(f"👉 小程序路径 (若H5打不开，用此路径): {url_info.get('we_app_page_path')}")
    #
    #     # ⚠️ 特别提醒站长本人的操作指南
    #     logger.info("-" * 60)
    #     logger.info("【最终激活指北】：")
    #     logger.info("1. 复制上面的 H5 短链接。")
    #     logger.info("2. 打开手机微信，发给【文件传输助手】并点击打开。")
    #     logger.info("3. 页面弹出『允许获取你的推广信息』时，点击【同意】。")
    #     logger.info("4. 只要点完这一下，本 PID 永久激活！后续转链代码畅通无阻，访客买单佣金全拿！")
    #     logger.info("-" * 60)
    # else:
    #     logger.error(f"❌ 生成备案链接失败，请检查网络或应用权限：{auth_res['error_msg']}")


    test_keyword = "可乐"
    test_limit = 5
    logger.info(f"======== 🚀 开始测试关键词搜索 | 关键词: [{test_keyword}] | 限制获取: [{test_limit}条] ========")

    search_result = search_pdd_goods_by_keyword(
        client_id=pdd_client_id,
        client_secret=pdd_client_secret,
        pid=pdd_pid,
        search_key=test_keyword,
        limit_count=test_limit,
    )

    if "error" in search_result:
        logger.error(f"❌ 搜索测试失败: {search_result['error']}")
    else:
        goods_list = search_result.get("data", [])
        logger.info(f"✅ 搜索测试成功: 共获取到 {len(goods_list)} 条有效商品。详情如下：")

        for idx, item in enumerate(goods_list, start=1):
            # 获取统一清洗后的关键字段
            goods_name = item.get("goods_name", "未知商品")
            price_yuan = item.get("min_group_price", 0) / 100.0  # 拼多多价格单位是分，转为元
            commission_yuan = item.get("estimated_commission", 0) / 100.0  # 佣金单位是分，转为元
            sales_tip = item.get("sales_tip", "0")
            has_coupon = "是" if item.get("has_coupon") else "否"
            coupon_amount = item.get("coupon_discount", 0) / 100.0

            # 为了控制台输出整洁，商品名称截断到最长25个字符
            display_name = goods_name if len(goods_name) <= 25 else goods_name[:25] + "..."

            logger.info(
                f"  [{idx:02d}] {display_name} \n"
                f"       ├─ 拼团价: {price_yuan:.2f}元 | 预估佣金: {commission_yuan:.2f}元\n"
                f"       └─ 销量: {sales_tip} | 有券: {has_coupon} (券面额: {coupon_amount:.2f}元)"
            )

    logger.info("================ 搜索测试结束 ================")






    original_target_url = "https://mobile.pinduoduo.com/goods.html?goods_id=627575562243"

    # 执行聚合转链业务
    agg_result = verify_and_convert_pdd_goods(
        client_id=pdd_client_id,
        client_secret=pdd_client_secret,
        pid=pdd_pid,
        original_url=original_target_url,
        goods_id="980279341123",
    )

    # 外部仅关心成功时的日志。失败的已经在内部打过单行警告日志了。
    if agg_result["status"] == "success":
        goods = agg_result['goods_info']
        logger.info(
            f"[主流程] ✅ 转链成功 | 商品: 【{goods['goods_name']}】 | 验证价格: [{goods['min_group_price']}分] | 预估佣金: [{goods['estimated_commission']}分] | 转链地址: <{agg_result['convert_info']['h5_jump_url']}>")