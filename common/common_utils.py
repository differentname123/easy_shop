# -- coding: utf-8 --
""":authors:
    zhuxiaohu
:create_date:
    2026/4/7 0:32
:last_date:
    2026/4/7 0:32
:description:
    
"""
import ast
import asyncio
import hashlib
import json
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

from filelock import FileLock, Timeout

# common/log_config.py
import logging
import os
from logging.handlers import TimedRotatingFileHandler


def setup_logger(log_dir=r"W:\project\python_project\easy_shop\logs", app_name="BinanceBot"):
    """
    初始化全局日志配置。
    支持在多个文件中重复调用，但实际只会初始化一次。
    """
    os.makedirs(log_dir, exist_ok=True)

    # 获取根记录器
    logger = logging.getLogger()

    # 【核心安全阀】：如果已经有 Handler，说明被其他文件初始化过了，直接跳过！
    # 这一步极其重要，否则你在 10 个文件里调用，一行日志就会被打印 10 次。
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    # 包含 文件名.函数名:行号 的终极溯源格式
    formatter = logging.Formatter(
        '%(asctime)s,%(msecs)03d | %(levelname)s | [%(funcName)s] | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 按天切割日志
    log_file_path = os.path.join(log_dir, f"{app_name}.log")
    file_handler = TimedRotatingFileHandler(
        filename=log_file_path,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    logger.propagate = False

    return logger

def read_json(json_path):
    """
    读取 JSON 文件并返回内容。

    Args:
        json_path (str): JSON 文件的路径。

    Returns:
        dict: 解析后的 JSON 内容。
    """
    if not os.path.exists(json_path):
        return {}

    with open(json_path, 'r', encoding='utf-8') as f:
        try:
            data = json.load(f)
            return data
        except json.JSONDecodeError as e:
            raise ValueError(f"无法解析 JSON 文件 '{json_path}': {e}")


def save_json(json_path, data):
    dir_path = os.path.dirname(json_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

    # 锁文件通常以 .lock 结尾
    lock_path = json_path + ".lock"

    # FileLock 会在文件系统层面创建锁，支持多进程和多线程安全
    with FileLock(lock_path):
        # 原子写入
        tmp_path = json_path + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4, default=str)
        os.replace(tmp_path, json_path)


def read_file_to_str(filepath,
                     encoding: str = "utf-8",
                     errors: str = "strict") -> str:
    """
    读取文件并返回整个内容的字符串。

    参数:
        filepath: 文件路径（str 或 pathlib.Path）。
        encoding: 文本编码（默认 'utf-8'）。
        errors: 解码错误处理策略（'strict'|'replace'|'ignore' 等，默认 'strict'）。
                'strict' 会在遇到无法解码的字节时抛出 UnicodeDecodeError，
                'replace' 会用替代字符替换无法解码的字节，'ignore' 则忽略它们。

    返回:
        文件内容（str）。

    抛出:
        FileNotFoundError 如果文件不存在。
        UnicodeDecodeError 如果 decoding 失败且 errors='strict'。
    """
    p = Path(filepath)
    with p.open("r", encoding=encoding, errors=errors) as f:
        return f.read()


def string_to_object(input_str: str):
    """
    从字符串中提取并解析出 Python 列表或字典对象，专为处理 LLM 输出设计。

    核心增强：
    1. 智能提取：自动忽略 Markdown 标记（如 ```json）和首尾废话。
    2. 安全清洗（String-Aware）：在移除注释和尾随逗号时，严格保护字符串内部的内容（如 URL、带 // 的规格名）。
    3. 双引擎解析：结合 json.loads 的高性能与 ast.literal_eval 的宽容度。
    """
    if not isinstance(input_str, str):
        raise TypeError(f"输入必须是字符串，但收到了 {type(input_str).__name__}。")

    def _create_error_message(reason: str) -> str:
        preview = (input_str[:50] + '...') if len(input_str) > 50 else input_str
        return f"{reason} | 输入内容预览: '{preview}'"

    # 1. 智能提取：在字符串中寻找对象边界
    first_bracket = input_str.find('[')
    first_curly = input_str.find('{')

    if first_bracket == -1 and first_curly == -1:
        raise ValueError(_create_error_message("输入字符串中未找到起始符号 '[' 或 '{'"))

    if first_bracket == -1:
        start_pos = first_curly
    elif first_curly == -1:
        start_pos = first_bracket
    else:
        start_pos = min(first_bracket, first_curly)

    end_pos = max(input_str.rfind(']'), input_str.rfind('}'))

    if end_pos <= start_pos:
        raise ValueError(_create_error_message("未找到与起始括号匹配的结束括号 ']' 或 '}'"))

    potential_obj_str = input_str[start_pos: end_pos + 1]

    # 2. 尝试直接解析（在不进行任何危险替换的情况下优先尝试）
    try:
        return json.loads(potential_obj_str)
    except json.JSONDecodeError:
        pass

    try:
        # ast.literal_eval 原生支持尾随逗号，能解决很多问题
        return ast.literal_eval(potential_obj_str)
    except (ValueError, SyntaxError, MemoryError):
        pass

    # 3. 字符串感知的安全清洗 (如果直接解析失败，再进行深度清洗)

    # 核心黑科技：匹配字符串字面量(组1) 或 注释/尾随逗号(组2)
    # 如果匹配到组1，原样返回（保护字符串）；如果匹配到组2，返回空（删除干扰符）
    def safe_cleaner(match):
        if match.group(1):  # 如果是字符串内部的内容，原样保留
            return match.group(1)
        return ""  # 如果是注释或尾随逗号，删除它

    # 3.1 安全移除注释 (// 和 /* */)
    # 匹配规则：双引号字符串 | 单引号字符串 | 块级注释 | 行级注释
    comment_pattern = r'(".*?(?<!\\)(?:\\\\)*"|\'.*?(?<!\\)(?:\\\\)*\')|(/\*[\s\S]*?\*/|//[^\r\n]*)'
    cleaned_str = re.sub(comment_pattern, safe_cleaner, potential_obj_str)

    # 3.2 安全移除尾随逗号 (例如 {"a": 1, } -> {"a": 1})
    comma_pattern = r'(".*?(?<!\\)(?:\\\\)*"|\'.*?(?<!\\)(?:\\\\)*\')|(,\s*(?=[\]}]))'
    cleaned_str = re.sub(comma_pattern, safe_cleaner, cleaned_str).strip()

    # 4. 使用清洗后的字符串进行最终解析
    try:
        return json.loads(cleaned_str)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(cleaned_str)
        except (ValueError, SyntaxError, MemoryError) as e:
            cleaned_preview = (cleaned_str[:150] + '...') if len(cleaned_str) > 150 else cleaned_str
            error_reason = f"无法将提取的内容解析为列表或字典，解析器错误: {e}"
            raise ValueError(f"{_create_error_message(error_reason)}\n"
                             f"尝试解析的内容 (安全清洗后): '''{cleaned_str}'''")

def get_config(key):
    """
    从 config.json 文件中获取指定字段的值
    :param key: 配置字段名
    :return: 配置字段值
    """
    # 获取当前脚本所在目录
    base_dir = Path(os.path.dirname(os.path.abspath(__file__))).resolve().parent
    # 拼接 config.json 文件的绝对路径
    config_file = os.path.join(base_dir, 'config/config.json')

    # 检查 config.json 文件是否存在
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"配置文件 '{config_file}' 不存在，请检查文件路径。")

    # 读取配置文件
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"配置文件 '{config_file}' 格式错误: {e}")

    # 获取指定字段的值
    if key not in config_data:
        raise KeyError(f"配置文件中缺少字段: {key}")

    return config_data[key]


async def _download_media_async(url: str, save_dir: str, proxy: str = None) -> str:
    """异步核心：使用 MD5 作为安全文件名下载文件，返回绝对路径。"""

    # 【核心修复】：将重量级网络库移到函数内部局部导入，彻底杜绝全局 DLL 冲突
    import aiohttp
    import aiofiles
    import httpx

    # 提取后缀名 (例如 .jpg, .mp4)
    suffix = Path(urlparse(url).path).suffix

    # 直接对整个 URL 进行 MD5 哈希，绝对安全，不会有任何非法字符
    safe_name = f"{hashlib.md5(url.encode('utf-8')).hexdigest()}{suffix}"

    save_path = Path(save_dir) / safe_name
    abs_path = str(save_path.resolve())

    # 文件已存在则直接返回路径
    if save_path.exists():
        return abs_path

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    try:
        async with httpx.AsyncClient(proxy=proxy, verify=False) as client:
            async with client.stream('GET', url, headers=headers, timeout=30.0) as response:
                response.raise_for_status()

                save_path.parent.mkdir(parents=True, exist_ok=True)

                async with aiofiles.open(save_path, 'wb') as f:
                    async for chunk in response.aiter_bytes():
                        await f.write(chunk)

        return abs_path

    except Exception as e:
        print(f"[ERROR] 下载失败 {url}: {e}")
        return None


def download_web_media(url: str, save_dir: str, proxy: str = None) -> str:
    """同步入口：下载网络文件并返回本地绝对路径。"""
    return asyncio.run(_download_media_async(url, save_dir, proxy=proxy))
