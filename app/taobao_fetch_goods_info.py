from datetime import datetime
import hashlib
import json
import time
import requests

from common.common_utils import get_config

# ----------------- 1. 配置信息 -----------------
APP_KEY = get_config("myself_taobao_app_key")
APP_SECRET = get_config("myself_taobao_app_secret")
ADZONE_ID = get_config("myself_taobao_pid")
GATEWAY_URL = "https://eco.taobao.com/router/rest"


def generate_sign(params: dict, app_secret: str) -> str:
    valid_items = [
        (k, str(v)) for k, v in params.items() if v is not None and k != "sign"
    ]
    sorted_items = sorted(valid_items, key=lambda x: x[0])
    query_str = (
        app_secret + "".join(f"{k}{v}" for k, v in sorted_items) + app_secret
    )
    return hashlib.md5(query_str.encode("utf-8")).hexdigest().upper()


def top_execute(method: str, biz_params: dict) -> dict:
    try:
        params = {
            "method": method,
            "app_key": APP_KEY,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "format": "json",
            "v": "2.0",
            "sign_method": "md5",
        }
        params.update(biz_params)
        params["sign"] = generate_sign(params, APP_SECRET)

        response = requests.post(GATEWAY_URL, data=params, timeout=10)
        return response.json()
    except Exception as e:
        return {"error_response": {"sub_msg": str(e)}}


def search_items(keyword: str, desire_count: int = 0) -> list:
    """使用已拥有的 27939 权限包进行搜索

    :param keyword: 搜索关键词
    :param desire_count: 需要的商品数量，如果是 0 则查询所有数据
    :return: 商品列表（发生任何异常至少返回空列表 []，保证不崩溃）
    """
    all_results = []
    try:
        if desire_count < 0:
            return []

        page_no = 1
        max_page_limit = 100  # 平台翻页上限保护（防止死循环）

        while True:
            # 计算当前页请求的条数
            if desire_count == 0:
                current_page_size = 100
            else:
                remain = desire_count - len(all_results)
                if remain <= 0:
                    break
                current_page_size = min(remain, 100)

            biz_params = {
                "q": keyword,
                "adzone_id": ADZONE_ID,
                "page_no": page_no,
                "page_size": current_page_size,
            }
            res = top_execute(
                "taobao.tbk.dg.material.optional.upgrade", biz_params
            )

            # 校验响应结构及错误信息
            if not isinstance(res, dict) or "error_response" in res:
                err_msg = ""
                if isinstance(res, dict) and "error_response" in res:
                    err_msg = res["error_response"].get("sub_msg", "")
                if err_msg:
                    print(f"搜索提示: {err_msg}")
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
            if desire_count > 0 and len(all_results) >= desire_count:
                break

            page_no += 1
            if page_no > max_page_limit:
                break

            # 多页循环时微量休眠，防止触发高频流控
            time.sleep(0.1)

        if desire_count > 0:
            return all_results[:desire_count]
        return all_results

    except Exception as e:
        print(f"获取商品异常: {e}")
        return all_results if all_results else []


if __name__ == "__main__":
    keyword = "万志蓝"
    print(f"=== 正在搜索关键词: 【{keyword}】 ===")
    items = search_items(keyword, desire_count=0)

    if not items:
        print("未获取到结果，请检查。")
    else:
        for idx, item in enumerate(items, start=1):
            # 1. 基础信息
            basic = item.get("item_basic_info", {})
            title = basic.get("title")  # 完整标题
            shop_title = basic.get("shop_title")  # 店铺名
            brand_name = basic.get("brand_name")  # 品牌
            pict_url = basic.get("pict_url")  # 主图

            # 2. 价格与优惠信息
            price_info = item.get("price_promotion_info", {})
            reserve_price = price_info.get("reserve_price")  # 吊牌/原价
            zk_price = price_info.get("zk_final_price")  # 折扣在售价
            final_price = price_info.get(
                "final_promotion_price"
            )  # 券后到手价
            predict_price = price_info.get(
                "predict_rounding_up_price"
            )  # 凑单预估价

            # 3. 推广信息
            publish = item.get("publish_info", {})
            click_url = publish.get("click_url")  # 你的专属推广链接
            item_id = item.get("item_id")  # 加密商品ID

            print(f"\n[{idx}] 【{brand_name or '无品牌'}】{title}")
            print(f"    店铺: {shop_title}")
            print(f"    划线原价: {reserve_price} 元 | 现标价: {zk_price} 元")
            print(
                f"    🔥 最终到手价: {final_price} 元 (凑单低至: {predict_price} 元)"
            )
            print(f"    推广直达链接: https:{click_url}")