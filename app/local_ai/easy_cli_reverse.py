import re
import time
import base64
import httpx
from datetime import datetime
from pathlib import Path
from openai import OpenAI

# ==========================================
# 1. 配置待探测的 API 列表与全局参数
# ==========================================
API_CONFIGS = [
    {
        "base_url": "http://127.0.0.1:8317//v1",
        "name": "antigravity_codex_web",
        "api_key": "sk-7a5c7ec7086b49ca9bd1f002621d0bec"
    },


    # {
    #     "base_url": "http://127.0.0.1:2048//v1",
    #     "name": "aistudio_web",
    #     "api_key": "sk-7a5c7ec7086b49ca9bd1f002621d0bec"
    # },

    #
    # {
    #     "base_url": "http://127.0.0.1:3000/v1",
    #     "name": "chatgpt_web",
    #     "api_key": "sk-my-local-key"
    # },
    # {
    #     "base_url": "http://127.0.0.1:8083/v1",
    #     "name": "gemini_web",
    #     "api_key": "sk-7a5c7ec7086b49ca9bd1f002621d0bec"
    # },
]

TIMEOUT_SECONDS = 180.0
BASE_DIR = Path(__file__).parent

# 分离探针：避免常规对话模型被提示词诱导去调用网页端画图工具
TEXT_PROMPT = "请用一句话（50字以内）解释什么是『Python闭包』，直接输出纯文字解释。"
MEDIA_PROMPT = "请直接创作：一只戴着霓虹墨镜的赛博朋克猫。"
CANVAS_PROMPT = "请直接输出一个完整的包含 <!DOCTYPE html> 和 <html> 标签的极简赛博朋克风 Hello World 网页代码，不要输出任何额外解释。"

# Google 内部虚拟占位符正则（不带子域名的 http://googleusercontent.com/... 均非真实公网链接）
FAKE_PLACEHOLDER_RE = re.compile(r"https?://googleusercontent\.com/[a-zA-Z0-9_/-]+")


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def detect_ext_by_bytes(data: bytes, content_type: str = "", url: str = "") -> tuple[str, str]:
    """通过二进制文件头魔数 (Magic Bytes) 和 Content-Type 精准判断媒体格式"""
    ctype = content_type.lower()
    url_lower = url.lower().split("?")[0]

    # 1. 优先通过二进制文件头特征码精准识别
    if len(data) >= 12:
        if b"ftyp" in data[:16]:
            return "视频", "mp4"
        if data[:4] == b"\x1a\x45\xdf\xa3":
            return "视频", "webm"
        if data[:3] == b"\xff\xd8\xff":
            return "图片", "jpg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "图片", "png"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "图片", "webp"
        if data[:4] == b"GIF8":
            return "图片", "gif"
        if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
            return "音频", "mp3"
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            return "音频", "wav"

    # 2. 兜底通过 Content-Type 或 URL 后缀识别
    if "video" in ctype or url_lower.endswith((".mp4", ".mov", ".webm")):
        return "视频", "mp4"
    if "audio" in ctype or url_lower.endswith((".mp3", ".wav", ".ogg")):
        return "音频", "mp3"
    if "image" in ctype or url_lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "图片", "jpg" if "jpeg" in ctype or url_lower.endswith(".jpg") else "png"

    return "未知媒体", "bin"


def classify_and_save(reply: str, output_dir: Path, model_name: str) -> tuple[str, str]:
    """根据模型的实际返回内容确定模型类型，并保存非文本产物（图片/视频/音频/HTML）"""
    fname = safe_filename(model_name)

    # 1. 检测 Base64 内嵌媒体 (支持 image / video / audio)
    b64_match = re.search(r"data:(image|video|audio)/([a-zA-Z0-9+.-]+);base64,([A-Za-z0-9+/=\s]+)", reply)
    if b64_match:
        raw_bytes = base64.b64decode(b64_match.group(3))
        media_cn, ext = detect_ext_by_bytes(raw_bytes, b64_match.group(0))
        save_path = output_dir / f"{fname}.{ext}"
        save_path.write_bytes(raw_bytes)
        return f"多模态({media_cn})", f"已保存{media_cn}: `{save_path.name}`"

    # 2. 检测 HTML / Canvas 网页源码
    if "<html" in reply.lower() or "<!doctype html" in reply.lower():
        # 若包裹在 ```html 代码块中则提取纯 HTML
        html_match = re.search(r"```html\s*(.*?)\s*```", reply, re.DOTALL | re.IGNORECASE)
        html_content = html_match.group(1) if html_match else reply
        save_path = output_dir / f"{fname}.html"
        save_path.write_text(html_content, encoding="utf-8")
        return "Canvas/网页", f"已保存网页: `{save_path.name}`"

    # 3. 提取所有 URL 并区分「真实媒体链接」与「Google内部虚拟占位符」
    all_urls = re.findall(r"https?://[^\s)\]\">']+", reply)
    if all_urls:
        real_urls = [u for u in all_urls if not FAKE_PLACEHOLDER_RE.match(u)]

        # 遍历所有真实 URL 尝试下载媒体文件（解决第一个链接是占位符而漏掉后续真实视频链接的问题）
        for url in real_urls:
            try:
                with httpx.Client(timeout=60.0, follow_redirects=True) as http_client:
                    resp = http_client.get(url)
                    if resp.status_code == 200 and len(resp.content) > 512:
                        ctype = resp.headers.get("content-type", "")
                        media_cn, ext = detect_ext_by_bytes(resp.content, ctype, url)
                        if ext != "bin" or any(k in ctype for k in ("image", "video", "audio", "octet-stream")):
                            save_path = output_dir / f"{fname}.{ext}"
                            save_path.write_bytes(resp.content)
                            return f"多模态({media_cn})", f"已下载{media_cn}: `{save_path.name}`"
            except Exception:
                continue

        # 如果没有任何真实可下载链接，说明网关只吐出了 Google 内部虚拟占位符
        raw_path = output_dir / f"{fname}_raw.txt"
        raw_path.write_text(reply, encoding="utf-8")
        if not real_urls:
            placeholder_sample = all_urls[0][:42]
            return "仅返回占位符", f"网关未提取出媒体实体 (`{placeholder_sample}...`)，原文见 `{raw_path.name}`"
        return "多媒体(外链)", f"外链无法直接下载，已存 `{raw_path.name}` ({real_urls[0][:35]}...)"

    # 4. 常规纯文本回答
    clean_note = reply.replace("\n", " ").replace("|", "/")[:50]
    if len(reply) > 50:
        clean_note += "..."
    return "纯文本模型", clean_note


def call_chat_stream(client: OpenAI, model_name: str, prompt: str) -> tuple[str, float | None, float]:
    """执行单次 Chat 流式请求，返回 (完整回复, 首字耗时, 总耗时)"""
    start_time = time.time()
    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        stream=True
    )
    first_token_time = None
    full_reply = ""
    for chunk in response:
        if chunk.choices and chunk.choices[0].delta.content:
            if first_token_time is None:
                first_token_time = time.time() - start_time
            full_reply += chunk.choices[0].delta.content
    return full_reply.strip(), first_token_time, time.time() - start_time


def select_initial_prompt(model_name: str) -> str:
    """根据模型特征选择初始探针，避免用画图词诱导常规文本模型，最终类型仍由返回内容决定"""
    m_lower = model_name.lower()
    if any(k in m_lower for k in ("canvas", "html", "artifact")):
        return CANVAS_PROMPT
    if any(k in m_lower for k in ("image", "img", "video", "veo", "sora", "music", "audio", "flux", "dall", "wanx", "paint", "draw")):
        return MEDIA_PROMPT
    return TEXT_PROMPT


def test_single_model(client: OpenAI, model_name: str, output_dir: Path) -> dict:
    """测试单个模型：支持错误自适应重试与 Images 专用接口兜底"""
    prompt = select_initial_prompt(model_name)
    start_time = time.time()

    try:
        full_reply, ttft, total_time = call_chat_stream(client, model_name, prompt)
    except Exception as first_err:
        err_str = str(first_err)
        err_lower = err_str.lower()
        elapsed = time.time() - start_time

        # 自适应重试1：若未知名称的模型在纯文本探针下报「缺少HTML/Canvas」，自动换用 CANVAS_PROMPT 重试
        if prompt == TEXT_PROMPT and ("html" in err_lower or "canvas" in err_lower):
            try:
                full_reply, ttft, total_time = call_chat_stream(client, model_name, CANVAS_PROMPT)
            except Exception as retry_err:
                return {
                    "model": model_name, "type": "探测失败", "status": "❌ 失败",
                    "ttft": "N/A", "total_time": f"{time.time() - start_time:.2f}s",
                    "note": str(retry_err).splitlines()[0][:65].replace("|", "/")
                }

        # 自适应重试2：若未知名称的模型在纯文本探针下报「缺少图片/视频/媒体」，自动换用 MEDIA_PROMPT 重试
        elif prompt == TEXT_PROMPT and any(k in err_lower for k in ("image", "video", "media", "generation")):
            try:
                full_reply, ttft, total_time = call_chat_stream(client, model_name, MEDIA_PROMPT)
            except Exception as retry_err:
                return {
                    "model": model_name, "type": "探测失败", "status": "❌ 失败",
                    "ttft": "N/A", "total_time": f"{time.time() - start_time:.2f}s",
                    "note": str(retry_err).splitlines()[0][:65].replace("|", "/")
                }

        # 自适应重试3：若快速报错且非网关内部提取错误，尝试 /v1/images/generations 专用画图接口
        elif elapsed < 15.0 and not any(k in err_lower for k in ("timed out", "canvas", "artifact", "media generation")):
            try:
                img_start = time.time()
                img_resp = client.images.generate(
                    model=model_name,
                    prompt="A cute cyberpunk cat wearing neon glasses",
                    n=1,
                    size="1024x1024"
                )
                total_time = time.time() - img_start
                img_data = img_resp.data[0]
                fname = safe_filename(model_name)

                if getattr(img_data, "b64_json", None):
                    raw_bytes = base64.b64decode(img_data.b64_json)
                    _, ext = detect_ext_by_bytes(raw_bytes)
                    save_path = output_dir / f"{fname}.{ext}"
                    save_path.write_bytes(raw_bytes)
                    note = f"已保存图片: `{save_path.name}`"
                elif getattr(img_data, "url", None):
                    save_path = output_dir / f"{fname}_url.txt"
                    save_path.write_text(img_data.url, encoding="utf-8")
                    note = f"图片链接已存: `{save_path.name}`"
                else:
                    note = "返回成功但无图像负载"

                return {
                    "model": model_name, "type": "图像专用(Images)", "status": "✅ 可用",
                    "ttft": "N/A", "total_time": f"{total_time:.2f}s", "note": note
                }
            except Exception:
                return {
                    "model": model_name, "type": "探测失败", "status": "❌ 失败",
                    "ttft": "N/A", "total_time": f"{elapsed:.2f}s",
                    "note": err_str.splitlines()[0][:65].replace("|", "/")
                }
        else:
            return {
                "model": model_name, "type": "探测失败", "status": "❌ 失败",
                "ttft": "N/A", "total_time": f"{elapsed:.2f}s",
                "note": err_str.splitlines()[0][:65].replace("|", "/")
            }

    if not full_reply:
        return {
            "model": model_name, "type": "未知", "status": "⚠️ 空回复",
            "ttft": "N/A", "total_time": f"{total_time:.2f}s", "note": "接口返回成功但内容为空"
        }

    model_type, note = classify_and_save(full_reply, output_dir, model_name)
    status = "⚠️ 半可用" if model_type == "仅返回占位符" else "✅ 可用"

    return {
        "model": model_name,
        "type": model_type,
        "status": status,
        "ttft": f"{ttft:.2f}s" if ttft else "N/A",
        "total_time": f"{total_time:.2f}s",
        "note": note
    }


def run_benchmark():
    for cfg in API_CONFIGS:
        name = cfg.get("name", "default_api").strip()
        base_url = cfg.get("base_url", "").strip()
        api_key = cfg.get("api_key", "").strip()

        report_file = BASE_DIR / f"report_{safe_filename(name)}.md"
        output_dir = BASE_DIR / f"outputs_{safe_filename(name)}"
        output_dir.mkdir(exist_ok=True)

        print(f"\n{'=' * 75}")
        print(f"🚀 开始探测网关: [{name}] ({base_url})")
        print(f"📂 非文本产物目录: {output_dir.name}/ | 报告文件: {report_file.name}")
        print(f"{'=' * 75}")

        report_lines = [
            f"# 📊 API 模型可用性报告 - {name}",
            f"- **Base URL**: `{base_url}`",
            f"- **测试时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- **非文本产物目录**: `./{output_dir.name}/`",
        ]

        client = OpenAI(base_url=base_url, api_key=api_key, timeout=TIMEOUT_SECONDS)

        try:
            models_resp = client.models.list()
            detected_models = [m.id for m in models_resp.data]
            print(f"📋 发现 {len(detected_models)} 个暴露模型: {', '.join(detected_models)}")
        except Exception as e:
            err_msg = str(e).splitlines()[0][:80]
            print(f"❌ 无法拉取 /v1/models 列表: {err_msg}")
            report_lines.append(f"- **状态**: ❌ 拉取模型列表失败 (`{err_msg}`)\n")
            report_file.write_text("\n".join(report_lines), encoding="utf-8")
            continue

        results = []
        for model_name in detected_models:
            print(f"  ▶ 正在验证 [{model_name}] ...", end="", flush=True)
            res = test_single_model(client, model_name, output_dir)
            results.append(res)
            print(f" {res['status']} | {res['type']} | 耗时: {res['total_time']} | {res['note']}")

        ok_count = sum(1 for r in results if "✅" in r["status"])
        report_lines.append(f"- **实测完全可用率**: **{ok_count} / {len(results)}**\n")
        report_lines.append("| 模型名称 | 实测类型(按返回内容) | 状态 | 首字响应(TTFT) | 总耗时 | 响应摘要 / 产物文件 |")
        report_lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")

        for r in results:
            report_lines.append(
                f"| `{r['model']}` | {r['type']} | {r['status']} | {r['ttft']} | {r['total_time']} | {r['note']} |"
            )

        report_file.write_text("\n".join(report_lines), encoding="utf-8")
        print(f"✅ [{name}] 探测完毕！报告已保存至: {report_file.resolve()}")


if __name__ == "__main__":
    run_benchmark()