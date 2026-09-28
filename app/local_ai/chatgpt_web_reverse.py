import time
import re
import base64
import urllib.request
from openai import OpenAI

# 1. 初始化客户端，指向本地部署的容器地址
client = OpenAI(
    api_key="sk-my-local-key",
    base_url="http://127.0.0.1:3000/v1"
)


def save_image_from_reply(reply: str, model_id: str) -> None:
    """从模型响应中提取图片（支持 Base64 Data URI 和 HTTP/HTTPS 链接）并保存到本地"""
    # 过滤模型名称中的非法路径字符（如 / \ : * ? " < > |）
    safe_model_name = re.sub(r'[\\/*?:"<>|]', "_", model_id)

    # 情况 A：匹配 Markdown 或文本中的 Base64 Data URI
    # 格式示例: data:image/png;base64,iVBORw0KGgo...
    b64_match = re.search(r'data:image/(?P<ext>[a-zA-Z0-9]+);base64,(?P<data>[A-Za-z0-9+/=\r\n]+)', reply)
    if b64_match:
        ext = b64_match.group("ext").lower()
        b64_data = b64_match.group("data")
        file_name = f"{safe_model_name}.{ext}"
        print(f"   ⬇️ 检测到 Base64 图片数据，正在解码保存到 {file_name} ... ", end="", flush=True)
        try:
            image_bytes = base64.b64decode(b64_data)
            with open(file_name, "wb") as f:
                f.write(image_bytes)
            print(f"✅ 保存成功! ({len(image_bytes) / 1024:.1f} KB)")
        except Exception as e:
            print(f"❌ Base64 解码保存失败: {e}")
        return

    # 情况 B：匹配 Markdown 格式的 HTTP/HTTPS 链接 ![...](http...) 或纯 URL
    url_match = re.search(r'!\[.*?\]\((https?://[^\)\s]+)\)', reply)
    if url_match:
        image_url = url_match.group(1)
    else:
        urls = re.findall(r'(https?://[^\s\)]+)', reply)
        image_url = urls[0] if urls else None

    if image_url:
        file_name = f"{safe_model_name}.png"
        print(f"   ⬇️ 发现图片链接，正在下载到 {file_name} ... ", end="", flush=True)
        try:
            # 添加 User-Agent 防止 CDN 返回 403 Forbidden
            req = urllib.request.Request(
                image_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=60) as resp, open(file_name, "wb") as f:
                f.write(resp.read())
            print("✅ 保存成功!")
        except Exception as dl_e:
            print(f"❌ 下载失败: {dl_e}")
    else:
        print("   ⚠️ 未在响应中识别到有效的图片链接或 Base64 数据。")


def test_available_models():
    print("🔄 正在向本地 API 请求可用模型列表...")

    try:
        models_response = client.models.list()
        model_ids = [model.id for model in models_response.data]
        print(f"✅ 成功获取到 {len(model_ids)} 个可用模型标识：")
        print(f"👉 {', '.join(model_ids)}\n")
    except Exception as e:
        print(f"❌ 获取模型列表失败，请检查 Docker 是否运行正常: {e}")
        return

    print("-" * 50)
    print("🚀 开始逐一测试各个模型的连通性...\n")

    for model_id in model_ids:
        print(f"正在测试模型: [{model_id}] ... ", end="", flush=True)
        is_image_model = "image" in model_id.lower()

        try:
            if is_image_model:
                test_prompt = "画一个简单的红色苹果"
                request_kwargs = {"timeout": 300}  # 画图模型不限制 max_tokens=100，避免截断
            else:
                test_prompt = "测试连接，请只回复数字 1"
                request_kwargs = {"max_tokens": 100, "timeout": 60}

            response = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": test_prompt}],
                **request_kwargs
            )

            reply = (response.choices[0].message.content or "").strip()

            # 防止 Base64 超长字符串刷屏控制台
            if "data:image" in reply or len(reply) > 120:
                display_reply = f"{reply[:80]}... [内容过长已省略，总长度 {len(reply)} 字符]"
            else:
                display_reply = reply

            print(f"✅ 成功! (响应: {display_reply})")

            # 如果是画图模型（或响应中包含图片数据），执行提取保存
            if is_image_model or "data:image" in reply:
                save_image_from_reply(reply, model_id)

        except Exception as e:
            print(f"❌ 失败! (原因: {e})")

        time.sleep(2)


if __name__ == "__main__":
    test_available_models()