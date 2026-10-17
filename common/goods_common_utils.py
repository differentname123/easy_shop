import math
import time
from pathlib import Path
from common.common_utils import read_file_to_str, setup_logger, string_to_object
from common.model_api import generate_content

logger = setup_logger(app_name="goods_format")

SEGMENT_PROMPT_FILE_PATH = Path(__file__).resolve().parents[1] / "prompt" / "商品分词.txt"
LLM_MAX_RETRIES = 3


def _is_clean_string(value):
    """拒绝空白、首尾空格和不能编码为 UTF-8 的文本，保护模型输出协议。"""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def check_segment_info(segment_dict):
    """校验模型分词结果是否为 {"词语1": 0.95, "词语2": 0.5}。
    返回: (是否通过, 规整后的字典, 错误原因)
    """
    if not isinstance(segment_dict, dict) or not segment_dict:
        return False, {}, "顶层结构必须是非空对象(JSON Object)"

    cleaned_results = {}
    for word, weight in segment_dict.items():
        if not _is_clean_string(word):
            return False, {}, f"分词键名 [{word!r}] 必须是非空、无首尾空白的 UTF-8 字符串"

        # 使用 type() 排除 bool 类型（Python 中 isinstance(True, int) 为 True），并拦截 NaN/Inf
        if type(weight) not in (int, float) or not math.isfinite(weight) or not (0.0 < weight <= 1.0):
            return False, {}, f"词语 [{word}] 的权重 ({weight!r}) 必须是 (0, 1.0] 范围内的有限数值"

        cleaned_results[word] = round(float(weight), 4)

    return True, cleaned_results, ""


def gen_goods_segment_info(
    name,
    prompt_text=None,
    model="qwen/qwen3.8-27b",
    preset_model_group=None,
):
    """对单个商品名称进行分词与权重打分。
    支持最多三次模型尝试及 2/4 秒指数退避；返回 {status, results: {词: 权重}, model_used, error}。
    """
    outcome = {
        "status": "failed",
        "results": {},
        "model_used": None,
        "error": "",
    }

    # 1. 入参合法性校验
    if not isinstance(name, str) or not name.strip():
        outcome["error"] = "商品名称 name 必须是非空字符串"
        return outcome

    clean_name = name.strip()

    # 2. 读取或复用外部传入的 prompt_text
    if prompt_text is None:
        try:
            prompt_text = read_file_to_str(SEGMENT_PROMPT_FILE_PATH)
        except Exception as exc:
            outcome["error"] = f"读取分词提示词文件失败: {type(exc).__name__}: {exc}"
            return outcome

    if not isinstance(prompt_text, str) or not prompt_text.strip():
        outcome["error"] = "分词提示词内容为空"
        return outcome

    full_prompt = f"{prompt_text.rstrip()}\n{clean_name}"

    # 3. 组装模型调用参数（优先使用 preset_model_group，否则使用具体 model）
    call_kwargs = {"prompt": full_prompt}
    if preset_model_group is not None:
        call_kwargs["preset_model_group"] = preset_model_group
    else:
        call_kwargs["model"] = model

    # 4. 指数退避重试与结果校验
    errors, content = [], ""
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            result = generate_content(**call_kwargs)
            if not isinstance(result, dict):
                raise RuntimeError(f"模型接口返回格式异常，预期 dict，实际为 {type(result).__name__}")

            model_used = (result.get("metrics") or {}).get("model_used") or model
            if model_used:
                outcome["model_used"] = model_used

            if result.get("status") != "✅ 成功":
                detail = "；".join(str(err) for err in (result.get("error_history") or []))
                raise RuntimeError(detail or result.get("content") or "模型调用失败")

            content = result.get("content", "")
            parsed_obj = string_to_object(content)

            valid, cleaned_results, check_error = check_segment_info(parsed_obj)
            if not valid:
                raise ValueError(f"分词结构校验未通过: {check_error}")

            outcome.update(status="success", results=cleaned_results)
            return outcome

        except Exception as exc:
            detail = f"尝试 {attempt}/{LLM_MAX_RETRIES}: {type(exc).__name__}: {exc}"
            errors.append(detail)
            if attempt < LLM_MAX_RETRIES:
                delay = 2 ** attempt
                logger.warning(
                    "⚠️ [商品分词/重试] 模型调用、解析或校验未通过 | 商品名: [%s] "
                    "| 尝试: [%d/%d] | 等待: [%d 秒] | 原因: [%s] | 排查: [模型服务、提示词协议]",
                    clean_name[:60], attempt, LLM_MAX_RETRIES, delay,
                    " ".join(detail.split())[:400],
                )
                time.sleep(delay)

    outcome["error"] = "；".join(errors) + (f"\n模型原文：{content}" if content else "")
    logger.error(
        "❌ [商品分词/失败] 已达最大重试次数 | 商品名: [%s] | 模型: [%s] | 原因: [%s]",
        clean_name[:60], outcome["model_used"] or "未返回", " ".join(outcome["error"].split())[:400],
    )
    return outcome

if __name__ == "__main__":
    name = "【70g*7袋】重庆德庄小方块火锅底料家用麻辣小龙虾香锅调料批发"

    # 方式 1：单次直接调用（内部自动读取默认路径下的 商品分词.txt）
    res = gen_goods_segment_info(name=name, model="qwen/qwen3.8-27b")

    # 方式 2：高频/多线程循环调用（外部预读一次 prompt_text 传入，避免重复磁盘 I/O）
    # prompt_text = read_file_to_str(r"W:\project\python_project\easy_shop\prompt\商品分词.txt")
    # res = gen_goods_segment_info(name=name, prompt_text=prompt_text, model="qwen/qwen3.8-27b")

    if res["status"] == "success":
        print(f"使用模型: {res['model_used']}")
        print(f"分词结果: {res['results']}")
        # 输出示例: [{'word': '火锅底料', 'weight': 0.95}, {'word': '德庄', 'weight': 0.85}, ...]
    else:
        print(f"处理失败: {res['error']}")