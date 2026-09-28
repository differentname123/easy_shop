import requests
from openai import OpenAI
import time


# =========================
# 填你的 ModelScope Token
# =========================

API_KEY = "ms-3a52119a-e458-405e-9ee6-ce26c6bb8bcb"


BASE_URL = "https://api-inference.modelscope.cn/v1"


# =========================
# 获取模型列表
# =========================

def get_models():

    headers = {
        "Authorization": f"Bearer {API_KEY}"
    }

    url = BASE_URL + "/models"

    r = requests.get(
        url,
        headers=headers,
        timeout=20
    )

    if r.status_code != 200:
        print("获取模型失败:")
        print(r.text)
        return []

    data = r.json()

    models = []

    for m in data.get("data", []):
        models.append(m["id"])

    return models



# =========================
# 测试模型
# =========================

def test_model(model):

    client = OpenAI(
        api_key=API_KEY,
        base_url=BASE_URL
    )

    try:

        r = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": "你好，请回复一句测试信息"
                }
            ],
            max_tokens=20,
            timeout=30
        )


        text = r.choices[0].message.content

        return True, text


    except Exception as e:

        return False, str(e)



# =========================
# 主程序
# =========================

if __name__ == "__main__":


    print("正在获取模型列表...\n")


    models = get_models()


    if not models:
        exit()


    print(
        f"发现 {len(models)} 个模型\n"
    )


    success = []
    failed = []


    for i, model in enumerate(models):

        print(
            f"[{i+1}/{len(models)}] 测试 {model}"
        )


        ok, result = test_model(model)


        if ok:

            print(
                "  ✅ 可用:",
                result[:50]
            )

            success.append(model)


        else:

            print(
                "  ❌ 失败:",
                result[:80]
            )

            failed.append(model)


        time.sleep(1)



    print("\n===================")
    print("可用模型:")
    print("===================")


    for m in success:
        print(
            "✅",
            m
        )


    print("\n失败模型数量:", len(failed))