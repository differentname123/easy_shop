# 文件路径建议: app/taobao_utils.py
from datetime import datetime
import hashlib
import time
from typing import Optional
import requests

from common.common_utils import get_config, setup_logger

logger = setup_logger("taobao_utils")

# ----------------- 1. 默认配置信息 -----------------
DEFAULT_APP_KEY = get_config("myself_taobao_app_key")
DEFAULT_APP_SECRET = get_config("myself_taobao_app_secret")
DEFAULT_ADZONE_ID = get_config("myself_taobao_pid")
GATEWAY_URL = "https://eco.taobao.com/router/rest"


def generate_sign(params: dict, app_secret: str) -> str:
    """按淘宝开放平台规范生成 MD5 签名"""
    valid_items = [
        (k, str(v)) for k, v in params.items() if v is not None and k != "sign"
    ]
    sorted_items = sorted(valid_items, key=lambda x: x[0])
    query_str = (
        app_secret + "".join(f"{k}{v}" for k, v in sorted_items) + app_secret
    )
    return hashlib.md5(query_str.encode("utf-8")).hexdigest().upper()


def top_execute(
    method: str,
    biz_params: dict,
    app_key: Optional[str] = None,
    app_secret: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> dict:
    """执行淘宝开放平台 (TOP) API 请求"""
    use_app_key = app_key or DEFAULT_APP_KEY
    use_app_secret = app_secret or DEFAULT_APP_SECRET

    if not use_app_key or not use_app_secret:
        return {"error_response": {"sub_msg": "未配置淘宝联盟 app_key 或 app_secret"}}

    try:
        params = {
            "method": method,
            "app_key": use_app_key,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "format": "json",
            "v": "2.0",
            "sign_method": "md5",
        }
        params.update(biz_params)
        params["sign"] = generate_sign(params, use_app_secret)

        requester = session or requests
        response = requester.post(GATEWAY_URL, data=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error_response": {"sub_msg": str(e)}}


def search_items(
    keyword: str,
    desire_count: int = 0,
    app_key: Optional[str] = None,
    app_secret: Optional[str] = None,
    adzone_id: Optional[str] = None,
) -> list:
    """使用 27939 权限包 (taobao.tbk.dg.material.optional.upgrade) 搜索淘宝联盟商品

    :param keyword: 搜索关键词
    :param desire_count: 需要的商品数量，如果是 0 则查询所有数据
    :param app_key: 可选覆盖配置中的 app_key
    :param app_secret: 可选覆盖配置中的 app_secret
    :param adzone_id: 可选覆盖配置中的推广位 pid (adzone_id)
    :return: 商品列表（发生任何异常至少返回已获取列表或空列表 []，保证不崩溃）
    """
    res = search_taobao_goods_by_keyword(
        search_key=keyword,
        limit_count=desire_count,
        app_key=app_key,
        app_secret=app_secret,
        adzone_id=adzone_id,
    )
    return res.get("data", [])


def search_taobao_goods_by_keyword(
    search_key: str,
    limit_count: int = 0,
    app_key: Optional[str] = None,
    app_secret: Optional[str] = None,
    adzone_id: Optional[str] = None,
) -> dict:
    """与主应用风格对齐的搜索封装函数

    :return: {"status": "success" | "error", "data": list, "error": str}
    """
    all_results = []
    use_adzone_id = adzone_id or DEFAULT_ADZONE_ID

    if not search_key or not str(search_key).strip():
        return {"status": "error", "data": [], "error": "搜索关键词为空"}
    if limit_count < 0:
        return {"status": "error", "data": [], "error": "limit_count 不能小于 0"}

    page_no = 1
    max_page_limit = 100  # 平台翻页上限保护（防止死循环）
    error_msg = ""

    try:
        with requests.Session() as session:
            while True:
                if limit_count == 0:
                    current_page_size = 100
                else:
                    remain = limit_count - len(all_results)
                    if remain <= 0:
                        break
                    current_page_size = min(remain, 100)

                biz_params = {
                    "q": str(search_key).strip(),
                    "adzone_id": use_adzone_id,
                    "page_no": page_no,
                    "page_size": current_page_size,
                }
                res = top_execute(
                    "taobao.tbk.dg.material.optional.upgrade",
                    biz_params,
                    app_key=app_key,
                    app_secret=app_secret,
                    session=session,
                )

                # 校验响应结构及错误信息
                if not isinstance(res, dict) or "error_response" in res:
                    if isinstance(res, dict) and "error_response" in res:
                        err_obj = res["error_response"]
                        error_msg = err_obj.get("sub_msg") or err_obj.get("msg") or str(err_obj)
                    else:
                        error_msg = "响应格式非合法 JSON 字典"
                    if error_msg:
                        logger.warning("[淘宝API/提示] 关键词: [%s] | 页码: [%d] | 信息: %s",
                                       search_key, page_no, error_msg)
                    break

                page_items = (
                    res.get("tbk_dg_material_optional_upgrade_response", {})
                    .get("result_list", {})
                    .get("map_data", [])
                )

                if not page_items or not isinstance(page_items, list):
                    break

                all_results.extend(page_items)

                # 如果当前页返回的实际条数小于请求条数，说明数据已见底
                if len(page_items) < current_page_size:
                    break

                # 达到指定数量则退出
                if limit_count > 0 and len(all_results) >= limit_count:
                    break

                page_no += 1
                if page_no > max_page_limit:
                    break

                # 多页循环时微量休眠，防止触发高频流控
                time.sleep(0.15)

        if limit_count > 0:
            all_results = all_results[:limit_count]

        # 如果第一页就报错且没有任何数据，返回 error 状态；否则返回已采集到的数据
        if error_msg and not all_results:
            return {"status": "error", "data": [], "error": error_msg}
        return {"status": "success", "data": all_results, "error": error_msg}

    except Exception as e:
        logger.error("[淘宝API/异常] 关键词: [%s] 获取商品异常: %s", search_key, e)
        return {
            "status": "success" if all_results else "error",
            "data": all_results,
            "error": str(e),
        }


if __name__ == "__main__":
    keyword = "万志蓝"
    print(f"=== 正在搜索关键词: 【{keyword}】 ===")
    items = search_items(keyword, desire_count=0)

    if not items:
        print("未获取到结果，请检查。")
    else:
        for idx, item in enumerate(items, start=1):
            basic = item.get("item_basic_info", {})
            title = basic.get("title")
            shop_title = basic.get("shop_title")
            brand_name = basic.get("brand_name")

            price_info = item.get("price_promotion_info", {})
            reserve_price = price_info.get("reserve_price")
            zk_price = price_info.get("zk_final_price")
            final_price = price_info.get("final_promotion_price")
            predict_price = price_info.get("predict_rounding_up_price")

            publish = item.get("publish_info", {})
            click_url = publish.get("click_url", "")
            full_click_url = f"https:{click_url}" if click_url.startswith("//") else click_url

            print(f"\n[{idx}] 【{brand_name or '无品牌'}】{title}")
            print(f"    店铺: {shop_title}")
            print(f"    划线原价: {reserve_price} 元 | 现标价: {zk_price} 元")
            print(f"    🔥 最终到手价: {final_price} 元 (凑单低至: {predict_price} 元)")
            print(f"    推广直达链接: {full_click_url}")