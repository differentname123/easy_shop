#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
好单库「近2h销量」活动商品 —— 批量转链 + 社群素材保存
活动页：https://www.haodanku.com/Activity/index?id=24

功能流程：
  1. 调用活动商品列表接口（网站内部接口，无需 apikey），按「近2h销量」
     排序拉取商品，支持指定页数（默认 1 页，每页 20 条）。
  2. 对每个商品调用好单库 ratesurl 转链接口，生成自己的「二合一链接」
     和「淘口令」（带券商品会自动绑定 activityid 防亏券）。
  3. 下载商品主图 + 详情图集到当前目录的 material/ 文件夹。
  4. 每个商品生成一个 JSON（data/{itemid}.json），包含：
     商品唯一标识、二合一链接、淘口令、真实种草文案、素材图片本地地址列表。
  5. 处理前若发现该商品已存在完整 JSON，则自动跳过，支持断点续跑。

用法：
  python3 haodanku_activity_crawler.py            # 默认拉取第 1 页
  python3 haodanku_activity_crawler.py 4          # 拉取 1~4 页（共约 80 个商品）
  python3 haodanku_activity_crawler.py 2 --start-page 3   # 从第 3 页开始拉 2 页
=====================================================================
"""

import argparse
import json
import logging
import os
import time

import requests

# ==================== 1. 日志格式配置（排查必备） ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("HDK_Activity")

# ==================== 2. 专属配置信息 ====================
HAODANKU_APIKEY = "9D3E0E43E5FD"  # 好单库专属 APIKEY
MY_PID = "mm_328750149_3161250458_115832850163"  # 淘宝联盟 PID
TB_NAME = "不同名字123"  # 好单库推广中心已授权的淘宝账号昵称

# ==================== 3. 活动与接口配置 ====================
ACTIVITY_ID = 24          # 活动 id（对应近2h销量所在的双11活动页）
PAGE_SIZE = 20            # 每页商品数（页面固定 20）
SORT_TYPE = 1             # 排序：1=近2h销量（页面默认选中项）
MAX_RETRY = 3             # 失败重试次数
REQUEST_GAP = 0.3         # 每个商品处理间隔（秒），礼貌限速避免触发风控

# 活动商品列表接口（网站内部接口，浏览器抓包所得；无需 apikey）
LIST_API = "https://www.haodanku.com/HolidayActivity/weal_list"
# 好单库官方「淘联商品转链」接口
RATES_API = "http://v3.api.haodanku.com/ratesurl"

# 输出目录（基于脚本所在目录，保证在任意工作目录下运行都一致）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MATERIAL_DIR = os.path.join(BASE_DIR, "material")  # 素材图片目录
DATA_DIR = os.path.join(BASE_DIR, "data")          # 每商品 JSON 目录

# Content-Type 到文件扩展名的映射（淘宝图床常返回 webp，需按真实类型保存）
CTYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


class HaoDanKuConverter:
    """好单库转链：根据商品 itemid 生成二合一链接与淘口令。"""

    def __init__(self, apikey: str, pid: str, tb_name: str):
        self.apikey = apikey
        self.pid = pid
        self.tb_name = tb_name
        self.session = requests.Session()
        # 模拟桌面浏览器，避免被反爬拦截
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )

    def _post_with_retry(self, url: str, data: dict) -> dict:
        """带重试的 POST，返回解析后的 JSON。"""
        last_err = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                resp = self.session.post(url, data=data, timeout=15)
                return resp.json()
            except Exception as e:  # 网络异常 / JSON 解析失败
                last_err = e
                logger.warning(
                    "[转链重试] 第 %d/%d 次请求失败: %s",
                    attempt, MAX_RETRY, e,
                )
                time.sleep(1 * attempt)
        raise last_err

    def convert_item(self, itemid: str, activityid: str, title: str) -> dict:
        """
        请求好单库 ratesurl 转链接口。
        :param itemid: 淘系加密商品 ID
        :param activityid: 优惠券 ID（无券则传空）
        :param title: 商品标题（生成淘口令必填，需大于 8 字符）
        """
        # 好单库规定：返回淘口令时 title 必填且需大于 8 字符
        if not title or len(title) < 8:
            title = f"{title or ''}【超值爆款正品特惠推荐】"

        payload = {
            "apikey": self.apikey,
            "tb_name": self.tb_name,
            "pid": self.pid,
            "itemid": itemid,
            "get_taoword": 1,  # 同时生成淘口令
            "title": title,
        }
        # 防亏券机制：带券商品强行绑定原券；不带券则由好单库自动抓取最大券
        if activityid:
            payload["activityid"] = activityid

        logger.info(
            "[发起转链] itemid=%s, activityid=%s", itemid, activityid or "无"
        )
        try:
            res_json = self._post_with_retry(RATES_API, payload)
        except Exception as e:
            return {"status": False, "msg": f"转链请求异常: {e}"}

        # 好单库成功码为 1 或 200
        if res_json.get("code") in (1, 200, "1", "200"):
            data = res_json.get("data", {})
            # coupon_click_url：二合一长链接（同时完成领券与下单绑定）
            two_in_one_url = (
                data.get("coupon_click_url")
                or data.get("item_url")
                or data.get("tbk_url")
            )
            taoword = data.get("taoword") or data.get("new_model")
            coupon_money = data.get("couponmoney", 0)
            logger.info(
                "[转链成功] 二合一链接已生成, 券面额: %s 元", coupon_money
            )
            return {
                "status": True,
                "two_in_one_url": two_in_one_url,
                "taoword": taoword,
                "coupon_money": coupon_money,
            }

        err_msg = res_json.get("msg") or "未知错误"
        logger.error("[转链失败] %s", err_msg)
        return {"status": False, "msg": err_msg}


class ActivityCrawler:
    """活动商品列表拉取、素材下载、JSON 落盘主流程。"""

    def __init__(self, converter: HaoDanKuConverter):
        self.converter = converter
        self.session = requests.Session()
        # 列表接口需带 Referer 与 AJAX 标识，模拟页面自身请求
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": f"https://www.haodanku.com/Activity/index?id={ACTIVITY_ID}",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }
        )
        os.makedirs(MATERIAL_DIR, exist_ok=True)
        os.makedirs(DATA_DIR, exist_ok=True)

    # ---------------- 列表拉取 ----------------
    def fetch_page(self, page_no: int) -> dict:
        """拉取指定页的商品数据，返回 data 对象。"""
        params = {
            "cat_id": 0,
            "type": SORT_TYPE,
            "page_no": page_no,
            "page_size": PAGE_SIZE,
            "activity_id": ACTIVITY_ID,
            # 以下筛选字段页面会原样带上，保持空值即可
            "startprice": "",
            "endprice": "",
            "start_tkrates": "",
            "end_tkrates": "",
            "coupon": "",
            "start_endprice": "",
        }
        last_err = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                resp = self.session.get(LIST_API, params=params, timeout=15)
                res_json = resp.json()
                if res_json.get("code") == 200:
                    return res_json["data"]
                last_err = res_json.get("msg") or "接口返回非 200"
            except Exception as e:
                last_err = e
            logger.warning(
                "[列表重试] 第 %d 页 %d/%d 次失败: %s",
                page_no, attempt, MAX_RETRY, last_err,
            )
            time.sleep(1 * attempt)
        raise RuntimeError(f"第 {page_no} 页拉取失败: {last_err}")

    # ---------------- 素材下载 ----------------
    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        return url

    def _download_image(self, img_url: str, itemid: str, order: int) -> str:
        """下载单张图片，按真实 Content-Type 决定扩展名，返回本地绝对路径。"""
        img_url = self._normalize_url(img_url)
        resp = self.session.get(img_url, timeout=20)
        resp.raise_for_status()

        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        ext = CTYPE_EXT.get(ctype)
        if not ext:
            # 兜底：尝试从 URL 路径取合法扩展名，否则默认 .jpg
            url_path = img_url.split("?")[0].lower()
            ext = next(
                (e for e in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
                 if url_path.endswith(e)),
                ".jpg",
            )
            if ext == ".jpeg":
                ext = ".jpg"

        filename = f"{itemid}_{order:02d}{ext}"
        local_path = os.path.join(MATERIAL_DIR, filename)
        with open(local_path, "wb") as f:
            f.write(resp.content)
        return local_path

    # ---------------- 单个商品处理 ----------------
    def process_item(self, item: dict) -> str:
        """
        处理单个商品，返回状态：ok / skip / fail。
        """
        itemid = item.get("itemid", "").strip()
        if not itemid:
            logger.warning("[跳过异常] 商品缺少 itemid: %s", item.get("id"))
            return "fail"

        json_path = os.path.join(DATA_DIR, f"{itemid}.json")

        # 断点续跑：已存在且字段完整的 JSON 直接跳过
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    old = json.load(f)
                if old.get("two_in_one_url") and "material_images" in old:
                    logger.info("[已处理跳过] %s (%s)", itemid,
                                old.get("item_title", "")[:20])
                    return "skip"
            except Exception:
                logger.warning("[JSON 损坏] 将重新处理: %s", json_path)

        # 1) 转链
        title = item.get("itemtitle") or item.get("itemshorttitle") or ""
        conv = self.converter.convert_item(
            itemid=itemid,
            activityid=item.get("activityid") or "",
            title=title,
        )
        if not conv.get("status"):
            # 转链失败不写 JSON，下次运行会自动重试
            logger.error("[商品处理失败] %s: %s", itemid, conv.get("msg"))
            return "fail"

        # 2) 收集并去重素材图（主图 + 详情图集），保持顺序
        img_urls = []
        if item.get("itempic"):
            img_urls.append(item["itempic"])
        if item.get("images"):
            img_urls.extend([u for u in item["images"].split(",") if u.strip()])

        seen, uniq_urls = set(), []
        for u in img_urls:
            u = self._normalize_url(u)
            if u not in seen:
                seen.add(u)
                uniq_urls.append(u)

        local_paths = []
        for i, u in enumerate(uniq_urls, start=1):
            try:
                local_paths.append(self._download_image(u, itemid, i))
            except Exception as e:
                # 单张失败不阻断整体，记录后继续
                logger.warning("[图片下载失败] %s -> %s: %s", itemid, u, e)

        # 3) 真实种草文案：优先 itemdesc，缺失时回退到商品标题
        real_content = (
            (item.get("itemdesc") or "").strip()
            or (item.get("itemtitle") or item.get("itemshorttitle") or "").strip()
        )

        # 4) 组装并落盘 JSON（先写临时文件再替换，防止中途中断产生坏文件）
        record = {
            "item_id": itemid,                  # 商品唯一标识（淘系加密ID）
            "hdk_id": item.get("id"),           # 好单库内部ID
            "item_title": item.get("itemtitle"),
            "two_in_one_url": conv["two_in_one_url"],   # 转换后的二合一链接
            "taoword": conv["taoword"],                 # 淘口令
            "real_content": real_content,               # 真实种草文案
            "material_images": local_paths,             # 素材图片本地地址列表
            "source_image_urls": uniq_urls,             # 原始图片URL（便于核对）
            "coupon_money": conv.get("coupon_money") or item.get("couponmoney"),
            "item_price": item.get("itemprice"),        # 原价
            "item_end_price": item.get("itemendprice"),  # 券后价
            "sale_2h": item.get("itemsale2"),           # 近2小时销量
            "tkrates": item.get("tkrates"),             # 佣金比例
            "tkmoney": item.get("tkmoney"),             # 佣金金额
            "shop_name": item.get("shopname"),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        tmp_path = json_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, json_path)
        logger.info(
            "[已保存] %s | 图片 %d 张 | %s",
            itemid, len(local_paths), (record["item_title"] or "")[:24],
        )
        return "ok"

    # ---------------- 主流程 ----------------
    def run(self, pages: int, start_page: int = 1) -> dict:
        stats = {"ok": 0, "skip": 0, "fail": 0}
        end_page = start_page + pages - 1
        logger.info(
            "开始执行：活动 id=%s，排序=近2h销量，拉取第 %d~%d 页",
            ACTIVITY_ID, start_page, end_page,
        )

        for page_no in range(start_page, end_page + 1):
            logger.info("============ 拉取第 %d 页商品列表 ============", page_no)
            try:
                data = self.fetch_page(page_no)
            except Exception as e:
                logger.error(e)
                stats["fail"] += PAGE_SIZE
                continue

            items = data.get("items", [])
            logger.info(
                "本页 %d 个商品（活动总商品 %s，共 %s 页）",
                len(items), data.get("total"), data.get("page_total"),
            )
            for item in items:
                try:
                    stats[self.process_item(item)] += 1
                except Exception as e:
                    logger.error("[商品异常] %s: %s", item.get("itemid"), e)
                    stats["fail"] += 1
                time.sleep(REQUEST_GAP)

        logger.info(
            "全部完成：成功 %d，跳过 %d，失败 %d",
            stats["ok"], stats["skip"], stats["fail"],
        )
        return stats


# ==================== 4. 运行入口 ====================
def main():
    parser = argparse.ArgumentParser(
        description="好单库近2h销量活动商品批量转链与社群素材保存"
    )
    parser.add_argument(
        "pages",
        nargs="?",
        type=int,
        default=1,
        help="要拉取的页数，默认 1（每页 20 个商品）",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="起始页码，默认 1",
    )
    args = parser.parse_args()

    if args.pages < 1:
        parser.error("页数必须大于等于 1")
    if args.start_page < 1:
        parser.error("起始页码必须大于等于 1")

    converter = HaoDanKuConverter(
        apikey=HAODANKU_APIKEY, pid=MY_PID, tb_name=TB_NAME
    )
    crawler = ActivityCrawler(converter)
    crawler.run(pages=args.pages, start_page=args.start_page)


if __name__ == "__main__":
    main()
