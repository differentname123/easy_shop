from typing import Any, Dict, Optional
import requests


def search_pdd_goods(
    keyword: str,
    page: int = 1,
    sort_type: str = "",
    uid: str = "1249921860559478786",
    user_id: str = "2113892012536152068",
    device_unique_id: str = "2042858551363026946",
) -> Optional[Dict[str, Any]]:
    """搜索拼多多商品列表

    :param keyword: 搜索关键词（如：'可乐500ml'）
    :param page: 页码
    :param sort_type: 排序类型
    :param uid: 业务UID
    :param user_id: 用户ID
    :param device_unique_id: 设备唯一ID
    :return: 接口返回的JSON字典
    """
    url = "https://cmssvr.52youzai.com/pdd/goods/list"

    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "authorization": "null",
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": "http://fn5owt.3-34.cn",
        "pragma": "no-cache",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    payload = {
        "uid": uid,
        "userId": user_id,
        "keyword": keyword,
        "sort_type": sort_type,
        "page": page,
        "deviceType": "work_wechat",
        "deviceUniqueId": device_unique_id,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()  # 如果状态码不是 200 会抛出异常
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"请求发生异常: {e}")
        return None


# ================== 使用示例 ==================
if __name__ == "__main__":
    # 查第1页的可乐
    result = search_pdd_goods(keyword="可乐500ml", page=1)
    print(result)

    # 查第2页的雪碧
    # result2 = search_pdd_goods(keyword="雪碧", page=2)
    # print(result2)