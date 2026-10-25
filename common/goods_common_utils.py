import math
import time
from pathlib import Path
from common.common_utils import read_file_to_str, setup_logger, string_to_object
from common.model_api import generate_content

logger = setup_logger(app_name="goods_format")

SEGMENT_PROMPT_FILE_PATH = Path(__file__).resolve().parents[1] / "prompt" / "商品分词.txt"
LLM_MAX_RETRIES = 3


def _is_clean_string(value):
    """拒绝空白、首尾空格和不能编码为 UTF-8 的文本。"""
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

        if type(weight) not in (int, float) or not math.isfinite(weight) or not (0.0 < weight <= 1.0):
            return False, {}, f"词语 [{word}] 的权重 ({weight!r}) 必须是 (0, 1.0] 范围内的有限数值"

        cleaned_results[word] = round(float(weight), 4)

    return True, cleaned_results, ""



def gen_goods_segment_info(
        name,
        prompt_text=None,
        top_key=3,
        model="qwen/qwen3.8-27b",
        preset_model_group=None,
):
    """提取商品名称中权重最高的前 top_key 个核心词，直接返回字符串列表。

    校验规则：
      1. check_segment_info 基础结构/类型校验通过；
      2. 模型输出的分词必须全部存在于原商品名称中（禁止模型产生同义词/幻觉词），否则触发重试。
    若模型调用异常、解析失败或无有效分词，严格兜底返回 [name]。
    """
    # 1. 入参校验与兜底初值定义
    if not isinstance(name, str) or not name.strip():
        logger.warning("⚠️ [商品分词/警告] 输入的商品名称为空")
        return [name] if name is not None else []

    clean_name = name.strip()
    fallback = [clean_name]
    if len(clean_name) <=3:
        return fallback  # 对于过短的商品名，直接返回原词，避免模型输出不稳定


    # 2. 读取或复用外部传入的提示词文本
    if prompt_text is None:
        try:
            prompt_text = read_file_to_str(SEGMENT_PROMPT_FILE_PATH)
        except Exception as exc:
            logger.error("❌ [商品分词/错误] 读取提示词文件失败: %s，降级返回原词", exc)
            return fallback

    if not isinstance(prompt_text, str) or not prompt_text.strip():
        logger.error("❌ [商品分词/错误] 提示词内容为空，降级返回原词")
        return fallback

    full_prompt = f"{prompt_text.rstrip()}\n{clean_name}"

    # 3. 构造请求参数
    call_kwargs = {"prompt": full_prompt}
    if preset_model_group is not None:
        call_kwargs["preset_model_group"] = preset_model_group
    else:
        call_kwargs["model"] = model

    # 4. 指数退避重试
    errors, content = [], ""
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            result = generate_content(**call_kwargs)
            if not isinstance(result, dict):
                raise RuntimeError(f"返回预期为 dict，实际为 {type(result).__name__}")

            if result.get("status") != "✅ 成功":
                detail = "；".join(str(err) for err in (result.get("error_history") or []))
                raise RuntimeError(detail or result.get("content") or "模型调用失败")

            content = result.get("content", "")
            parsed_obj = string_to_object(content)

            # 4.1 基础格式与结构校验
            valid, cleaned_results, check_error = check_segment_info(parsed_obj)
            if not valid:
                raise ValueError(f"结构校验未通过: {check_error}")

            # 4.2 严格校验：分词必须存在于原商品名 clean_name 中
            hallucinated_words = [word for word in cleaned_results if word not in clean_name]
            if hallucinated_words:
                raise ValueError(
                    f"模型输出幻觉分词（不在商品原名中）: {hallucinated_words} | 原名: '{clean_name}'"
                )

            # 4.3 按权重从大到小排序并截取 top_key 个
            sorted_words = sorted(cleaned_results, key=cleaned_results.get, reverse=True)
            top_words = sorted_words[:top_key] if (top_key and top_key > 0) else sorted_words

            return top_words if top_words else fallback

        except Exception as exc:
            detail = f"尝试 {attempt}/{LLM_MAX_RETRIES}: {type(exc).__name__}: {exc}"
            errors.append(detail)
            if attempt < LLM_MAX_RETRIES:
                delay = 2 ** attempt
                logger.warning(
                    "⚠️ [商品分词/重试] 校验未通过 | 商品名: [%s] | 尝试: [%d/%d] | 等待: [%d 秒] | 原因: [%s]",
                    clean_name[:50], attempt, LLM_MAX_RETRIES, delay, " ".join(detail.split())[:300]
                )
                time.sleep(delay)

    # 5. 重试耗尽，记录详细日志并安全兜底
    logger.error(
        "❌ [商品分词/耗尽] 模型处理失败，保底返回原词 | 商品名: [%s] | 原因: [%s]",
        clean_name[:50], "；".join(errors)[:400]
    )
    return fallback