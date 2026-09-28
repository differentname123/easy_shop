from openai import OpenAI

# 初始化 OpenAI 客户端
# 注意：如果您的 Python 代码和该服务不在同一台机器上，请将 127.0.0.1 替换为实际的服务器 IP 地址
client = OpenAI(
    api_key="sk-7a5c7ec7086b49ca9bd1f002621d0bec", #[cite: 1]
    base_url="http://127.0.0.1:8083/v1" #[cite: 1]
)

# 发起聊天请求
response = client.chat.completions.create(
    model="gemini-3.7-flash", #[cite: 1]
    messages=[
        {"role": "system", "content": "你是一个有用的助手。"},
        {"role": "user", "content": "你好，请用一句话介绍一下自己。"}
    ]
)

# 打印返回的文本内容
print(response.choices[0].message.content)