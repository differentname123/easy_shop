import time
import base64
from pathlib import Path
from openai import OpenAI

# 连接你本机的 EasyCLIProxyAPI 网关
client = OpenAI(
    base_url="http://127.0.0.1:8317/v1",
    api_key="123456",
    timeout=60.0
)

def run_full_benchmark():
    print("=== 1. 正在拉取本地网关模型列表 ===")
    models_resp = client.models.list()
    detected_models = [m.id for m in models_resp.data]
    print(f"当前 /v1/models 暴露的模型 ({len(detected_models)}个): {', '.join(detected_models)}\n")

    # 将模型自动分为「文本/代码模型」与「图像生成模型」
    text_models = [m for m in detected_models if not m.startswith("gpt-image")]
    image_models = [m for m in detected_models if m.startswith("gpt-image")]

    # 额外加入 Plus 账号专属的 3 个高阶旗舰模型进行探测
    plus_flagship_models = ["gpt-6-sol", "gpt-6-astra", "gpt-5.6-sol"]
    for pm in plus_flagship_models:
        if pm not in text_models:
            text_models.append(pm)

    summary_results = []

    # ==========================================
    # 第一部分：逐一测试所有【文本与代码模型】
    # ==========================================
    print("=" * 65)
    print("🧠 开始测试【文本 / 推理 / 代码审查模型】")
    print("=" * 65)

    for model_name in text_models:
        print(f"\n▶ 正在测试模型: [{model_name}] ...")
        start_time = time.time()

        # 针对 codex-auto-review 使用专门的代码找茬提示词，其他模型使用逻辑+精简代码测试
        if model_name == "codex-auto-review":
            prompt = "请审查以下 Python 函数并指出潜在 Bug：\ndef add_item(item, target=[]):\n    target.append(item)\n    return target"
        else:
            prompt = "请用一句话解释什么是『闭包(Closure)』，并给出一个3行以内的Python示例。"

        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "回答请保持精炼，控制在100字以内。"},
                    {"role": "user", "content": prompt}
                ],
                stream=True
            )

            first_token_time = None
            full_reply = ""
            print("  回复: ", end="")
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    if first_token_time is None:
                        first_token_time = time.time() - start_time
                    piece = chunk.choices[0].delta.content
                    full_reply += piece
                    print(piece, end="", flush=True)

            total_time = time.time() - start_time
            ttft_str = f"{first_token_time:.2f}s" if first_token_time else "N/A"
            print(f"\n  ⏱️ 首字响应(TTFT): {ttft_str} | 总耗时: {total_time:.2f}s")
            summary_results.append((model_name, "文本/代码", "✅ 成功", f"{total_time:.2f}s", full_reply[:30].replace("\n", " ") + "..."))

        except Exception as e:
            total_time = time.time() - start_time
            err_msg = str(e).splitlines()[0][:80]
            print(f"  ❌ 调用失败 ({total_time:.2f}s): {err_msg}")
            summary_results.append((model_name, "文本/代码", "❌ 失败", f"{total_time:.2f}s", err_msg))

    # ==========================================
    # 第二部分：逐一测试所有【图像生成模型】
    # ==========================================
    print("\n" + "=" * 65)
    print("🎨 开始测试【图像生成模型】(/v1/images/generations)")
    print("=" * 65)

    image_prompt = "A cute cyberpunk cat wearing neon glasses, digital art icon, minimalist style"
    output_dir = Path(__file__).parent / "generated_images"
    output_dir.mkdir(exist_ok=True)

    for img_model in image_models:
        print(f"\n▶ 正在测试画图模型: [{img_model}] ...")
        start_time = time.time()
        try:
            img_resp = client.images.generate(
                model=img_model,
                prompt=image_prompt,
                n=1,
                size="1024x1024",
                quality="low"  # 测试时使用 low 加快生成速度并节省额度
            )
            total_time = time.time() - start_time
            img_data = img_resp.data[0]

            # 保存生成的图片到本地目录（兼容 b64_json 或 url 返回）
            save_path = output_dir / f"test_{img_model}.png"
            if getattr(img_data, "b64_json", None):
                with open(save_path, "wb") as f:
                    f.write(base64.b64decode(img_data.b64_json))
                result_note = f"已保存至 {save_path.name}"
                print(f"  🖼️ 生图成功！耗时: {total_time:.2f}s -> 图片已保存至: {save_path}")
            elif getattr(img_data, "url", None):
                result_note = f"图片URL: {img_data.url[:35]}..."
                print(f"  🖼️ 生图成功！耗时: {total_time:.2f}s -> 返回链接: {img_data.url}")
            else:
                result_note = "返回成功但无图像负载"

            summary_results.append((img_model, "图像生成", "✅ 成功", f"{total_time:.2f}s", result_note))

        except Exception as e:
            total_time = time.time() - start_time
            err_msg = str(e).splitlines()[0][:80]
            print(f"  ❌ 生图失败 ({total_time:.2f}s): {err_msg}")
            summary_results.append((img_model, "图像生成", "❌ 失败", f"{total_time:.2f}s", err_msg))

    # ==========================================
    # 第三部分：打印全模型测试成绩单
    # ==========================================
    print("\n" + "=" * 85)
    print("📊 全模型实测汇总成绩单")
    print("=" * 85)
    print(f"{'模型名称':<25} | {'类型':<8} | {'状态':<6} | {'耗时':<8} | {'结果摘要 / 备注'}")
    print("-" * 85)
    for name, m_type, status, cost, note in summary_results:
        print(f"{name:<25} | {m_type:<8} | {status:<6} | {cost:<8} | {note}")
    print("=" * 85)

if __name__ == "__main__":
    run_full_benchmark()