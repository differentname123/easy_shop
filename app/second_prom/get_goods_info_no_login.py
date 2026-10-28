import re
import requests


def fetch_taobao_uland(url):
    # 模拟手机端浏览器（移动端页面更容易解析且风控相对较弱）
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148"
            " Safari/604.1"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Referer": "https://uland.taobao.com/",
    }

    session = requests.Session()

    try:
        print(f"[*] 正在请求初始链接: {url}\n")
        # allow_redirects=True 会自动跟踪 301/302 重定向
        response = session.get(
            url, headers=headers, allow_redirects=True, timeout=15
        )

        # 1. 打印重定向路径
        if response.history:
            print("=" * 20 + " 重定向过程 " + "=" * 20)
            for resp in response.history:
                print(f"[{resp.status_code}] -> {resp.url}")
        else:
            print("[!] 未发生 HTTP 级别的 301/302 重定向。")

        # 2. 打印最终着陆页信息
        print("\n" + "=" * 20 + " 最终结果 " + "=" * 20)
        print(f"最终 URL: {response.url}")
        print(f"状态码: {response.status_code}")

        # 3. 尝试从 URL 或 HTML 源码中正则匹配商品 ID
        # 常见字段：itemId=xxxx, id=xxxx, auctionId=xxxx, bizId=xxxx
        print("\n" + "=" * 20 + " 自动匹配商品 ID " + "=" * 20)
        found_ids = set()

        # 从最终 URL 匹配
        url_match = re.search(r"[?&](?:id|itemId|auctionId)=(\d+)", response.url)
        if url_match:
            found_ids.add(url_match.group(1))

        # 从 HTML 内容匹配
        html_matches = re.findall(
            r'["\']?(?:itemId|auctionId|item_id)["\']?\s*[:=]\s*["\']?(\d{10,13})["\']?',
            response.text,
        )
        for m in html_matches:
            found_ids.add(m)

        if found_ids:
            print(f"✅ 疑似找到商品 ID: {list(found_ids)}")
        else:
            print(
                "❌ 未能在源码中直接正则匹配到纯数字商品 ID（可能需看下方源码或为动态 JS 渲染）"
            )

        # 4. 打印全部返回内容
        print("\n" + "=" * 20 + " 返回的完整网页源码 " + "=" * 20)
        print(response.text)

    except Exception as e:
        print(f"请求发生异常: {e}")


if __name__ == "__main__":
    target_url = "https://s.click.taobao.com/byfKrDi"
    fetch_taobao_uland(target_url)