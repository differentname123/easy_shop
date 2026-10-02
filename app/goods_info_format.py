import time

from common.common_utils import read_file_to_str, string_to_object, setup_logger
from common.model_api import generate_content
logger = setup_logger(app_name="goods_format")

PROMPT_FILE_PATH = r"W:\project\python_project\easy_shop\prompt\商品数据结构化清洗.txt"
LLM_MAX_RETRIES = 3

def check_format_info(format_info):
    """
    检测返回的数据是否符合预期的结构和内容要求。
    :param format_info:
    :return:
    """
    pass


def gen_goods_format_info(good_desc):
    """由原帖和本地媒体提取 {evidences: [...]}；按原约定，重试耗尽返回 {}。
    post 含 post_id、content.text_content、media.local_mapping。
    """
    full_prompt = f"{read_file_to_str(PROMPT_FILE_PATH)}\n{good_desc}"
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        raw_response, error_detail = "", ""
        try:
            result = generate_content(prompt=full_prompt, preset_model_group="low")
            raw_response = result.get("content", "")
            model_used = result.get("metrics", {}).get("model_used", "")


            # : 保留此阶段只按响应正文判断成功的行为；error_detail 非空是否必须失败待确认。
            format_info = string_to_object(raw_response)
            valid, error = check_format_info(format_info)
            if not valid:
                raise ValueError(error)
            return format_info
        except Exception as exc:
            exhausted = attempt == LLM_MAX_RETRIES
            delay = 0 if exhausted else 2 ** attempt
            log = logger.error if exhausted else logger.warning
            if exhausted:
                return {}
            time.sleep(delay)
    return {}
