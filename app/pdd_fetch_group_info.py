# [功能摘要] 使用本地冷却账号池逐分类采集拼多多商品，将收到的字段增量写入 MongoDB。
# [输入数据] pdd_browser_data_list 浏览器目录；本地账号 JSON；导航 DOM；goods_list 接口 JSON。
# [数据流转/交互] 锁定本地状态文件 → 按原顺序选账号并记录开始时间 → 探测分类 → 监听 HTTP 200
#                 响应 → 清洗实际收到的字段 → ProductManager.update；页面失败按原规则换号重试。
# [输出数据] products 中的商品补丁、账号 ISO 时间 JSON、分类统计及异常 PNG/HTML；存储故障释放资源后上抛。

import json
import logging
import os
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from app.pdd_utils import search_pdd_goods_by_keyword, get_pdd_recommend_goods
from common.playwright_utils import launch_persistent_context, search_goods_and_intercept
from common.common_utils import get_config, read_json, save_json
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager

USER_DATA_DIR = r"W:\temp\biance_pdd_myself"

logger = logging.getLogger("pdd_scraper")

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
# 新增：API 数据清洗模块
# ==========================================
# ==========================================
# 修改：API 数据清洗模块 (新增 source_api 参数)
# ==========================================
def normalize_api_goods(item, default_category, source_api="api_search"):
    """
    清洗 API 返回的商品数据，将其转为与数据库已有格式 (ProductManager.update 所需) 保持一致。
    价格字段（原价、拼团价、优惠券）除以 100 转为元。
    """
    goods_id = item.get("goods_id")
    if type(goods_id) not in (str, int) or not str(goods_id).strip():
        return None

    record = {
        "platform": GLOBAL_CONFIG["platform"],
        "product_id": str(goods_id).strip(),
        # 如果 API 没有返回 category_name，则用搜索关键词或外部定义分类保底
        "category": item.get("category_name") or default_category,
        "_source_api": source_api  # 【核心修改】：支持动态指定来源，如 api_recommend
    }

    # 基础字段映射 (从 API 字段 -> 数据库字段)
    for source, target in (
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
            record[target] = (item[source] or 0) / 100

    return record


# ==========================================
# 修改：UI 拦截数据清洗模块 (新增 url 与 dict 结构)
# ==========================================
def normalize_intercept_goods(item, default_category):
    """
    清洗 search_goods_and_intercept 返回的混合商品数据（API Json + Excel 融合字段）。
    将其转为与数据库已有格式保持一致，并新增推广链接与佣金详情集合。
    """
    # 兼容处理商品ID的取值（API字段 或 Excel融合字段）
    goods_id = item.get("goodsId") or item.get("商品ID")
    if type(goods_id) not in (str, int) or not str(goods_id).strip():
        return None

    record = {
        "platform": GLOBAL_CONFIG["platform"],
        "product_id": str(goods_id).strip(),
        "category": item.get("categoryName") or default_category,
        "_source_api": "web_intercept"  # 标识数据来源为 UI 拦截
    }

    # 基础字段映射 (从驼峰或中文表头字段 -> 数据库字段)
    record["name"] = item.get("goodsName") or item.get("商品名称", "")
    record["brand"] = item.get("mallName", "")  # 取店铺名作为 brand 占位
    record["sales_tip"] = str(item.get("salesTip", ""))
    record["image_url"] = item.get("goodsImageUrl") or item.get("goodsThumbnailUrl", "")

    # 价格字段映射 (千分位 -> 元)
    record["original_price"] = (item.get("goodsMarkPrice") or 0) / 1000
    record["activity_price"] = (item.get("minGroupPrice") or 0) / 1000
    record["saved_price"] = (item.get("couponDiscount") or 0) / 1000

    # ================= 新增要求映射 =================
    # 1. 提取短链接为 promotion_url
    record["promotion_url"] = item.get("短链接", "")

    # 2. 安全提取并计算估算的佣金(元)，去除多余的字符串
    try:
        commission_str = str(item.get("佣金(元)", "0")).replace("元", "").strip()
        est_commission = float(commission_str) if commission_str else 0.0
    except Exception:
        est_commission = 0.0

    # 3. 组装 promotion_info 字典结构
    record["promotion_info"] = {
        "promotion_rate": item.get("promotionRate", 0),
        "estimated_commission": est_commission,
        "has_mall_coupon": item.get("hasCoupon", False)
    }

    return record

# ==========================================
# 修改：UI 拦截搜索任务模块 (单个搜索与新结构适配)
# ==========================================
def web_search_intercept_task():
    """后台任务：利用 Playwright 拦截指定关键词的商品流数据，每轮等待 24 小时"""
    search_keywords = [
        "猕猴桃",
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

    # 状态记录文件路径，与账号 JSON 放在同一 data 目录下
    stats_file_path = Path("keyword_intercept_stats.json")
    while True:
        logger.info("[UI拦截任务/轮次开始] 开始执行 UI 搜索数据拦截...")
        try:
            # 1. 读取历史记录字典
            current_stats = read_json(str(stats_file_path)) or {}

            # 2. 对关键字进行排序
            # 排序规则：(是否出现过(未出现为0，已出现为1), 上次拉取的个数(默认0))
            # 这样保证：从未拉取过的在最前；拉取过的按数量升序排列（越少越靠前）
            search_keywords.sort(key=lambda k: (
                1 if k in current_stats else 0,
                current_stats.get(k, {}).get("count", 0)
            ))

            logger.info(f"[UI拦截任务/排序完成] 即将拉取的前5个关键字预览: {search_keywords[:5]}")

            # 在最外层建立数据库连接，避免内层循环反复重连
            with closing(gen_db_object()) as db_instance:
                db_instance.ping()
                product_manager = ProductManager(db_instance)

                # 遍历排序后的关键词列表
                for keyword in search_keywords:
                    logger.info(f"[UI拦截任务/搜索] 正在执行关键字: [{keyword}] 的搜索拦截...")
                    item_count = 0  # 记录本次拉取的商品数量

                    try:
                        # 包装成单元素列表传入
                        intercept_result = search_goods_and_intercept(
                            search_key_list=[keyword],
                            user_data_dir=USER_DATA_DIR,
                            debug=False
                        )

                        if intercept_result and keyword in intercept_result:
                            data = intercept_result[keyword]

                            # 适配新数据结构，提取字典内部的 goodsList
                            if isinstance(data, dict):
                                item_list = data.get("goodsList", [])
                            else:
                                item_list = data

                            if not item_list:
                                logger.info("[UI拦截任务/空数据] 关键词: [%s] | 未拦截到商品", keyword)
                            else:
                                records = []
                                now = datetime.now(timezone.utc)
                                for item in item_list:
                                    record = normalize_intercept_goods(item, keyword)
                                    if record is not None:
                                        record["updated_at"] = now
                                        records.append(record)

                                if records:
                                    item_count = len(records)
                                    counts = product_manager.update(records)
                                    logger.info("[UI拦截任务/入库] 关键词: [%s] | 获取: [%d] | 新增/更新: [%d/%d]",
                                                keyword, item_count, counts.get("new", 0), counts.get("update", 0))
                        else:
                            logger.warning("[UI拦截任务/失败] 关键词: [%s] 拦截工具未返回有效数据", keyword)

                    except Exception as inner_exc:
                        logger.error("[UI拦截任务/单次异常] 执行关键词 [%s] 拦截或落库时发生错误 | 错误: [%s]", keyword,
                                     inner_exc)

                    # 3. 更新本地关键字记录字典并落盘
                    bj_tz = timezone(timedelta(hours=8))
                    current_stats[keyword] = {
                        "last_time": datetime.now(bj_tz).strftime("%Y-%m-%d %H:%M:%S"),  # 北京时间
                        "count": item_count
                    }
                    stats_file_path.parent.mkdir(parents=True, exist_ok=True)
                    save_json(str(stats_file_path), current_stats)

                    # 4. 判断本次是否为0，触发冷却预警或正常休眠
                    if item_count == 0:
                        print("\n" + "❗" * 45)
                        print(f"🚨🚨🚨 醒目警报: 关键字 [{keyword}] 本次抓取数量为 0！🚨🚨🚨")
                        print("⏳ 触发风控或限流保护，强制等待 2 分钟 (120秒) 后继续...")
                        print("❗" * 45 + "\n")
                        logger.warning("[UI拦截任务/冷却保护] 关键字: [%s] 数量为0，开始深度休眠 120 秒", keyword)
                        time.sleep(120)
                    else:
                        # 正常数据，给每次搜索独立操作间增加喘息时间，防反爬
                        time.sleep(3)

        except Exception as exc:
            logger.error("[UI拦截任务/全局异常] 数据库连接或执行时发生严重错误 | 错误: [%s]", exc)

        logger.info("[UI拦截任务/轮次结束] 本轮 UI 拦截拉取完成，休眠 24 小时...")
        time.sleep(24 * 3600)

# ==========================================
# 新增：API 并行任务模块
# ==========================================
def api_search_task():
    """后台任务：通过 API 搜索指定关键词商品并入库，每轮等待 24 小时"""
    keywords = [
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

    while True:
        logger.info("[API任务/轮次开始] 开始执行 API 数据拉取...")
        try:
            # 每次拉取建立独立的数据库连接（由于等待时间长达24h，保持长连接易引发断联报错）
            with closing(gen_db_object()) as db_instance:
                db_instance.ping()
                product_manager = ProductManager(db_instance)

                for keyword in keywords:
                    logger.info("[API任务/搜索] 正在拉取关键词: [%s]", keyword)
                    try:
                        result = search_pdd_goods_by_keyword(
                            client_id=pdd_client_id,
                            client_secret=pdd_client_secret,
                            pid=pdd_pid,
                            search_key=keyword,
                            limit_count=0  # 可根据需求调整拉取数量
                        )

                        if not result or result.get("status") != "success":
                            logger.warning("[API任务/失败] 搜索未成功 | 关键词: [%s] | 响应: [%s]", keyword, result)
                            continue

                        records = []
                        now = datetime.now(timezone.utc)
                        for item in result.get("data", []):
                            record = normalize_api_goods(item, keyword)
                            if record is not None:
                                record["updated_at"] = now
                                records.append(record)

                        if records:
                            counts = product_manager.update(records)
                            logger.info("[API任务/入库] 关键词: [%s] | 获取: [%d] | 新增/更新: [%d/%d]",
                                        keyword, len(records), counts.get("new", 0), counts.get("update", 0))
                        else:
                            logger.info("[API任务/空数据] 关键词: [%s] | 未解析到有效商品", keyword)

                    except Exception as exc:
                        logger.error("[API任务/异常] 搜索或入库异常 | 关键词: [%s] | 错误: [%s]", keyword, exc)

                    # 避免并发打满，每个关键词查询之间短暂停顿
                    time.sleep(5)

        except Exception as exc:
            logger.error("[API任务/数据库异常] 连接或全局操作失败 | 错误: [%s]", exc)

        logger.info("[API任务/轮次结束] 本轮 API 拉取完成，休眠 24 小时...")
        time.sleep(24 * 3600)


def normalize_goods(item, tab_name):
    """item 核心键为 goods_id，价格来源为 origin_price/activity_price/group_order_price_reduce（分）。
    返回 {platform, product_id, category, 实际收到的商品字段}；非法 ID 返回 None，缺失字段不补空值。
    """
    goods_id = item.get("goods_id")
    if type(goods_id) not in (str, int) or not str(goods_id).strip():
        return None
    record = {"platform": GLOBAL_CONFIG["platform"], "product_id": str(goods_id).strip(), "category": tab_name}
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
            record[target] = (item[source] or 0) / 100
    if "link_url" in item:
        record["product_url"] = urljoin("https://mobile.pinduoduo.com/", item["link_url"] or "")
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

    # 【新增开关】：数据收集控制开关，防止提前写入默认的“首页”数据
    is_ready_to_collect = False

    def check_storage():
        """把事件回调的存储异常交回同步控制流，禁止换号重试掩盖入库故障。"""
        if storage_error is not None:
            raise StorageError(f"账号 [{account_name}] 分类 [{tab_display}] 商品写入或回执处理失败；"
                               "请检查 Mongo 连接、写入权限和 ProductManager.update 回执") from storage_error

    def handle_response(response):
        """仅处理目标接口 HTTP 200；清洗补丁后就地累计 stats，存储异常留给 check_storage 上抛。"""
        nonlocal hit_risk, storage_error, is_ready_to_collect

        # 【新增拦截】：如果还未确认点击到目标分类并等待残余请求过期，直接丢弃所有响应，防污染！
        if not is_ready_to_collect:
            return

        if storage_error is not None or "brand-group-home/home/goods_list" not in response.url or response.status != 200:
            return
        # : requests 仍统计所有目标 HTTP 200，包含无效 JSON、失败业务响应与空批次。
        stats["requests"] += 1
        try:
            data = response.json()
            if not isinstance(data, dict) or not data.get("success") or not isinstance(data.get("result"), dict):
                return
            # : 保留成功响应全文匹配 risk；商品文字可能误报，失败响应的风控可能漏报。
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
            # : 保留一个商品价格异常便跳过整批的规则，不擅自改成逐商品容错。
            logger.warning("[采集/解析] 本批未入库，继续监听 | 分类: [%s] | 错误: [%s] "
                           "| 排查: [JSON 结构、价格字段]", tab_display, exc)
            return
        if not records:
            return
        try:
            counts = product_manager.update(records)
            new, updated = stats["new"] + counts["new"], stats["update"] + counts["update"]
            stats.update(new=new, update=updated)
        except Exception as exc:
            storage_error = exc

    try:
        with sync_playwright() as p, closing(launch_persistent_context(
                p, user_data_dir=user_data_dir, headless=GLOBAL_CONFIG["headless_mode"])) as context:
            page = context.pages[0] if context.pages else context.new_page()
            # : 监听虽然前置，但已被 is_ready_to_collect 开关阻断旧数据
            page.on("response", handle_response)
            try:
                page.goto(GLOBAL_CONFIG["target_url"], wait_until="domcontentloaded")
                check_storage()
                # : 保留先等待导航再查风控；缺少导航的拦截页仍按 ERROR 处理。
                navigation = page.locator("#brand-first-nav")
                navigation.wait_for(state="visible", timeout=15000)
                page.wait_for_timeout(3000)
                check_storage()
                if check_risk_control(page):
                    if stats["requests"] < 10:
                        save_error_snapshot(page, tab_name, "风控拦截_请求不足10次")
                    return "RISK_CONTROL", stats
                clear_popups(page)
                check_storage()
                navigation.wait_for(state="visible", timeout=5000)

                # 【核心修复】：抛弃 index 点击，通过在当前页面执行完全对称的 JS 代码，根据名称精准匹配标签
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
                    # 彻底解决千人千面：如果这个账号确实没这个分类，安全跳过，留给后续账号
                    logger.warning("[采集/跳过] 当前账号无目标分类标签，放弃该分类 | 账号: [%s] | 目标: [%s]",
                                   account_name, tab_name)
                    return "PAGE_MISMATCH", stats

                # 【核心防御】：点击完毕后，强制等待，让首页滞留的网络请求彻底被抛弃，同时等待新分类数据触发
                page.wait_for_timeout(3500)
                check_storage()

                if page.locator("input[type='search']").first.is_visible():
                    save_error_snapshot(page, tab_name, "异常跑偏_误入搜索页")
                    return "PAGE_MISMATCH", stats

                # 【核心开启】：万事俱备，放行拦截器，接下来的数据才是纯净的目标分类数据！
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
                        # : 连续十次无新 HTTP 200 即结束；慢响应或断网可能被误判为到底。
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
                logger.warning("[采集/异常] 页面作业失败，重新申请账号 | 分类: [%s] | 账号: [%s] "
                               "| 错误: [%s] | 排查: [网络、导航或点击目标]", tab_display, account_name, exc)
                save_error_snapshot(page, tab_name, "代码崩溃异常")
                return "ERROR", stats
    finally:
        # 关闭浏览器时仍可能执行回调；存储故障优先于提前返回与关闭异常。
        check_storage()
    if stats["requests"] == 0:
        return "PAGE_MISMATCH", stats
    # : 仍以存在目标 HTTP 200 判成功；空商品或全部解析失败也可能返回 SUCCESS。
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
        while not tabs:
            tabs = get_tab_list(account_pool.acquire("探测分类"))
            if tabs is None:
                time.sleep(5)
        round_stats = []
        for tab_index, tab in enumerate(tabs, 1):
            tab_info = dict(tab, round=round_count, tab_index=tab_index, total_tabs=len(tabs))
            tab_display = f"第{round_count}轮-第{tab_index}/{len(tabs)}个({tab['name']})"
            totals = dict.fromkeys(STAT_KEYS, 0)
            # : 每分类最多三次失败后仍推进下一分类；被跳过不代表采集成功。
            for attempt in range(1, MAX_ATTEMPTS_PER_TAB + 1):
                account = account_pool.acquire(tab_display)
                status, stats = scrape_single_tab(account, tab_info, product_manager)
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
        details = "; ".join(f"{item['tab_name']}[状态={item['status']}, 滑动/响应={item['scrolls']}/{item['requests']}, "
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
        with AccountPool(accounts, GLOBAL_CONFIG["account_status_file"]) as account_pool, closing(gen_db_object()) as db_instance:
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
    with AccountPool(accounts, GLOBAL_CONFIG["account_status_file"]) as account_pool, closing(gen_db_object()) as db_instance:
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
    from app.pdd_utils import get_pdd_recommend_goods  # 确保导入你的函数

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
                    # 如果是频道4，则遍历类目；如果是其他榜单，无需传类目 (传 [None])
                    current_cat_list = cat_id_list if channel == 4 else [None]

                    for cid in current_cat_list:
                        # 组合出分类名称作为默认 Category 落库
                        category_name = f"推荐榜单_ch{channel}" + (f"_cat{cid}" if cid else "")
                        logger.info("[推荐API任务/拉取] 正在调用底层引擎拉取大类: [%s] ...", category_name)

                        # 调用你的自翻页实现，limit_count=0 代表拉干为止
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

                        # 【核心转换】：你的函数吐出的是 format_unified_response 的大一统数据
                        # 我们需要将其适配进 ProductManager 的 MongoDB 字段规范
                        records = []
                        now = datetime.now(timezone.utc)
                        for item in raw_items:
                            goods_id = item.get("goods_id")
                            if not goods_id:
                                continue

                            record = {
                                "platform": GLOBAL_CONFIG["platform"],
                                "product_id": str(goods_id).strip(),
                                "category": item.get("category_name") or category_name,
                                "_source_api": "api_recommend",  # 【要求实现】来源强行覆盖为 api_recommend
                                "name": item.get("goods_name", ""),
                                "brand": item.get("brand_name", ""),
                                "sales_tip": str(item.get("sales_tip", "")),
                                "image_url": item.get("goods_image_url") or item.get("goods_thumbnail_url", ""),
                                # 大一统数据价格单位是分，入库前除以 100 转为元
                                "original_price": item.get("min_normal_price", 0) / 100,
                                "activity_price": item.get("min_group_price", 0) / 100,
                                "saved_price": item.get("coupon_discount", 0) / 100,
                                "updated_at": now
                            }
                            records.append(record)

                        # 【核心安全设计】：每次调完 get_pdd_recommend_goods 立刻落库，即使后续其他分类崩溃，本分类也已保存
                        if records:
                            counts = product_manager.update(records)
                            logger.info("[推荐API任务/落库] [%s] 采集结束 | 新增/更新: [%d/%d] | 标志: [api_recommend]",
                                        category_name, counts.get("new", 0), counts.get("update", 0))

                        # 防护机制：不同的大类之间请求休眠 3 秒，防止被判机器人封禁IP
                        time.sleep(3)

        except Exception as exc:
            logger.error("[推荐API任务/数据库异常] 连接或全局操作失败 | 错误: [%s]", exc)

        logger.info("[推荐API任务/轮次结束] 本轮推荐横扫完成，休眠 12 小时等待下一次全盘拉取...")
        time.sleep(24 * 3600)



if __name__ == "__main__":

    # 配置基础日志 (将其提取到最外层，共享给所有线程)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")

    # 可以通过注释掉下面的某一行，非常灵活地控制启停哪个任务
    tasks = [
        playwright_task,
        # api_search_task,
        # api_recommend_task,         # 商品推荐 API 任务
        web_search_intercept_task    # 【新增】：UI 关键词拦截搜索任务
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