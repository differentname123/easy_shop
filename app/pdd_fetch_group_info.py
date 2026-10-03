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

from common.playwright_utils import launch_persistent_context
from common.common_utils import get_config
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager

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
}


class StorageError(RuntimeError):
    """商品写入或回执处理失败必须终止采集，不能误当成页面问题换号。"""


class AccountPool:
    """accounts 为目录列表；JSON 形貌为 {平台: {绝对目录: ISO 时间字符串}}。
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
                raise RuntimeError(f"账号文件无法加锁 [{self.path}]；请检查是否有采集器正在使用此文件或目录权限不足") from exc
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
                if last_used is not None:
                    try:
                        last_used = datetime.fromisoformat(last_used)
                    except (TypeError, ValueError):
                        # : 原规则允许使用时间格式异常的账号，可能绕过冷却；保留并显式提醒。
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


def get_tab_list(user_data_dir):
    """探测导航；返回 [{index: DOM 下标, name: 分类名}]，页面失败返回 None，空导航返回 []。"""
    account_name = os.path.basename(user_data_dir)
    started = time.monotonic()
    logger.info("[导航/探测] 开始读取分类 | 账号: [%s]", account_name)
    with sync_playwright() as p, closing(launch_persistent_context(
            p, user_data_dir=user_data_dir, headless=GLOBAL_CONFIG["headless_mode"])) as context:
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(GLOBAL_CONFIG["target_url"], wait_until="domcontentloaded")
            # : 保留先等导航再查风控的顺序；没有导航的拦截页仍进入页面异常路径。
            page.locator("#brand-first-nav").wait_for(state="visible", timeout=15000)
            page.wait_for_timeout(3000)
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
                        account_name, "; ".join(f"{tab['index']}:{tab['name']}" for tab in tabs), time.monotonic() - started)
            return tabs
        except Exception as exc:
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

    def check_storage():
        """把事件回调的存储异常交回同步控制流，禁止换号重试掩盖入库故障。"""
        if storage_error is not None:
            raise StorageError(f"账号 [{account_name}] 分类 [{tab_display}] 商品写入或回执处理失败；"
                               "请检查 Mongo 连接、写入权限和 ProductManager.update 回执") from storage_error

    def handle_response(response):
        """仅处理目标接口 HTTP 200；清洗补丁后就地累计 stats，存储异常留给 check_storage 上抛。"""
        nonlocal hit_risk, storage_error
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
            # : 监听仍早于分类点击，首页和迟到响应仍归入目标分类；请求归属需业务确认。
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
                # : 沿用探测账号的 DOM 下标；不同账号的导航顺序若不同，可能点错分类。
                navigation.locator(":scope > div").nth(tab_info["index"]).evaluate("node => node.click()")
                page.wait_for_timeout(3500)
                check_storage()
                if page.locator("input[type='search']").first.is_visible():
                    save_error_snapshot(page, tab_name, "异常跑偏_误入搜索页")
                    return "PAGE_MISMATCH", stats
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
                stats["scrolls"], stats["requests"], stats["new"], stats["update"], time.monotonic() - started, stop_reason)
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


if __name__ == "__main__":
    main_controller()
