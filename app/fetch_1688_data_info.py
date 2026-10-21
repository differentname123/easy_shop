import hashlib
import hmac
import json
import time
import requests

# ==========================================
# 1. 填入你的真实凭证
# ==========================================
APP_KEY = "1640547"
APP_SECRET = "CyRzp2ZIIiJK"  # 开放平台后台点击【查看】复制
ACCESS_TOKEN = "9676d82b-6bfa-4de7-8eed-f80a647e751f"  # 刚才在授权配置中生成的永久 Token

GATEWAY_URL = "https://gw.open.1688.com/openapi"


def sign(url_path: str, params: dict, secret: str) -> str:
    """计算 1688 开放网关 HMAC_SHA1 签名"""
    sorted_params = sorted([(k, str(v)) for k, v in params.items() if v is not None])
    sign_str = url_path + "".join([f"{k}{v}" for k, v in sorted_params])
    hashed = hmac.new(secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha1)
    return hashed.hexdigest().upper()


def raw_call(namespace: str, api_name: str, version: int, api_params: dict):
    """
    底层透明请求：打印完整的请求信息与未经处理的原始响应报文
    """
    url_path = f"param2/{version}/{namespace}/{api_name}/{APP_KEY}"
    full_url = f"{GATEWAY_URL}/{url_path}"

    payload = api_params.copy()
    payload["access_token"] = ACCESS_TOKEN
    payload["_aop_timestamp"] = str(int(time.time() * 1000))
    payload["_aop_signature"] = sign(url_path, payload, APP_SECRET)

    print("\n" + "=" * 80)
    print(f"🚀 发起请求: {namespace}:{api_name}")
    print(f"🔗 请求地址: {full_url}")
    print(f"📦 提交参数: {json.dumps(payload, ensure_ascii=False)}")
    print("=" * 80)

    try:
        response = requests.post(full_url, data=payload, timeout=10)
        print(f"📡 HTTP 状态码: {response.status_code}")

        try:
            res_json = response.json()
            print("\n📄 【网关原始返回 RAW JSON】:")
            print(json.dumps(res_json, indent=2, ensure_ascii=False))
            return res_json
        except Exception:
            print("⚠️ 响应不是合法的 JSON，原始文本如下:")
            print(response.text)
            return None

    except Exception as e:
        print(f"❌ 网络层异常: {str(e)}")
        return None


# ==========================================
# 测试接口 1：根据关键词搜索商品列表
# ==========================================
def test_search(keyword: str):
    print(f"\n>>> 正在测试【关键词搜索】: {keyword} ...")
    params = {
        "keywords": keyword,
        "pageNo": 1,
        "pageSize": 5
    }
    return raw_call(
        namespace="com.alibaba.product",
        api_name="alibaba.product.search",
        version=1,
        api_params=params
    )


# ==========================================
# 测试接口 2：根据具体商品 ID 查询完整详情（SKU规格、多级价格、运费信息）
# ==========================================
def test_get_product_detail(product_id: int):
    print(f"\n>>> 正在测试【拉取单品完整信息 (SKU/价格/运费)】: ID = {product_id} ...")
    params = {
        "productId": product_id,
        "needReturnOriginalCross": "true"  # 强制要求返回代发价与跨境/分销属性
    }
    return raw_call(
        namespace="com.alibaba.product",
        api_name="alibaba.product.get",
        version=1,
        api_params=params
    )


if __name__ == "__main__":
    # -------------------------------------------------------------
    # 测试 A：先看你的关键词搜索接口，网关到底返回了什么真实的错误/数据
    # -------------------------------------------------------------
    test_search(keyword="可乐")

    # -------------------------------------------------------------
    # 测试 B：测试商品详情接口（拉取 SKU 属性树、各规格底价、起批量）
    # 请在 1688 随便找一个商品，把网址中的数字填在这里进行测试
    # 例如：https://detail.1688.com/offer/612345678901.html
    # -------------------------------------------------------------
    TEST_PRODUCT_ID = 612345678901  # 👈 替换为一个真实的 1688 商品 ID
    # test_get_product_detail(TEST_PRODUCT_ID)