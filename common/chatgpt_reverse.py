import time
import re
import urllib.request
from openai import OpenAI

# 1. 初始化客户端，指向你本地部署的容器地址
client = OpenAI(
    api_key="sk-my-local-key",  # 对应 docker-compose.yml 中固定的 CHATGPT2API_AUTH_KEY
    base_url="http://127.0.0.1:3000/v1"  # 容器映射到本机的 3000 端口，且以 /v1 结尾
)


def test_available_models():
    print("🔄 正在向本地 API 请求可用模型列表...")

    try:
        # 获取模型列表 (GET /v1/models)
        models_response = client.models.list()
        model_ids = [model.id for model in models_response.data]
        print(f"✅ 成功获取到 {len(model_ids)} 个可用模型标识：")
        print(f"👉 {', '.join(model_ids)}\n")
    except Exception as e:
        print(f"❌ 获取模型列表失败，请检查 Docker 是否运行正常: {e}")
        return

    print("-" * 50)
    print("🚀 开始逐一测试各个模型的连通性...\n")

    # 2. 遍历模型列表进行测试
    for model_id in model_ids:
        print(f"正在测试模型: [{model_id}] ... ", end="", flush=True)

        try:
            # 根据模型名称动态调整测试 prompt
            if "image" in model_id:
                test_prompt = "画一个简单的红色苹果"
            else:
                test_prompt = "测试连接，请只回复数字 1"

            response = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "user", "content": test_prompt}
                ],
                max_tokens=100,
                timeout=300  # 画图时间比较长，把超时时间设大一点
            )

            reply = response.choices[0].message.content.strip()
            print(f"✅ 成功! (响应: {reply})")

            # 新增图片保存逻辑：如果是画图模型且返回了链接，将其下载到本地
            if "image" in model_id:
                # 尝试提取 Markdown 格式的图片链接 ![...](url)
                match = re.search(r'!\[.*?\]\((https?://[^\)]+)\)', reply)
                image_url = match.group(1) if match else None

                # 如果没找到 Markdown 格式，尝试直接提取文本中的纯 URL
                if not image_url:
                    urls = re.findall(r'(https?://[^\s]+)', reply)
                    if urls:
                        image_url = urls[0]

                if image_url:
                    file_name = f"{model_id}.png"
                    print(f"   ⬇️ 发现图片链接，正在保存到 {file_name} ... ", end="", flush=True)
                    try:
                        urllib.request.urlretrieve(image_url, file_name)
                        print("✅ 保存成功!")
                    except Exception as dl_e:
                        print(f"❌ 保存失败: {dl_e}")

        except Exception as e:
            # 常见报错如 403/404 说明你的账号(如免费号)无权访问该模型(如 gpt-4)
            print(f"❌ 失败! (原因: {e})")

        # ⚠️ 加上 2 秒延迟，防止连续高频发问触发 ChatGPT 网页端的 429 防御机制
        time.sleep(2)


if __name__ == "__main__":
    test_available_models()