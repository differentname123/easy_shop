# [功能摘要] 使用本地冷却账号池逐分类采集拼多多商品，将收到的字段增量写入 MongoDB。
# [输入数据] pdd_browser_data_list 浏览器目录；本地账号 JSON；导航 DOM；goods_list 接口 JSON。
# [数据流转/交互] 锁定本地状态文件 → 按原顺序选账号并记录开始时间 → 探测分类 → 监听 HTTP 200
#                 响应 → 清洗实际收到的字段 → ProductManager.update；页面失败按原规则换号重试。
# [输出数据] products 中的商品补丁、账号 ISO 时间 JSON、分类统计及异常 PNG/HTML；存储故障释放资源后上抛。

import json
import logging
import os
import time
import traceback
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from app.pdd_utils import search_pdd_goods_by_keyword, get_pdd_recommend_goods
from app.taobao_fetch_goods_info import search_taobao_goods_by_keyword
from common.playwright_utils import launch_persistent_context, search_goods_and_intercept
from common.common_utils import get_config, read_json, save_json, setup_logger
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager

USER_DATA_DIR = r"W:\temp\biance_pdd_myself"
USER_DATA_DIR_LIST = [r"W:\temp\biance_pdd_myself", r"W:\temp\biance_pdd_nana"]
logger = setup_logger("pdd_scraper")

GLOBAL_CONFIG = {
    "target_url": "https://mobile.pinduoduo.com/pincard_ask.html?__rp_name=brand_amazing_price_group_channel",
    "platform": "pdd",
    "account_status_file": Path(__file__).resolve().parents[1] / "data" / "crawler_account_status.json",
    "account_cooldown_minutes": 30,
    "wait_no_account_seconds": 60,
    "max_scrolls_per_tab": -1,
    "headless_mode": True,
    "scroll_step_y": 6000,
    "scroll_interval": 2.0,
}
STAT_KEYS = ("scrolls", "requests", "new", "update")
MAX_ATTEMPTS_PER_TAB = 3
STATUS_REASONS = {
    "RISK_CONTROL": "触发风控",
    "PAGE_MISMATCH": "进入搜索页或未捕获目标响应",
    "ERROR": "页面加载或交互异常",
    "LOGGED_OUT": "账号掉登录",  # 【新增】状态枚举
}
import threading  # 请确保在文件顶部添加此导入


class StorageError(RuntimeError):
    """商品写入或回执处理失败必须终止采集，不能误当成页面问题换号。"""


import hashlib
import re

# 专门用于从淘金币、88VIP等促销ID前缀中精准剥离出纯数字商品ID（num_iid）的正则
_TAOBAO_NUM_IID_PATTERN = re.compile(r"^(\d{10,15})(?:coin|vip|f|\d)*", re.IGNORECASE)


def _safe_float(val, default=0.0):
    """安全将字符串或数值转为 float"""
    if val is None or val == "":
        return default
    try:
        return round(float(val), 2)
    except (ValueError, TypeError):
        return default


def _normalize_url(url_str):
    """补全淘宝返回的 // 相对协议链接"""
    if not url_str or not isinstance(url_str, str):
        return ""
    url_str = url_str.strip()
    if url_str.startswith("//"):
        return f"https:{url_str}"
    return url_str


def _extract_pure_digital_product_id(item: dict) -> tuple[str, str]:
    """
    核心：不使用动态变化的 item_id，强制从促销 ID 或其他恒定字段中提取纯数字商品 ID (num_iid)。
    返回: (pure_digital_product_id, fallback_url)
    """
    price_info = item.get("price_promotion_info") or {}

    # 1. 遍历所有促销列表（final_promotion_path_map_data, more_promotion_map_data）
    promo_lists = [
        price_info.get("final_promotion_path_list", {}).get("final_promotion_path_map_data", []),
        price_info.get("more_promotion_list", {}).get("more_promotion_map_data", []),
    ]
    for p_list in promo_lists:
        if not isinstance(p_list, list):
            continue
        for promo in p_list:
            if not isinstance(promo, dict):
                continue
            promo_id = str(promo.get("promotion_id") or "").strip()
            # 尝试匹配前缀的 10~15 位纯数字商品 ID
            match = _TAOBAO_NUM_IID_PATTERN.match(promo_id)
            if match:
                num_iid = match.group(1)
                if len(num_iid) >= 8:  # 确保是合法的淘宝商品数字ID长度
                    return num_iid, f"https://item.taobao.com/item.htm?id={num_iid}"

    # 2. 备用策略：尝试从 click_url 或 coupon_share_url 的参数中正则匹配 id=XXXXXX
    publish = item.get("publish_info") or {}
    for url_key in ("click_url", "coupon_share_url"):
        raw_url = str(publish.get(url_key) or "")
        id_match = re.search(r"[?&]id=(\d{8,15})", raw_url)
        if id_match:
            num_iid = id_match.group(1)
            return num_iid, f"https://item.taobao.com/item.htm?id={num_iid}"

    # 3. 终极兜底：如果实在找不到纯数字 ID，用 seller_id + title 的 MD5 生成唯一指纹（确保入库不崩，但它是稳定的）
    basic = item.get("item_basic_info") or {}
    seller_id = str(basic.get("seller_id") or "").strip()
    title = str(basic.get("title") or "").strip()
    if seller_id and title:
        raw_key = f"tb_digital_{seller_id}_{title}"
        hash_id = "tb_" + hashlib.md5(raw_key.encode("utf-8")).hexdigest()[:16]
        return hash_id, ""

    return "", ""


def normalize_taobao_goods(item, default_category, source_api="taobao_api_search"):
    """
    清洗淘宝联盟返回的数据：
    1. 严禁使用动态变化的 item_id。
    2. 强制提取或映射为恒定不变的纯数字商品 ID 作为 product_id。
    3. 构造统一的默认 sku_info 列表，并打标 need_update_sku: True。
    """
    if not isinstance(item, dict):
        return None

    # 强制获取纯数字商品 ID
    product_id, fallback_url = _extract_pure_digital_product_id(item)
    if not product_id:
        return None

    # 规范化 _source_api 为列表结构
    if isinstance(source_api, list):
        api_list = [s for s in source_api if s]
    elif isinstance(source_api, (tuple, set)):
        api_list = [s for s in source_api if s]
    elif source_api:
        api_list = [str(source_api)]
    else:
        api_list = ["taobao_api_search"]

    basic = item.get("item_basic_info") or {}
    price_info = item.get("price_promotion_info") or {}
    publish = item.get("publish_info") or {}
    income_info = publish.get("income_info") or {}

    # 基础信息提取
    category = (
            basic.get("category_name")
            or basic.get("level_one_category_name")
            or default_category
    )
    title = basic.get("title") or basic.get("short_title") or ""
    brand = basic.get("brand_name") or basic.get("shop_title") or ""
    sales_tip = (
            basic.get("annual_vol")
            or (f"已售{basic.get('tk_total_sales')}" if basic.get("tk_total_sales") else "")
            or str(basic.get("volume", ""))
    )
    image_url = _normalize_url(basic.get("pict_url") or basic.get("white_image") or "")
    item_id = str(item.get("item_id") or "").strip()  # 仅用于日志或调试，绝不作为 product_id

    # 价格计算
    reserve_price = _safe_float(price_info.get("reserve_price"))
    zk_final_price = _safe_float(price_info.get("zk_final_price"))
    final_promo_price = _safe_float(price_info.get("final_promotion_price"))
    predict_price = _safe_float(price_info.get("predict_rounding_up_price"))

    original_price = zk_final_price if zk_final_price > 0 else reserve_price
    activity_price = final_promo_price if final_promo_price > 0 else original_price

    if original_price > activity_price > 0:
        saved_price = round(original_price - activity_price, 2)
    else:
        promo_paths = (
            price_info.get("final_promotion_path_list", {})
            .get("final_promotion_path_map_data", [])
        )
        saved_price = round(
            sum(_safe_float(p.get("promotion_fee")) for p in promo_paths if isinstance(p, dict)),
            2,
        )

    # 推广链接
    coupon_share_url = _normalize_url(publish.get("coupon_share_url", ""))
    click_url = _normalize_url(publish.get("click_url", ""))
    promotion_url = coupon_share_url or click_url or fallback_url
    product_url = fallback_url or click_url or promotion_url

    est_commission = _safe_float(income_info.get("commission_amount"))
    commission_rate = _safe_float(
        income_info.get("commission_rate") or publish.get("income_rate")
    )

    # 统一构造默认单规格 sku_info
    default_sku_info = [
        {
            "价格": f"￥{activity_price}",
            "截图地址": "",
        }
    ]

    return {
        "platform": "taobao",
        "product_id": product_id,
        "item_id": item_id,
        "category": category,
        "_source_api": api_list,
        "name": title,
        "brand": brand,
        "sales_tip": str(sales_tip),
        "image_url": image_url,
        "original_price": original_price,
        "activity_price": activity_price,
        "saved_price": saved_price,
        "promotion_url": promotion_url,
        "product_url": product_url,
        "promotion_info": {
            "promotion_rate": commission_rate,
            "estimated_commission": est_commission,
            "has_mall_coupon": bool(coupon_share_url),
            "predict_rounding_up_price": predict_price,
            "shop_title": basic.get("shop_title", ""),
        },
        "sku_info": default_sku_info,
        "need_update_sku": True,
    }


class AccountPool:
    """accounts 为目录列表；JSON 形貌为 {平台: {绝对目录: ISO 时间字符串 或 状态字符串}}。
    整个调度期间持有操作系统文件锁；临时文件原子替换，避免多进程抢号和半写文件。
    """

    def __init__(self, accounts, state_path):
        if not isinstance(accounts, (list, tuple)) or not accounts:
            raise ValueError("账号配置必须是非空目录列表")
        if any(not isinstance(account, str) or not account.strip() for account in accounts):
            raise ValueError("每个账号目录必须是非空字符串")
        self.accounts = list(dict.fromkeys(str(Path(account.strip()).expanduser().resolve()) for account in accounts))
        self.path = Path(state_path).expanduser().resolve()
        self.platform = GLOBAL_CONFIG["platform"]
        self._lock = None
        self._state = {}

    def __enter__(self):
        if self._lock is not None:
            raise RuntimeError("账号池不能重复进入尚未结束的 with 语句")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        try:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._lock.seek(0, os.SEEK_END)
                    if self._lock.tell() == 0:
                        self._lock.write(b"\0")
                        self._lock.flush()
                    self._lock.seek(0)
                    msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(
                    f"账号文件无法加锁 [{self.path}]；请检查是否有采集器正在使用此文件或目录权限不足") from exc
            self._state = {}
            if self.path.exists():
                with self.path.open(encoding="utf-8") as source:
                    self._state = json.load(source)
            if not isinstance(self._state, dict) or not isinstance(self._state.get(self.platform, {}), dict):
                raise ValueError(f"账号 JSON 结构不正确 [{self.path}]，应为 {{平台: {{目录: ISO 时间}}}}")
            return self
        except BaseException:
            self._lock.close()
            self._lock = None
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def _save(self, state):
        """state 为完整账号 JSON 字典；同目录临时写入并落盘，成功后才替换当前状态。"""
        temporary_path = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                    prefix=f".{self.path.name}.", suffix=".tmp", delete=False) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(state, temporary, ensure_ascii=False, indent=2, allow_nan=False)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
            self._state = state
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def mark_invalid(self, account, reason="LOGGED_OUT"):
        """【新增功能】将账号标记为失效（如掉登录），不再参与调度，修改 JSON 并显眼输出警报"""
        if self._lock is None:
            raise RuntimeError("账号池必须在 with 语句中使用")
        state = dict(self._state)
        statuses = state.get(self.platform, {})
        # 覆盖为 LOGGED_OUT 字符串写入 json 文件中
        state[self.platform] = {**statuses, account: reason}
        self._save(state)

        # 控制台最显眼的警报
        print("\n" + "❗" * 35)
        print(f"🚨🚨🚨 警报: 检测到账号异常状态 [{reason}] 🚨🚨🚨")
        print(f"📁 账号目录: {account}")
        print(f"⚠️  该账号已被移出可用池！状态已写入 JSON")
        print(f"👉 恢复方法: 重新登录后，请手动修改 JSON 内的状态才能恢复调度！")
        print("❗" * 35 + "\n")
        logger.error("❌ [账号/封禁] 账号被标记为 %s，永久跳过调度 | 账号: [%s]", reason, os.path.basename(account))

    def acquire(self, task_name):
        """按配置顺序等待可用账号，先成功记录使用时间，再返回绝对目录。"""
        if self._lock is None:
            raise RuntimeError("账号池必须在 with 语句中使用")
        cooldown = timedelta(minutes=GLOBAL_CONFIG["account_cooldown_minutes"])
        while True:
            now = datetime.now(timezone.utc)
            statuses = self._state.get(self.platform, {})
            for account in self.accounts:
                last_used = statuses.get(account)

                # 【新增拦截】如果状态为 LOGGED_OUT，永久无视该账号，不计入冷却逻辑
                if last_used == "LOGGED_OUT":
                    continue

                if last_used is not None:
                    try:
                        last_used = datetime.fromisoformat(last_used)
                    except (TypeError, ValueError):
                        logger.warning("[调度/时间校验] 时间格式异常，沿用允许使用规则 | 账号: [%s] "
                                       "| 排查: [账号 JSON 的 ISO 时间字段]", os.path.basename(account))
                        last_used = None
                if last_used is not None:
                    if last_used.tzinfo is None:
                        last_used = last_used.replace(tzinfo=timezone.utc)
                    if now - last_used < cooldown:
                        continue
                state = dict(self._state)
                state[self.platform] = {**statuses, account: now.isoformat()}
                # : 冷却从任务开始计时，结束或触发风控不刷新；长任务完成后可能立即再次可用。
                self._save(state)
                logger.info("[调度/分配] 账号已分配并记录时间 | 任务: [%s] | 账号: [%s] "
                            "| 冷却: [%d 分钟，自此刻起算]", task_name, os.path.basename(account),
                            GLOBAL_CONFIG["account_cooldown_minutes"])
                return account
            wait_seconds = GLOBAL_CONFIG["wait_no_account_seconds"]
            logger.info("[调度/等待] 暂无可用账号 | 任务: [%s] | 再次检查: [%d 秒后]", task_name, wait_seconds)
            time.sleep(wait_seconds)

# ==========================================
# 新增：淘宝联盟 API 搜索并行任务模块
# ==========================================
def taobao_api_search_task():
    """后台任务：通过淘宝联盟 API 搜索指定关键词商品并入库，每轮等待 24 小时"""
    default_keywords = [
        # 基础水饮与酒水
        "可乐", "牛奶", "矿泉水", "果汁", "咖啡", "茶叶", "啤酒", "酸奶", "功能饮料", "气泡水",
        "奶茶", "豆奶", "苏打水", "纯净水", "鸡尾酒", "红酒", "白酒", "燕麦奶", "柠檬茶", "凉茶",
        # 休闲零食
        "零食", "饼干", "薯片", "巧克力", "坚果", "糖果", "火腿肠", "牛肉干", "辣条", "冰淇淋",
        "果冻", "话梅", "肉脯", "海苔", "曲奇", "瓜子", "花生", "魔芋爽", "凤爪", "鸭脖",
        # 饱腹代餐与速食
        "方便面", "面包", "麦片", "速冻水饺", "自热火锅", "螺蛳粉", "酸辣粉", "罐头", "蛋黄酥", "手撕面包",
        # 厨房粮油与生鲜调味
        "大米", "面条", "食用油", "酱油", "食盐", "鸡蛋", "蜂蜜", "燕麦片", "火锅底料", "老干妈",
        "陈醋", "白糖", "鸡精", "豆瓣酱", "蚝油", "芝麻酱", "面粉", "粉丝", "紫菜", "干香菇",
        # 家庭日用与清洁
        "抽纸", "卷纸", "湿巾", "洗衣液", "洗洁精", "垃圾袋", "保鲜膜", "保鲜袋", "洁厕灵", "消毒液",
        "柔顺剂", "洗手液", "驱蚊液", "除湿盒", "厨房纸",
        # 个人护理与日化
        "洗发水", "沐浴露", "牙膏", "牙刷", "洗面奶", "护发素", "润唇膏", "身体乳", "卫生巾", "棉签",
        "洗脸巾", "漱口水", "香皂", "剃须刀", "护手霜"
    ]

    taobao_app_key = get_config("myself_taobao_app_key")
    taobao_app_secret = get_config("myself_taobao_app_secret")
    taobao_pid = get_config("myself_taobao_pid")
    monitored_file = Path(r"W:\project\python_project\easy_shop\app\web_front\monitored_products.json")

    while True:
        logger.info("[淘宝API任务/轮次开始] 开始执行淘宝联盟 API 数据拉取...")

        # 优先尝试读取前端监控词配置，若不存在或为空则回退到内置词表
        keywords = default_keywords
        if monitored_file.exists():
            try:
                monitored_products = read_json(str(monitored_file)) or {}
                monitored_keys = [k for k, v in monitored_products.items() if isinstance(v, dict) and v.get("is_monitoring")]
                if monitored_keys:
                    keywords = monitored_keys
                    logger.info("[淘宝API任务/配置] 已从监控配置加载 %d 个关键词", len(keywords))
            except Exception as cfg_exc:
                logger.warning("[淘宝API任务/配置] 读取监控词文件失败，沿用默认词表 | 错误: [%s]", cfg_exc)

        try:
            with closing(gen_db_object()) as db_instance:
                db_instance.ping()
                product_manager = ProductManager(db_instance)

                for keyword in keywords:
                    logger.info("[淘宝API任务/搜索] 正在拉取关键词: [%s]", keyword)
                    try:
                        result = search_taobao_goods_by_keyword(
                            search_key=keyword,
                            limit_count=500,  # 0 表示拉取全部可用页，也可按需设为如 200
                            app_key=taobao_app_key,
                            app_secret=taobao_app_secret,
                            adzone_id=taobao_pid,
                        )

                        if not result or result.get("status") != "success":
                            logger.warning(
                                "[淘宝API任务/失败] 搜索未成功 | 关键词: [%s] | 错误: [%s]",
                                keyword,
                                result.get("error") if isinstance(result, dict) else result,
                            )
                            time.sleep(3)
                            continue

                        records = []
                        now = datetime.now(timezone.utc)
                        for item in result.get("data", []):
                            record = normalize_taobao_goods(item, keyword)
                            if record is not None:
                                record["updated_at"] = now
                                records.append(record)

                        if records:
                            counts = safe_batch_update(product_manager, records)
                            logger.info(
                                "[淘宝API任务/入库] 关键词: [%s] | 获取: [%d] | 新增/更新: [%d/%d]",
                                keyword,
                                len(records),
                                counts.get("new", 0),
                                counts.get("update", 0),
                            )
                        else:
                            logger.info("[淘宝API任务/空数据] 关键词: [%s] | 未解析到有效商品", keyword)

                    except Exception as exc:
                        logger.error("[淘宝API任务/异常] 搜索或入库异常 | 关键词: [%s] | 错误: [%s]", keyword, exc)

                    # 每个关键词查询之间停顿，防止触发淘宝 TOP 流控
                    time.sleep(3)

        except Exception as exc:
            logger.error("[淘宝API任务/数据库异常] 连接或全局操作失败 | 错误: [%s]", exc)

        logger.info("[淘宝API任务/轮次结束] 本轮淘宝联盟拉取完成，休眠 24 小时...")
        time.sleep(24 * 3600)
# ==========================================
# 新增：API 数据清洗模块
# ==========================================
# ==========================================
# 修改：API 数据清洗模块 (新增 source_api 参数)
# ==========================================
def normalize_api_goods(item, default_category, source_api="api_search"):
    """
    清洗 API 返回的商品数据，将其转为与数据库已有格式保持一致。
    统一构造默认 sku_info 列表，并打标 need_update_sku: True。
    """
    goods_id = item.get("goods_id")
    if type(goods_id) not in (str, int) or not str(goods_id).strip():
        return None

    # 规范化 _source_api 为列表结构
    if isinstance(source_api, list):
        api_list = [s for s in source_api if s]
    elif isinstance(source_api, (tuple, set)):
        api_list = [s for s in source_api if s]
    elif source_api:
        api_list = [str(source_api)]
    else:
        api_list = ["api_search"]

    clean_goods_id = str(goods_id).strip()
    record = {
        "platform": GLOBAL_CONFIG["platform"],
        "product_id": clean_goods_id,
        "category": item.get("category_name") or default_category,
        "_source_api": api_list,
        "product_url": f"https://mobile.pinduoduo.com/goods.html?goods_id={clean_goods_id}",
    }

    # 基础字段映射 (从 API 字段 -> 数据库字段)
    for source, target in (
            ("goods_sign", "goods_sign"),
            ("goods_name", "name"),
            ("brand_name", "brand"),
            ("sales_tip", "sales_tip"),
            ("goods_image_url", "image_url"),
    ):
        if source in item:
            record[target] = item[source]

    # 价格字段映射 (分 -> 元)
    for source, target in (
            ("min_normal_price", "original_price"),
            ("min_group_price", "activity_price"),
            ("coupon_discount", "saved_price"),
    ):
        if source in item:
            record[target] = round((item[source] or 0) / 100, 2)

    # 【修复】：多多进宝 API 的 min_group_price 为券前拼团价，需减去 coupon_discount (saved_price) 得到真实券后价
    raw_group_price = record.get("activity_price", 0.0)
    saved_price = record.get("saved_price", 0.0)
    act_price = round(max(0.0, raw_group_price - saved_price), 2)
    record["activity_price"] = act_price
    # 统一构造默认单规格 sku_info，并增加 need_update_sku 字段
    record["sku_info"] = [
        {
            "价格": f"￥{act_price}",
            "截图地址": "",
        }
    ]
    record["need_update_sku"] = True

    return record


def normalize_intercept_goods(item, default_category, source_account=""):
    """
    清洗 search_goods_and_intercept 返回的混合商品数据（API Json + Excel 融合字段）。
    统一构造默认 sku_info 列表，并打标 need_update_sku: True。
    """
    if not isinstance(item, dict):
        return None

    # 兼容处理商品ID的取值（API字段 或 Excel融合字段）
    goods_id = item.get("goodsId") or item.get("商品ID")
    if type(goods_id) not in (str, int) or not str(goods_id).strip():
        return None

    # 规范化账户名称（若传入的是完整路径，提取目录名如 biance_pdd_myself）
    clean_account = ""
    if source_account and str(source_account).strip():
        raw_acc = str(source_account).strip()
        clean_account = os.path.basename(os.path.normpath(raw_acc)) or raw_acc

    clean_goods_id = str(goods_id).strip()
    record = {
        "platform": GLOBAL_CONFIG["platform"],
        "product_id": clean_goods_id,
        "category": item.get("categoryName") or default_category,
        "_source_api": ["web_intercept"],
        "source_account": clean_account,
        "product_url": f"https://mobile.pinduoduo.com/goods.html?goods_id={clean_goods_id}",
    }

    # 基础字段映射 (从驼峰或中文表头字段 -> 数据库字段)
    record["name"] = item.get("goodsName") or item.get("商品名称", "")
    record["brand"] = item.get("mallName", "")  # 取店铺名作为 brand 占位
    record["sales_tip"] = str(item.get("salesTip", ""))
    record["image_url"] = item.get("goodsImageUrl") or item.get("goodsThumbnailUrl", "")

    # 价格字段安全映射 (分/千分位 -> 元)
    record["original_price"] = round(_safe_float(item.get("goodsMarkPrice")) / 100, 2)

    # 计算拼团价和优惠券金额
    raw_group_price = round(_safe_float(item.get("minGroupPrice")) / 1000, 2)
    saved_price = round(_safe_float(item.get("couponDiscount")) / 1000, 2)

    record["saved_price"] = saved_price
    activity_price = round(max(0.0, raw_group_price - saved_price), 2)
    record["activity_price"] = activity_price

    # 仅在推广短链接非空时写入，防止空字符串覆盖已有转链或阻断 pending_promotion_query
    short_url = str(item.get("短链接") or "").strip()
    if short_url:
        record["promotion_url"] = short_url

    # 安全提取并计算估算的佣金(元)
    try:
        commission_str = str(item.get("佣金(元)", "0")).replace("元", "").strip()
        est_commission = _safe_float(commission_str, default=0.0)
    except Exception:
        est_commission = 0.0

    # 组装 promotion_info 字典结构
    record["promotion_info"] = {
        "promotion_rate": _safe_float(item.get("promotionRate"), default=0.0),
        "estimated_commission": est_commission,
        "has_mall_coupon": bool(item.get("hasCoupon", False)),
    }

    # 统一构造默认单规格 sku_info，并增加 need_update_sku 字段
    record["sku_info"] = [
        {
            "价格": f"￥{activity_price}",
            "截图地址": "",
        }
    ]
    record["need_update_sku"] = True

    return record

# ==========================================
# 修改：UI 拦截搜索任务模块 (支持 USER_DATA_DIR_LIST 严格顺序容灾切换与来源账户落库)
# ==========================================
def web_search_intercept_task():
    """后台任务：利用 Playwright 拦截指定关键词的商品流数据，严格按 USER_DATA_DIR_LIST 顺序使用与切换账户"""
    default_keywords = [
        "猕猴桃", "蒜", "冲牙器",
        # 基础水饮与酒水
        "可乐", "牛奶", "果汁", "咖啡", "茶叶", "啤酒", "酸奶", "功能饮料", "气泡水",
        "奶茶", "豆奶", "苏打水", "鸡尾酒", "红酒", "白酒", "燕麦奶", "柠檬茶", "凉茶",
        # 休闲零食
        "零食", "饼干", "薯片", "巧克力", "坚果", "糖果", "火腿肠", "牛肉干", "辣条", "冰淇淋",
        "果冻", "话梅", "肉脯", "海苔", "曲奇", "瓜子", "花生", "魔芋爽", "凤爪", "鸭脖",
        # 饱腹代餐与速食
        "方便面", "面包", "麦片", "速冻水饺", "自热火锅", "螺蛳粉", "酸辣粉", "罐头", "蛋黄酥", "手撕面包",
        # 厨房粮油与生鲜调味
        "大米", "面条", "食用油", "酱油", "食盐", "鸡蛋", "蜂蜜", "燕麦片", "火锅底料", "老干妈",
        "陈醋", "白糖", "鸡精", "豆瓣酱", "蚝油", "芝麻酱", "面粉", "粉丝", "紫菜", "干香菇",
        # 家庭日用与清洁
        "抽纸", "卷纸", "湿巾", "洗衣液", "洗洁精", "垃圾袋", "保鲜膜", "保鲜袋", "洁厕灵", "消毒液",
        "柔顺剂", "洗手液", "驱蚊液", "除湿盒", "厨房纸",
        # 个人护理与日化
        "洗发水", "沐浴露", "牙膏", "牙刷", "洗面奶", "护发素", "润唇膏", "身体乳", "卫生巾", "棉签",
        "洗脸巾", "漱口水", "香皂", "剃须刀", "护手霜"
    ]

    stats_file_path = Path("keyword_intercept_stats.json")
    monitored_file = Path(r"W:\project\python_project\easy_shop\app\web_front\monitored_products.json")
    bj_tz = timezone(timedelta(hours=8))

    # 单个账户连续失败多少次后在本轮临时标记为失效（防止死磕已掉线/风控账户）
    max_consecutive_failures_per_account = 2

    while True:
        logger.info("[UI拦截任务/轮次开始] 开始执行 UI 搜索数据拦截...")

        # 0. 严格按照列表顺序初始化并校验可用账户目录列表
        raw_account_list = (
            USER_DATA_DIR_LIST
            if isinstance(USER_DATA_DIR_LIST, (list, tuple)) and USER_DATA_DIR_LIST
            else [USER_DATA_DIR]
        )
        # 保序去重，过滤空字符串
        account_list = list(dict.fromkeys(
            str(acc).strip() for acc in raw_account_list if isinstance(acc, str) and acc.strip()
        ))
        if not account_list:
            logger.error("❌ [UI拦截任务/配置错误] USER_DATA_DIR_LIST 与 USER_DATA_DIR 均为空，等待 60 秒后重试...")
            time.sleep(60)
            continue

        # 1. 健壮读取待搜索关键词（优先读取监控配置，异常或为空时回退到默认词表）
        search_keywords = default_keywords
        if monitored_file.exists():
            try:
                monitored_products = read_json(str(monitored_file))
                if isinstance(monitored_products, dict):
                    monitored_keys = [
                        k for k, v in monitored_products.items()
                        if isinstance(v, dict) and v.get("is_monitoring")
                    ]
                    if monitored_keys:
                        search_keywords = monitored_keys
                        logger.info("[UI拦截任务/配置] 已从监控配置加载 %d 个关键词", len(search_keywords))
            except Exception as cfg_exc:
                logger.warning("[UI拦截任务/配置] 读取监控词文件失败，沿用默认词表 | 错误: [%s]", cfg_exc)

        try:
            # 2. 健壮读取本地历史状态字典
            try:
                current_stats = read_json(str(stats_file_path))
                if not isinstance(current_stats, dict):
                    current_stats = {}
            except Exception as stat_exc:
                logger.warning("[UI拦截任务/状态] 读取历史状态文件异常，重置为空字典 | 错误: [%s]", stat_exc)
                current_stats = {}

            # 3. 过滤 12 小时内已成功搜索 (count > 0) 的关键字
            now_bj = datetime.now(bj_tz)
            pending_keywords = []
            for keyword in search_keywords:
                stat = current_stats.get(keyword)
                if isinstance(stat, dict) and stat.get("count", 0) > 0:
                    last_time_str = stat.get("last_time")
                    if last_time_str:
                        try:
                            last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=bj_tz)
                            if (now_bj - last_time).total_seconds() < 2 * 3600:
                                continue
                        except ValueError:
                            pass
                pending_keywords.append(keyword)

            # 4. 对剩余的待搜关键字进行排序：(是否出现过(未出现为0，已出现为1), 上次拉取的个数(默认0))
            pending_keywords.sort(key=lambda k: (
                1 if k in current_stats else 0,
                current_stats.get(k, {}).get("count", 0) if isinstance(current_stats.get(k), dict) else 0
            ))

            logger.info("\n" + "=" * 50)
            logger.info("🎯 [UI拦截任务] 过滤后本轮需搜索的关键字顺序 (共 %d 个):", len(pending_keywords))
            for idx, k in enumerate(pending_keywords, 1):
                print(f"  {idx:02d}. {k}")
            print("=" * 50 + "\n")

            if not pending_keywords:
                logger.info("[UI拦截任务/待命] 所有关键词均在 12 小时冷却期内，跳过本轮执行")
            else:
                # 记录本轮各账户连续失败次数与临时失效状态
                account_fail_streaks = {acc: 0 for acc in account_list}
                invalid_accounts_in_round = set()

                with closing(gen_db_object()) as db_instance:
                    db_instance.ping()
                    product_manager = ProductManager(db_instance)

                    for keyword in pending_keywords:
                        logger.info("[UI拦截任务/搜索] 正在执行关键字: [%s] 的搜索拦截...", keyword)
                        item_count = 0
                        used_account_name = ""

                        # 若所有账户均被标记为本轮失效，则重置失效集合，重新按列表顺序从头尝试
                        if len(invalid_accounts_in_round) >= len(account_list):
                            logger.warning("[UI拦截任务/重置账户池] 列表中所有账户均已触发失效标记，重置状态并按原列表顺序重新尝试")
                            invalid_accounts_in_round.clear()
                            account_fail_streaks = {acc: 0 for acc in account_list}

                        # 严格按照 USER_DATA_DIR_LIST 的顺序构建本次尝试队列：
                        # 优先按原顺序排入健康账户，已被标记失效的账户仅作为末尾兜底
                        ordered_candidates = (
                            [acc for acc in account_list if acc not in invalid_accounts_in_round]
                            + [acc for acc in account_list if acc in invalid_accounts_in_round]
                        )

                        for attempt_idx, account_dir in enumerate(ordered_candidates, 1):
                            account_name = os.path.basename(os.path.normpath(account_dir)) or account_dir
                            logger.info(
                                "[UI拦截任务/尝试账户] 关键词: [%s] | 顺位尝试: [%d/%d] | 账户: [%s]",
                                keyword, attempt_idx, len(ordered_candidates), account_name
                            )

                            try:
                                intercept_result = search_goods_and_intercept(
                                    search_key_list=[keyword],
                                    user_data_dir=account_dir,
                                    limit_count=1000,
                                    debug=False
                                )

                                item_list = []
                                if isinstance(intercept_result, dict) and keyword in intercept_result:
                                    data = intercept_result[keyword]
                                    item_list = data.get("goodsList", []) if isinstance(data, dict) else (data or [])

                                records = []
                                if item_list and isinstance(item_list, list):
                                    now_utc = datetime.now(timezone.utc)
                                    for item in item_list:
                                        record = normalize_intercept_goods(
                                            item,
                                            default_category=keyword,
                                            source_account=account_name
                                        )
                                        if record is not None:
                                            record["updated_at"] = now_utc
                                            records.append(record)

                                if records:
                                    item_count = len(records)
                                    used_account_name = account_name
                                    # 抓取成功，清零该账户的连续失败计数并移出失效集合
                                    account_fail_streaks[account_dir] = 0
                                    invalid_accounts_in_round.discard(account_dir)

                                    counts = safe_batch_update(product_manager, records)
                                    logger.info(
                                        "[UI拦截任务/入库成功] 关键词: [%s] | 来源账户: [%s] | 获取: [%d] | 新增/更新: [%d/%d]",
                                        keyword, account_name, item_count, counts.get("new", 0), counts.get("update", 0)
                                    )
                                    # 当前账户已成功获取商品，立即跳出账户循环，处理下一个关键词
                                    break
                                else:
                                    logger.warning(
                                        "[UI拦截任务/账户无数据] 关键词: [%s] | 账户: [%s] 未拦截到有效商品",
                                        keyword, account_name
                                    )

                            except Exception as inner_exc:
                                traceback.print_exc()
                                logger.error(
                                    "[UI拦截任务/账户异常] 关键词: [%s] | 账户: [%s] 执行拦截或入库出错 | 错误: [%s]",
                                    keyword, account_name, inner_exc
                                )

                            # 走到这里说明当前账户本次获取失败
                            account_fail_streaks[account_dir] = account_fail_streaks.get(account_dir, 0) + 1
                            if account_fail_streaks[account_dir] >= max_consecutive_failures_per_account:
                                invalid_accounts_in_round.add(account_dir)
                                logger.warning(
                                    "⚠️ [UI拦截任务/账户降级] 账户 [%s] 已连续失败 %d 次，本轮后续关键词将优先顺延使用列表中的下一个账户",
                                    account_name, account_fail_streaks[account_dir]
                                )

                            # 若列表中还有下一个候选账户，短暂间隔后切换下一个账户重试该关键词
                            if attempt_idx < len(ordered_candidates):
                                next_acc_name = os.path.basename(os.path.normpath(ordered_candidates[attempt_idx]))
                                logger.info(
                                    "🔄 [UI拦截任务/切换账户] 关键词: [%s] | 准备切换至列表下一个账户: [%s] ...",
                                    keyword, next_acc_name
                                )
                                time.sleep(2)

                        # 5. 更新本地关键字记录字典并落盘（额外记录本次成功的 source_account 便于排查）
                        current_stats[keyword] = {
                            "last_time": datetime.now(bj_tz).strftime("%Y-%m-%d %H:%M:%S"),
                            "count": item_count,
                            "source_account": used_account_name,
                        }
                        try:
                            stats_file_path.parent.mkdir(parents=True, exist_ok=True)
                            save_json(str(stats_file_path), current_stats)
                        except Exception as save_exc:
                            logger.error("[UI拦截任务/状态落盘失败] 无法写入状态文件 [%s] | 错误: [%s]", stats_file_path, save_exc)

                        # 6. 仅当列表中所有账户都尝试完毕且数量仍为 0 时，才触发深度冷却休眠
                        if item_count == 0:
                            print("\n" + "❗" * 45)
                            print(f"🚨🚨🚨 醒目警报: 关键字 [{keyword}] 遍历全部 {len(account_list)} 个账户后抓取数量仍为 0！🚨🚨🚨")
                            print("⏳ 触发全局风控或限流保护，强制等待 2 分钟 (120秒) 后继续...")
                            print("❗" * 45 + "\n")
                            logger.warning(
                                "[UI拦截任务/冷却保护] 关键字: [%s] 所有账户均未获取到数据，开始深度休眠 120 秒",
                                keyword
                            )
                            time.sleep(120)
                        else:
                            # 正常获取数据，每次搜索间短暂停顿防反爬
                            time.sleep(3)

        except Exception as exc:
            logger.error("[UI拦截任务/全局异常] 数据库连接或执行时发生严重错误 | 错误: [%s]", exc)

        logger.info("[UI拦截任务/轮次结束] 本轮 UI 拦截拉取完成，休眠 10 分钟后进行下一轮检查...")
        time.sleep(1 * 60)

def safe_batch_update(product_manager, records):
    """
    【安全批量更新封装】
    1. 价格与商品基础字段以当前最新抓取的数据为准进行更新。
    2. 对 _source_api 来源列表进行安全合并与追加，保留商品全部历史来源并保序去重。
    3. 【新增：商品变动穿透重置】：若已入库商品的价格 (activity_price) 或名称 (name) 发生变动，
       自动将其当做新商品穿透重置：重置 need_update_sku=True、sku_status="pending"、
       sku_retry_count=0、need_reformat=True 等全套下游控制字段，确保重新触发大模型清洗与 RPA 规格抓取。
    4. 对价格/名称未发生变动的稳定商品，继续保护已有 sku_info 与 need_update_sku，防止被默认占位覆盖。
    5. 保护库中已生成的非空 promotion_url，防止被空值覆盖。
    6. 维护 history_price_info 历史价格列表：每次新增或更新商品时，自动追加当前时间、对应价格与商品名称 (name)。
    """
    if not records:
        return {"new": 0, "update": 0}

    bj_tz = timezone(timedelta(hours=8))

    try:
        product_ids = [
            str(r["product_id"]) for r in records
            if r.get("product_id")
        ]

        if product_ids:
            # 扩展 projection：增加 activity_price 和 original_price，用于识别商品价格变动
            existing_docs = product_manager.query(
                {"product_id": {"$in": list(set(product_ids))}},
                projection={
                    "product_id": 1,
                    "_source_api": 1,
                    "need_update_sku": 1,
                    "sku_status": 1,
                    "promotion_url": 1,
                    "history_price_info": 1,
                    "name": 1,
                    "activity_price": 1,
                    "original_price": 1,
                }
            )

            source_api_map = {}
            existing_doc_map = {}
            history_price_map = {}

            if existing_docs:
                for doc in existing_docs:
                    pid = str(doc.get("product_id"))
                    existing_doc_map[pid] = doc

                    # 提取已有的 _source_api
                    old_source = doc.get("_source_api")
                    if old_source:
                        if isinstance(old_source, list):
                            source_api_map[pid] = [str(item) for item in old_source if item]
                        elif isinstance(old_source, str):
                            source_api_map[pid] = [old_source]
                        else:
                            source_api_map[pid] = [str(old_source)]

                    # 提取已有的 history_price_info
                    old_history = doc.get("history_price_info")
                    if isinstance(old_history, list):
                        history_price_map[pid] = [
                            item for item in old_history if isinstance(item, dict)
                        ]
                    else:
                        history_price_map[pid] = []

            for record in records:
                pid = str(record.get("product_id"))
                old_doc = existing_doc_map.get(pid) or {}

                # 1. 合并 _source_api
                current_source = record.get("_source_api")
                if current_source is not None:
                    if isinstance(current_source, list):
                        new_sources = [str(s) for s in current_source if s]
                    elif isinstance(current_source, str):
                        new_sources = [current_source] if current_source else []
                    else:
                        new_sources = [str(current_source)]

                    existing_sources = source_api_map.get(pid, [])
                    combined_sources = list(dict.fromkeys(existing_sources + new_sources))
                    record["_source_api"] = combined_sources
                    source_api_map[pid] = combined_sources

                # 2. 组装并追加 history_price_info（新增与更新均追加当前时间、对应价格以及商品名称）
                raw_time = record.get("updated_at")
                if isinstance(raw_time, datetime):
                    if raw_time.tzinfo is None:
                        raw_time = raw_time.replace(tzinfo=timezone.utc)
                    time_str = raw_time.astimezone(bj_tz).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    time_str = datetime.now(bj_tz).strftime("%Y-%m-%d %H:%M:%S")

                raw_price = record.get("activity_price")
                if raw_price is None:
                    raw_price = record.get("original_price", 0.0)
                current_price = _safe_float(raw_price, default=0.0)

                # 提取商品名：优先从当前抓取记录获取，缺失时使用旧文档记录兜底
                current_name = str(record.get("name") or old_doc.get("name") or "").strip()

                price_entry = {
                    "time": time_str,
                    "price": current_price,
                    "name": current_name,
                }

                existing_history = history_price_map.get(pid, [])
                combined_history = existing_history + [price_entry]
                record["history_price_info"] = combined_history
                history_price_map[pid] = combined_history

                # 3. 价格与名称变动监测与状态穿透重置
                if old_doc:
                    # 获取旧记录的价格和名称
                    raw_old_price = old_doc.get("activity_price")
                    if raw_old_price is None:
                        raw_old_price = old_doc.get("original_price", 0.0)
                    old_price = _safe_float(raw_old_price, default=0.0)
                    old_name = str(old_doc.get("name") or "").strip()

                    # 判定价格或名称是否发生变化（浮点数比对保留两位精度）
                    is_price_changed = (old_price > 0 and current_price > 0 and abs(current_price - old_price) >= 0.01)
                    is_name_changed = bool(old_name and current_name and old_name != current_name)

                    if is_price_changed or is_name_changed:
                        # 【核心改动】：检测到商品变动，完全视为新商品，重置下游全链路控制字段
                        logger.info(
                            "🔄 [商品变动重置] 商品 [%s] 发生关键信息变更 (价格: %s -> %s, 名称变动: %s) -> 重置为新商品状态",
                            pid, old_price, current_price, is_name_changed
                        )
                        # 重置 RPA SKU 采集全套字段
                        record["need_update_sku"] = True
                        record["sku_status"] = "pending"
                        record["sku_retry_count"] = 0
                        record["sku_error"] = ""
                        record["sku_info"] = [
                            {
                                "价格": f"￥{current_price}",
                                "截图地址": "",
                            }
                        ]

                        # 重置大模型结构化清洗全套字段
                        record["need_reformat"] = True
                        record["format_status"] = "pending"
                        record["format_retry_count"] = 0
                        record["format_error"] = ""
                    else:
                        # 未发生变动时，沿用原保护机制：避免将已确认的多规格数据还原为单规格占位
                        if old_doc.get("need_update_sku") is False or old_doc.get("sku_status") == "success":
                            record.pop("sku_info", None)
                            record.pop("need_update_sku", None)

                    # 若新记录未携带有效推广链接，但库中已有推广链接，绝不覆盖为空
                    if not record.get("promotion_url") and old_doc.get("promotion_url"):
                        record.pop("promotion_url", None)

    except Exception as e:
        logger.error("[安全更新/来源与历史价格合并异常] 查询失败，退回直接入库模式 | 错误: %s", e)
        # 若预查询异常，移除可能半途生成的 history_price_info，防止覆盖丢失库中已有的长历史记录
        for record in records:
            record.pop("history_price_info", None)

    return product_manager.update(records)


# ==========================================
# 修改：UI 拦截搜索任务模块 (单个搜索与新结构适配)
# ==========================================

# ==========================================
# 新增：API 并行任务模块
# ==========================================
# ==========================================
# 修改：API 并行任务模块（支持本地 JSON 状态与 12 小时冷却跳过）
# ==========================================
def api_search_task():
    """后台任务：通过 API 搜索指定关键词商品并入库，维护本地状态，跳过 12 小时内已成功搜索的关键词"""
    default_keywords = [
        # 基础水饮与酒水 (20个)
        "可乐", "牛奶", "矿泉水", "果汁", "咖啡", "茶叶", "啤酒", "酸奶", "功能饮料", "气泡水",
        "奶茶", "豆奶", "苏打水", "纯净水", "鸡尾酒", "红酒", "白酒", "燕麦奶", "柠檬茶", "凉茶",

        # 休闲零食 (20个)
        "零食", "饼干", "薯片", "巧克力", "坚果", "糖果", "火腿肠", "牛肉干", "辣条", "冰淇淋",
        "果冻", "话梅", "肉脯", "海苔", "曲奇", "瓜子", "花生", "魔芋爽", "凤爪", "鸭脖",

        # 饱腹代餐与速食 (10个)
        "方便面", "面包", "麦片", "速冻水饺", "自热火锅", "螺蛳粉", "酸辣粉", "罐头", "蛋黄酥", "手撕面包",

        # 厨房粮油与生鲜调味 (20个)
        "大米", "面条", "食用油", "酱油", "食盐", "鸡蛋", "蜂蜜", "燕麦片", "火锅底料", "老干妈",
        "陈醋", "白糖", "鸡精", "豆瓣酱", "蚝油", "芝麻酱", "面粉", "粉丝", "紫菜", "干香菇",

        # 家庭日用与清洁 (15个)
        "抽纸", "卷纸", "湿巾", "洗衣液", "洗洁精", "垃圾袋", "保鲜膜", "保鲜袋", "洁厕灵", "消毒液",
        "柔顺剂", "洗手液", "驱蚊液", "除湿盒", "厨房纸",

        # 个人护理与日化 (15个)
        "洗发水", "沐浴露", "牙膏", "牙刷", "洗面奶", "护发素", "润唇膏", "身体乳", "卫生巾", "棉签",
        "洗脸巾", "漱口水", "香皂", "剃须刀", "护手霜"
    ]

    pdd_client_id = get_config("myself_pdd_client_id")
    pdd_client_secret = get_config("myself_pdd_client_secret")
    pdd_pid = get_config("myself_pdd_pid")

    # 本地 API 搜索状态记录文件
    stats_file_path = Path("keyword_api_search_stats.json")
    monitored_file = Path(r"W:\project\python_project\easy_shop\app\web_front\monitored_products.json")
    bj_tz = timezone(timedelta(hours=8))

    while True:
        logger.info("[API任务/轮次开始] 开始执行 API 数据拉取流程...")

        # 1. 加载待搜索的关键词（优先使用监控文件中的关键词）
        search_keywords = default_keywords
        if monitored_file.exists():
            try:
                monitored_products = read_json(str(monitored_file)) or {}
                monitored_keys = [
                    k for k, v in monitored_products.items()
                    if isinstance(v, dict) and v.get("is_monitoring")
                ]
                if monitored_keys:
                    search_keywords = monitored_keys
                    logger.info("[API任务/配置] 已从监控配置中加载 %d 个有效监控词", len(search_keywords))
            except Exception as e:
                logger.warning("[API任务/配置] 读取监控配置文件失败，退回默认词表 | 错误: [%s]", e)

        try:
            # 2. 读取本地历史状态 JSON
            current_stats = read_json(str(stats_file_path)) or {}
            now_bj = datetime.now(bj_tz)

            # 3. 过滤 12 小时内已成功搜索 (count > 0) 的关键词
            pending_keywords = []
            for keyword in search_keywords:
                stat = current_stats.get(keyword)
                if stat and stat.get("count", 0) > 0:
                    last_time_str = stat.get("last_time")
                    if last_time_str:
                        try:
                            last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=bj_tz)
                            # 如果距离上次成功搜索小于 12 小时，则跳过
                            if (now_bj - last_time).total_seconds() < 12 * 3600:
                                continue
                        except ValueError:
                            # 格式异常则不跳过
                            pass

                pending_keywords.append(keyword)

            # 4. 排序：未拉取过的排前面，拉取商品数少的排前面
            pending_keywords.sort(key=lambda k: (
                1 if k in current_stats else 0,
                current_stats.get(k, {}).get("count", 0)
            ))

            logger.info("\n" + "=" * 50)
            logger.info("🎯 [API任务] 过滤后本轮需执行 API 搜索的关键词 (共 %d 个):", len(pending_keywords))
            for idx, k in enumerate(pending_keywords, 1):
                print(f"  {idx:02d}. {k}")
            print("=" * 50 + "\n")

            # 5. 若有待拉取的关键词，建立数据库连接进行拉取和入库
            if pending_keywords:
                with closing(gen_db_object()) as db_instance:
                    db_instance.ping()
                    product_manager = ProductManager(db_instance)

                    for keyword in pending_keywords:
                        logger.info("[API任务/搜索] 正在拉取关键词: [%s]", keyword)
                        item_count = 0  # 记录本次拉取成功的有效商品数

                        try:
                            result = search_pdd_goods_by_keyword(
                                client_id=pdd_client_id,
                                client_secret=pdd_client_secret,
                                pid=pdd_pid,
                                search_key=keyword,
                                limit_count=500  # 可根据需求调整拉取数量
                            )

                            if result and result.get("status") == "success":
                                records = []
                                now_utc = datetime.now(timezone.utc)
                                for item in result.get("data", []):
                                    record = normalize_api_goods(item, keyword)
                                    if record is not None:
                                        record["updated_at"] = now_utc
                                        records.append(record)

                                if records:
                                    item_count = len(records)
                                    counts = safe_batch_update(product_manager, records)
                                    logger.info(
                                        "[API任务/入库] 关键词: [%s] | 获取: [%d] | 新增/更新: [%d/%d]",
                                        keyword, item_count, counts.get("new", 0), counts.get("update", 0)
                                    )
                                else:
                                    logger.info("[API任务/空数据] 关键词: [%s] | 未解析到有效商品", keyword)
                            else:
                                logger.warning(
                                    "[API任务/失败] 搜索未成功 | 关键词: [%s] | 响应: [%s]",
                                    keyword, result.get("error") if isinstance(result, dict) else result
                                )

                        except Exception as exc:
                            logger.error("[API任务/异常] 搜索或入库异常 | 关键词: [%s] | 错误: [%s]", keyword, exc)

                        # 6. 更新本地状态并持久化（若失败则 count 为 0，不会被 12 小时规则跳过）
                        current_stats[keyword] = {
                            "last_time": datetime.now(bj_tz).strftime("%Y-%m-%d %H:%M:%S"),
                            "count": item_count
                        }
                        stats_file_path.parent.mkdir(parents=True, exist_ok=True)
                        save_json(str(stats_file_path), current_stats)

                        # 避免频繁请求触发接口风控，关键词之间短暂停顿
                        time.sleep(5)
            else:
                logger.info("[API任务/待命] 所有关键词均在 12 小时冷却期内，跳过本轮执行")

        except Exception as exc:
            logger.error("[API任务/数据库异常] 数据库连接或全局操作失败 | 错误: [%s]", exc)

        # 调整为每 30 分钟轮询一次，保证冷却期过后的关键词能及时被拾起
        logger.info("[API任务/轮次结束] 本轮处理完成，休眠 30 分钟后检查冷却到期的关键词...")
        time.sleep(1 * 60)

def normalize_goods(item, tab_name):
    """
    清洗分类流拦截的单个商品对象。
    统一构造默认 sku_info 列表，并打标 need_update_sku: True。
    确保 _source_api 列表中一定包含 "group"。
    """
    goods_id = item.get("goods_id")
    if type(goods_id) not in (str, int) or not str(goods_id).strip():
        return None

    # 兼容处理 _source_api：若原数据已存在该字段则追加 "group"，不存在则初始化为 ["group"]
    raw_source = item.get("_source_api")
    if isinstance(raw_source, (list, tuple, set)):
        source_api_list = [str(s).strip() for s in raw_source if s and str(s).strip()]
    elif isinstance(raw_source, str) and raw_source.strip():
        source_api_list = [raw_source.strip()]
    else:
        source_api_list = []

    if "group" not in source_api_list:
        source_api_list.append("group")

    record = {
        "platform": GLOBAL_CONFIG["platform"],
        "product_id": str(goods_id).strip(),
        "category": tab_name,
        "_source_api": source_api_list,
    }
    for source, target in (
            ("goods_name", "name"), ("brand_name", "brand"), ("sales_tip", "sales_tip"), ("hd_thumb_url", "image_url"),
    ):
        if source in item:
            record[target] = item[source]
    for source, target in (
            ("origin_price", "original_price"), ("activity_price", "activity_price"),
            ("group_order_price_reduce", "saved_price"),
    ):
        if source in item:
            record[target] = round((item[source] or 0) / 100, 2)
    if "link_url" in item:
        record["product_url"] = urljoin("https://mobile.pinduoduo.com/", item["link_url"] or "")

    act_price = record.get("activity_price", 0.0)

    # 统一构造默认单规格 sku_info，并增加 need_update_sku 字段
    record["sku_info"] = [
        {
            "价格": f"￥{act_price}",
            "截图地址": "",
        }
    ]
    record["need_update_sku"] = True

    return record
def save_error_snapshot(page, tab_name, reason):
    """page 为 Playwright Page；尽力保存 PNG/HTML，保留快照失败不阻断主流程的容错。"""
    try:
        os.makedirs("error_data", exist_ok=True)
        safe_name = "".join(char for char in f"{tab_name}_{reason}" if char.isalnum() or char in "_-") or "unknown"
        prefix = os.path.join("error_data", f"{datetime.now():%Y%m%d_%H%M%S_%f}_{safe_name}")
        page.screenshot(path=f"{prefix}.png", full_page=True)
        with open(f"{prefix}.html", "w", encoding="utf-8") as snapshot:
            snapshot.write(page.content())
        logger.info("[现场/保存] 截图与源码已保存 | 分类: [%s] | 原因: [%s] | 文件前缀: [%s]", tab_name, reason, prefix)
    except Exception as exc:
        logger.warning("[现场/失败] 保存页面失败，继续原流程 | 分类: [%s] | 错误: [%s] "
                       "| 排查: [页面是否关闭、目录权限]", tab_name, exc)


def check_risk_control(page):
    """沿用两个原有风控文案；首个匹配项避免重复节点触发严格定位异常。"""
    try:
        return any(page.get_by_text(text, exact=True).first.is_visible()
                   for text in ("活动陆续开放中", "回到首页"))
    except Exception as exc:
        # : 原规则在检测异常时按未命中处理；页面关闭或 DOM 异常可能漏报风控。
        logger.warning("[风控/检测] 无法读取提示文案，沿用未命中结果 | 错误: [%s] | 排查: [页面状态、文案节点]", exc)
        return False


def check_logged_out(page):
    """【新增功能】检测页面是否已重定向至登录页或呈现登录文案"""
    try:
        # 1. 检查URL特征
        if "login" in page.url.lower():
            return True
        # 2. 检查常见掉登录/未登录强制弹出的文案
        for text in ("手机号登录", "密码登录", "登 录", "获取验证码", "一键登录"):
            if page.get_by_text(text, exact=True).first.is_visible():
                return True
        return False
    except Exception as exc:
        logger.warning("[登录/检测] 无法读取登录状态文案，视为未掉登录 | 错误: [%s]", exc)
        return False


def get_tab_list(user_data_dir):
    """探测导航；返回 [{index: DOM 下标, name: 分类名}]，掉线返回 LOGGED_OUT，其余失败返回 None。"""
    account_name = os.path.basename(user_data_dir)
    started = time.monotonic()
    logger.info("[导航/探测] 开始读取分类 | 账号: [%s]", account_name)
    with sync_playwright() as p, closing(launch_persistent_context(
            p, user_data_dir=user_data_dir, headless=GLOBAL_CONFIG["headless_mode"])) as context:
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(GLOBAL_CONFIG["target_url"], wait_until="domcontentloaded")

            # 【新增】捕获节点寻找时的异常，如果是登录页则提早退出
            try:
                page.locator("#brand-first-nav").wait_for(state="visible", timeout=15000)
            except Exception as e:
                if check_logged_out(page):
                    logger.warning("[导航/拦截] 检测到账号掉登录，需重新扫码 | 账号: [%s]", account_name)
                    return "LOGGED_OUT"
                raise e

            page.wait_for_timeout(3000)

            # 【新增】正常加载完毕后，做一次登录确认
            if check_logged_out(page):
                logger.warning("[导航/拦截] 检测到账号掉登录，需重新扫码 | 账号: [%s]", account_name)
                return "LOGGED_OUT"

            if check_risk_control(page):
                logger.warning("[导航/拦截] 首页出现风控提示，重新申请账号 | 账号: [%s] "
                               "| 排查: [账号访问限制]", account_name)
                return None
            tabs = page.evaluate(r"""
                () => Array.from(document.querySelectorAll('#brand-first-nav > div')).map((tab, index) => {
                    let name = tab.innerText.replace('\n', '').trim();
                    if (!name) {
                        const img = tab.querySelector('img');
                        name = img ? (img.getAttribute('aria-label') || img.getAttribute('alt')
                                      || '图片标签_' + index) : '';
                    }
                    return {index, name: name.trim() || `未知标签_${index}`};
                })
            """)
            if not tabs:
                logger.warning("[导航/空结果] 未找到分类，继续探测 | 账号: [%s] "
                               "| 排查: [导航结构变化、页面尚未渲染]", account_name)
                return tabs
            logger.info("[导航/完成] 分类读取成功 | 账号: [%s] | 分类: [%s] | 耗时: [%.1f 秒]",
                        account_name, "; ".join(f"{tab['index']}:{tab['name']}" for tab in tabs),
                        time.monotonic() - started)
            return tabs
        except Exception as exc:
            # 【新增】代码崩溃时最后查验是否是掉登录引起的异常
            try:
                if check_logged_out(page):
                    logger.warning("[导航/拦截] 检测到账号掉登录，需重新扫码 | 账号: [%s]", account_name)
                    return "LOGGED_OUT"
            except Exception:
                pass

            logger.warning("[导航/失败] 读取分类失败，重新申请账号 | 账号: [%s] | 错误: [%s] "
                           "| 排查: [网络、导航节点、脚本执行]", account_name, exc)
            return None


def clear_popups(page):
    """按图片、按钮、ESC、遮罩的原有顺序关闭弹窗；保留失败后继续采集的容错。"""
    try:
        page.wait_for_timeout(1500)
        for keyword in ("多人团限时优惠", "立即抢购", "限时优惠", "爆款商品"):
            popup = page.locator(f"text='{keyword}'").first
            if popup.is_visible():
                break
        else:
            return
        # : 保留“含文案的末个 div 内首张图片”规则；它可能是商品图，需要确认关闭按钮特征。
        close_image = page.locator("div").filter(has_text=keyword).last.locator("img").first
        if close_image.is_visible():
            close_image.click(force=True)
            page.wait_for_timeout(1000)
            if not popup.is_visible():
                logger.info("[弹窗/完成] 活动弹窗已关闭 | 文案: [%s] | 方式: [容器图片]", keyword)
                return
        for selector in ("text='关闭'", "text='跳过'", "[class*='close' i]", ".am-modal-close"):
            button = page.locator(selector).first
            if not button.is_visible():
                continue
            button.click(force=True)
            page.wait_for_timeout(800)
            if not popup.is_visible():
                logger.info("[弹窗/完成] 活动弹窗已关闭 | 文案: [%s] | 方式: [%s]", keyword, selector)
                return
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
        if not popup.is_visible():
            logger.info("[弹窗/完成] 活动弹窗已关闭 | 文案: [%s] | 方式: [ESC]", keyword)
            return
        # : 保留固定坐标点击；布局变化可能误点，第二次点击后仍不额外等待确认。
        page.mouse.click(10, 10)
        page.wait_for_timeout(800)
        if not popup.is_visible():
            logger.info("[弹窗/完成] 活动弹窗已关闭 | 文案: [%s] | 方式: [遮罩坐标]", keyword)
            return
        page.mouse.click(10, 200)
        logger.warning("[弹窗/待确认] 常规关闭方式未成功，已尝试备用坐标 | 文案: [%s] "
                       "| 排查: [弹窗结构、遮罩位置]", keyword)
    except Exception as exc:
        logger.warning("[弹窗/失败] 自动关闭发生异常，继续采集 | 错误: [%s] "
                       "| 排查: [按钮定位、页面跳转]", exc)


def scrape_single_tab(user_data_dir, tab_info, product_manager):
    """tab_info 必含 index/name，可含 round/tab_index/total_tabs；返回 (状态, {scrolls, requests, new, update})。
    响应形貌为 {success, result: {goods_list: [dict]}}；存储异常经回调交回主流程并在释放浏览器后上抛。
    """
    tab_name = tab_info["name"]
    tab_display = (f"第{tab_info.get('round', 1)}轮-第{tab_info.get('tab_index', 1)}/"
                   f"{tab_info.get('total_tabs', 1)}个({tab_name})")
    account_name = os.path.basename(user_data_dir)
    stats = dict.fromkeys(STAT_KEYS, 0)
    storage_error = None
    hit_risk = False
    started = time.monotonic()
    logger.info("[采集/开始] 准备加载目标分类 | 分类: [%s] | 账号: [%s]", tab_display, account_name)

    is_ready_to_collect = False

    def check_storage():
        """把事件回调的存储异常交回同步控制流，禁止换号重试掩盖入库故障。"""
        if storage_error is not None:
            raise StorageError(f"账号 [{account_name}] 分类 [{tab_display}] 商品写入或回执处理失败；"
                               "请检查 Mongo 连接、写入权限和 ProductManager.update 回执") from storage_error

    def handle_response(response):
        """仅处理目标接口 HTTP 200；清洗补丁后就地累计 stats，存储异常留给 check_storage 上抛。"""
        nonlocal hit_risk, storage_error, is_ready_to_collect

        if not is_ready_to_collect:
            return

        if storage_error is not None or "brand-group-home/home/goods_list" not in response.url or response.status != 200:
            return
        stats["requests"] += 1
        try:
            data = response.json()
            if not isinstance(data, dict) or not data.get("success") or not isinstance(data.get("result"), dict):
                return
            hit_risk = hit_risk or "risk" in str(data).lower()
            records = []
            now = datetime.now(timezone.utc)
            for item in data["result"].get("goods_list", []) or []:
                if not isinstance(item, dict):
                    continue
                record = normalize_goods(item, tab_name)
                if record is not None:
                    record["updated_at"] = now
                    records.append(record)
        except Exception as exc:
            logger.warning("[采集/解析] 本批未入库，继续监听 | 分类: [%s] | 错误: [%s] "
                           "| 排查: [JSON 结构、价格字段]", tab_display, exc)
            return
        if not records:
            return
        try:
            counts = safe_batch_update(product_manager, records)
            new, updated = stats["new"] + counts["new"], stats["update"] + counts["update"]
            stats.update(new=new, update=updated)
        except Exception as exc:
            storage_error = exc

    try:
        with sync_playwright() as p, closing(launch_persistent_context(
                p, user_data_dir=user_data_dir, headless=GLOBAL_CONFIG["headless_mode"])) as context:
            page = context.pages[0] if context.pages else context.new_page()
            page.on("response", handle_response)
            try:
                page.goto(GLOBAL_CONFIG["target_url"], wait_until="domcontentloaded")
                check_storage()
                navigation = page.locator("#brand-first-nav")
                try:
                    navigation.wait_for(state="visible", timeout=15000)
                except Exception as nav_exc:
                    if check_logged_out(page):
                        save_error_snapshot(page, tab_name, "账号掉登录")
                        return "LOGGED_OUT", stats
                    raise nav_exc

                page.wait_for_timeout(3000)
                check_storage()
                if check_logged_out(page):
                    save_error_snapshot(page, tab_name, "账号掉登录")
                    return "LOGGED_OUT", stats
                if check_risk_control(page):
                    if stats["requests"] < 10:
                        save_error_snapshot(page, tab_name, "风控拦截_请求不足10次")
                    return "RISK_CONTROL", stats
                clear_popups(page)
                check_storage()
                navigation.wait_for(state="visible", timeout=5000)

                clicked = page.evaluate("""
                    (targetName) => {
                        const tabs = Array.from(document.querySelectorAll('#brand-first-nav > div'));
                        for (let index = 0; index < tabs.length; index++) {
                            let tab = tabs[index];
                            let name = tab.innerText.replace('\\n', '').trim();
                            if (!name) {
                                const img = tab.querySelector('img');
                                name = img ? (img.getAttribute('aria-label') || img.getAttribute('alt') || '图片标签_' + index) : '';
                            }
                            name = name.trim() || `未知标签_${index}`;

                            if (name === targetName) {
                                tab.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """, tab_name)

                if not clicked:
                    logger.warning("[采集/跳过] 当前账号无目标分类标签，放弃该分类 | 账号: [%s] | 目标: [%s]",
                                   account_name, tab_name)
                    return "PAGE_MISMATCH", stats

                page.wait_for_timeout(3500)
                check_storage()

                if page.locator("input[type='search']").first.is_visible():
                    save_error_snapshot(page, tab_name, "异常跑偏_误入搜索页")
                    return "PAGE_MISMATCH", stats

                is_ready_to_collect = True

                max_scrolls = GLOBAL_CONFIG["max_scrolls_per_tab"]
                stop_reason = "达到配置滑动上限"
                idle_scrolls = 0
                last_response_count = stats["requests"]
                while True:
                    check_storage()
                    if max_scrolls != -1 and stats["scrolls"] >= max_scrolls:
                        break
                    if check_risk_control(page) or hit_risk:
                        if stats["requests"] < 10:
                            save_error_snapshot(page, tab_name, "滑动拦截_请求不足10次")
                        return "RISK_CONTROL", stats
                    page.mouse.wheel(0, GLOBAL_CONFIG["scroll_step_y"])
                    stats["scrolls"] += 1
                    page.wait_for_timeout(GLOBAL_CONFIG["scroll_interval"] * 1000)
                    check_storage()
                    if max_scrolls == -1:
                        idle_scrolls = 0 if stats["requests"] > last_response_count else idle_scrolls + 1
                        last_response_count = stats["requests"]
                        if idle_scrolls >= 10:
                            stop_reason = "连续十次滑动无新目标 HTTP 200 响应"
                            break
                    if stats["scrolls"] % 10 == 0:
                        limit = "不限" if max_scrolls == -1 else max_scrolls
                        logger.info("[采集/进度] 分类加载中 | 分类: [%s] | 滑动: [%d/%s] | 空转: [%d] "
                                    "| 响应: [%d] | 新增/更新: [%d/%d]", tab_display, stats["scrolls"], limit,
                                    idle_scrolls, stats["requests"], stats["new"], stats["update"])
                if stats["requests"] < 10:
                    save_error_snapshot(page, tab_name, "正常结束_请求不足10次")
            except StorageError:
                raise
            except Exception as exc:
                check_storage()
                if check_logged_out(page):
                    save_error_snapshot(page, tab_name, "账号掉登录")
                    return "LOGGED_OUT", stats
                logger.warning("[采集/异常] 页面作业失败，重新申请账号 | 分类: [%s] | 账号: [%s] "
                               "| 错误: [%s] | 排查: [网络、导航或点击目标]", tab_display, account_name, exc)
                save_error_snapshot(page, tab_name, "代码崩溃异常")
                return "ERROR", stats
    finally:
        check_storage()
    if stats["requests"] == 0:
        return "PAGE_MISMATCH", stats
    logger.info("[采集/完成] 分类作业结束 | 分类: [%s] | 账号: [%s] | 滑动/响应: [%d/%d] "
                "| 新增/更新: [%d/%d] | 耗时: [%.1f 秒] | 结束依据: [%s]", tab_display, account_name,
                stats["scrolls"], stats["requests"], stats["new"], stats["update"], time.monotonic() - started,
                stop_reason)
    return "SUCCESS", stats


def run_collection_rounds(account_pool, product_manager):
    """账号池负责冷却和落盘；分类含 index/name，跨尝试累计 scrolls/requests/new/update。"""
    round_count = 0
    while True:
        round_count += 1
        started = time.monotonic()
        logger.info("[调度/轮次开始] 准备探测并遍历分类 | 轮次: [%d]", round_count)
        tabs = None
        while not isinstance(tabs, list) or not tabs:
            probe_account = account_pool.acquire("探测分类")
            tabs = get_tab_list(probe_account)
            if tabs == "LOGGED_OUT":
                account_pool.mark_invalid(probe_account, "LOGGED_OUT")
                tabs = None
                time.sleep(2)
            elif not tabs:
                time.sleep(5)
        round_stats = []
        for tab_index, tab in enumerate(tabs, 1):
            tab_info = dict(tab, round=round_count, tab_index=tab_index, total_tabs=len(tabs))
            tab_display = f"第{round_count}轮-第{tab_index}/{len(tabs)}个({tab['name']})"
            totals = dict.fromkeys(STAT_KEYS, 0)
            for attempt in range(1, MAX_ATTEMPTS_PER_TAB + 1):
                account = account_pool.acquire(tab_display)
                status, stats = scrape_single_tab(account, tab_info, product_manager)
                if status == "LOGGED_OUT":
                    account_pool.mark_invalid(account, "LOGGED_OUT")
                for key in STAT_KEYS:
                    totals[key] += stats.get(key, 0)
                if status != "SUCCESS":
                    reason = STATUS_REASONS.get(status, f"未知状态({status})")
                    exhausted = attempt == MAX_ATTEMPTS_PER_TAB
                    log = logger.error if exhausted else logger.warning
                    message = "❌ [调度/跳过] 分类失败达到上限，结束本分类" if exhausted else "[调度/重试] 分类尚未完成，重新申请账号"
                    log("%s | 分类: [%s] | 账号: [%s] | 尝试: [%d/%d] | 原因: [%s] "
                        "| 排查: [账号限制、页面结构或异常快照]", message, tab_display,
                        os.path.basename(account), attempt, MAX_ATTEMPTS_PER_TAB, reason)
                time.sleep(2)
                if status == "SUCCESS":
                    break
            round_stats.append({"tab_name": tab["name"], "status": status, **totals})
        totals = {key: sum(item[key] for item in round_stats) for key in STAT_KEYS}
        successful = sum(item["status"] == "SUCCESS" for item in round_stats)
        details = "; ".join(
            f"{item['tab_name']}[状态={item['status']}, 滑动/响应={item['scrolls']}/{item['requests']}, "
            f"新增/更新={item['new']}/{item['update']}]" for item in round_stats)
        logger.info("[调度/轮次完成] 分类遍历结束，进入下一轮 | 轮次: [%d] | 成功/跳过: [%d/%d] "
                    "| 滑动/响应: [%d/%d] | 新增/更新: [%d/%d] | 耗时: [%.1f 秒] | 分类明细: [%s]",
                    round_count, successful, len(round_stats) - successful, totals["scrolls"], totals["requests"],
                    totals["new"], totals["update"], time.monotonic() - started, details)


def main_controller():
    """读取现有目录配置并启动采集；账号锁与数据库连接在中断、异常和正常退出时释放。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    try:
        accounts = get_config("pdd_browser_data_list")
        if not accounts:
            logger.error("❌ [系统/启动失败] 未配置采集账号 | 配置项: [pdd_browser_data_list] | 排查: [浏览器目录列表]")
            return
        with AccountPool(accounts, GLOBAL_CONFIG["account_status_file"]) as account_pool, closing(
                gen_db_object()) as db_instance:
            db_instance.ping()
            product_manager = ProductManager(db_instance)
            logger.info("[系统/就绪] 商品数据库与本地账号池已就绪 | 配置账号: [%d] | 账号文件: [%s]",
                        len(account_pool.accounts), account_pool.path)
            run_collection_rounds(account_pool, product_manager)
    except KeyboardInterrupt:
        logger.info("[系统/退出] 收到中断，停止采集任务")
    except Exception as exc:
        logger.exception("❌ [系统/终止] 采集器异常退出，本轮未完成 | 错误: [%s] "
                         "| 排查: [配置、账号 JSON/文件锁、MongoDB 或浏览器初始化]", exc)
        raise


# ==========================================
# 修改：重构原有的 main_controller -> playwright_task
# ==========================================
def playwright_task():
    """后台任务：读取现有目录配置并启动 UI 抓取采集 (原 main_controller)"""
    accounts = get_config("pdd_browser_data_list")
    if not accounts:
        logger.error("❌ [系统/启动失败] 未配置采集账号 | 配置项: [pdd_browser_data_list] | 排查: [浏览器目录列表]")
        return
    with AccountPool(accounts, GLOBAL_CONFIG["account_status_file"]) as account_pool, closing(
            gen_db_object()) as db_instance:
        db_instance.ping()
        product_manager = ProductManager(db_instance)
        logger.info("[系统/就绪] 商品数据库与本地账号池已就绪 | 配置账号: [%d] | 账号文件: [%s]",
                    len(account_pool.accounts), account_pool.path)
        run_collection_rounds(account_pool, product_manager)


# ==========================================
# 修改：新增线程调度与守护逻辑
# ==========================================
def _run_task(task):
    """为后台入口的未处理异常补充上下文并重抛；保留线程退出、不自动重启的行为。"""
    try:
        task()
    except Exception:
        logger.exception(
            "[任务/退出] 后台任务异常结束 | 任务: [%s] | 结果: [当前线程停止] "
            "| 排查: [检查对应链路的数据、文件权限及外部服务]",
            task.__name__,
        )
        raise


# ==========================================
# 新增：API 推荐全盘横扫任务模块
# ==========================================
# ==========================================
# 新增：API 推荐全盘横扫任务模块
# ==========================================
def api_recommend_task():
    """后台任务：利用封装好的自动翻页引擎 get_pdd_recommend_goods 横扫推荐榜单。
    每遍历完一个频道/分类，立刻清洗落库，防止因中途网络异常或封禁导致全局数据丢失。
    """
    from app.pdd_utils import get_pdd_recommend_goods

    pdd_client_id = get_config("myself_pdd_client_id")
    pdd_client_secret = get_config("myself_pdd_client_secret")
    pdd_pid = get_config("myself_pdd_pid")

    # 定义要遍历的频道 (1:今日热销, 5:实时热销, 6:实时收益, 4:猜你喜欢)
    target_channels = [1, 5, 6, 4]

    # 频道 4 (猜你喜欢) 的细分类目 ID 列表
    cat_id_list = [
        20100, 20200, 20300, 20400, 20500, 20600, 20700, 20800, 20900,
        21000, 21100, 21200, 21300, 21400, 21500, 21600, 21700, 21800
    ]

    while True:
        logger.info("[推荐API任务/轮次开始] 开始执行推荐商品数据全盘拉取...")
        try:
            with closing(gen_db_object()) as db_instance:
                db_instance.ping()
                product_manager = ProductManager(db_instance)

                for channel in target_channels:
                    current_cat_list = cat_id_list if channel == 4 else [None]

                    for cid in current_cat_list:
                        category_name = f"推荐榜单_ch{channel}" + (f"_cat{cid}" if cid else "")
                        logger.info("[推荐API任务/拉取] 正在调用底层引擎拉取大类: [%s] ...", category_name)

                        res = get_pdd_recommend_goods(
                            client_id=pdd_client_id,
                            client_secret=pdd_client_secret,
                            pid=pdd_pid,
                            channel_type=channel,
                            limit_count=0,
                            cat_id=cid
                        )

                        if res.get("error"):
                            logger.warning("[推荐API任务/容错] 接口返回异常(本分类跳过): %s", res["error"])
                            continue

                        raw_items = res.get("data", [])
                        if not raw_items:
                            logger.info("[推荐API任务/空数据] [%s] 该分类/榜单暂无推荐数据", category_name)
                            continue

                        records = []
                        now = datetime.now(timezone.utc)
                        for item in raw_items:
                            record = normalize_api_goods(item, category_name, source_api=["api_recommend"])
                            if record is not None:
                                record["updated_at"] = now
                                records.append(record)

                        if records:
                            counts = safe_batch_update(product_manager, records)
                            logger.info("[推荐API任务/落库] [%s] 采集结束 | 新增/更新: [%d/%d] | 标志: [api_recommend]",
                                        category_name, counts.get("new", 0), counts.get("update", 0))

                        time.sleep(3)

        except Exception as exc:
            logger.error("[推荐API任务/数据库异常] 连接或全局操作失败 | 错误: [%s]", exc)

        logger.info("[推荐API任务/轮次结束] 本轮推荐横扫完成，休眠 24 小时等待下一次全盘拉取...")
        time.sleep(24 * 3600)

if __name__ == "__main__":

    # intercept_result = search_goods_and_intercept(
    #     search_key_list=["正宗渝兄重庆怪味胡豆"],
    #     user_data_dir=USER_DATA_DIR,
    #     debug=True
    # )

    # 配置基础日志 (将其提取到最外层，共享给所有线程)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")

    # 可以通过注释掉下面的某一行，非常灵活地控制启停哪个任务
    tasks = [
        playwright_task,
        # taobao_api_search_task,  # 【新增】：淘宝联盟 API 关键词搜索任务

        api_search_task,
        # api_recommend_task,         # 商品推荐 API 任务
        web_search_intercept_task  # 【新增】：UI 关键词拦截搜索任务
    ]

    threads = []
    for task in tasks:
        thread = threading.Thread(target=_run_task, args=(task,), name=task.__name__)
        thread.daemon = True  # 设置为守护线程，这样主线程因中断退出时，所有任务也会立即中止
        thread.start()
        threads.append(thread)
        logger.info("[系统/启动] 已启动 %s 线程 (TID: %d)", task.__name__, thread.ident)

    try:
        # 使用带 timeout 的 join 轮询，避免完全阻塞主线程，使得 Ctrl+C 中断信号能够被正常捕获
        for thread in threads:
            while thread.is_alive():
                thread.join(1.0)
    except KeyboardInterrupt:
        logger.info("[系统/退出] 收到中断信号，正在停止所有后台并行任务...")