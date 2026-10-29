#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
好单库「近2h销量」活动商品 —— 批量转链 + 社群素材保存
活动页：https://www.haodanku.com/Activity/index?id=24

功能流程：
1. 定时任务调度：每 1 小时自动运行一次，使用固定配置抓取。
2. 调用活动商品列表接口，按「近2h销量」排序拉取商品。
3. 对每个商品调用好单库 ratesurl 转链接口，生成「二合一链接」和「淘口令」。
4. 访问商品社群接口，解析右侧第 1 个推广群的社群实拍图与文案（保留原始图文顺序）；缺失则回退到主图文案。
5. 下载商品素材图片到 material/ 文件夹。
6. 每成功拉取并处理完一个商品，立即同步保存至汇总 JSON 文件（data/all_items.json）。
7. 支持断点续跑：若商品已存在于汇总 JSON 中且数据完整，则自动跳过。
=====================================================================
"""

import html as html_lib
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, Tag

from common.common_utils import setup_logger

logger = setup_logger(app_name="haodanku")

# ==================== 2. 专属配置信息 ====================
HAODANKU_APIKEY = "9D3E0E43E5FD"  # 好单库专属 APIKEY
MY_PID = "mm_328750149_3161250458_115832850163"  # 淘宝联盟 PID
TB_NAME = "不同名字123"  # 好单库推广中心已授权的淘宝账号昵称

# ==================== 3. 运行固定参数与定时配置 ====================
RUN_INTERVAL = 3600  # 定时执行间隔（秒）：1小时（3600秒）
FIXED_PAGES = 1  # 每次固定拉取的页数（默认 1 页，每页 20 条）
FIXED_START_PAGE = 1  # 固定起始页码
FORCE_REFRESH = False  # 是否强制覆盖已有记录（默认 False，支持断点跳过）

# ==================== 4. 活动与接口配置 ====================
ACTIVITY_ID = 24  # 活动 id（对应近2h销量所在的活动页）
PAGE_SIZE = 20  # 每页商品数（页面固定 20）
SORT_TYPE = 1  # 排序：1=近2h销量（页面默认选中项）
MAX_RETRY = 3  # 失败重试次数
REQUEST_GAP = 0.4  # 每个商品处理间隔（秒），礼貌限速避免触发风控

# 活动商品列表接口（网站内部接口，浏览器抓包所得；无需 apikey）
LIST_API = "https://www.haodanku.com/HolidayActivity/weal_list"
# 商品详情页地址（用于提取右侧「推广内容」第一个社群的实拍图和文案）
ITEM_DETAIL_URL = "https://www.haodanku.com/Item/details"
# 商品信息接口（用于换取社群素材接口所需的 itemid）
ITEM_INFO_API = "https://www.haodanku.com/detail/item_info"
# 社群素材接口（获取右侧各推广群素材 JSON）
ITEM_MATERIAL_API = "https://www.haodanku.com/ItemExtension/item_material"
# 好单库官方「淘联商品转链」接口
RATES_API = "http://v3.api.haodanku.com/ratesurl"

# 输出目录（基于脚本所在目录，保证在任意工作目录下运行都一致）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MATERIAL_DIR = os.path.join(BASE_DIR, "material")  # 素材图片目录
DATA_DIR = os.path.join(BASE_DIR, "data")  # JSON 数据目录
ALL_ITEMS_JSON = os.path.join(DATA_DIR, "all_items.json")  # 唯一维护的汇总 JSON 文件

# Content-Type 到文件扩展名的映射
CTYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}

# 需要排除的非素材图片关键词（群头像、微信/QQ小图标、表情图标、占位图等）
IGNORE_IMG_KEYWORDS = (
    "headimg",
    "avatar",
    "logo",
    "icon",
    "qrcode",
    "loading",
    "lazy",
    "default",
    "empty",
    "/public/assets/",
    "/static/",
    "bqfh",  # 好单库富文本表情小图标如 1_bqfh7.png
    "emoji",
)


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
            except Exception as e:
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
        if activityid:
            payload["activityid"] = activityid

        logger.info(
            "[发起转链] itemid=%s, activityid=%s", itemid, activityid or "无"
        )
        try:
            res_json = self._post_with_retry(RATES_API, payload)
        except Exception as e:
            return {"status": False, "msg": f"转链请求异常: {e}"}

        if res_json.get("code") in (1, 200, "1", "200"):
            data = res_json.get("data", {})
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


class CommunityMaterialExtractor:
    """
    从好单库社群素材接口及详情页提取右侧「推广内容」
    第一个推广群（最新群）的实拍图片列表与推广文案，严格保留原始顺序。
    """

    @staticmethod
    def _clean_img_url(url: str) -> str:
        """规范化图片 URL，去除好单库缩略图后缀以获取高清原图。"""
        if not url:
            return ""
        url = html_lib.unescape(str(url).strip())
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith(("http://", "https://")):
            return ""
        url = re.sub(r"-(?:100|200|240|300|310|400|600|w\d+)$", "", url)
        url = re.sub(r"_\d+x\d+(?:q\d+)?\.jpg.*$", "", url, flags=re.IGNORECASE)
        return url

    @classmethod
    def _is_valid_material_img(cls, img_tag: Tag, raw_url: str) -> bool:
        """判断 <img> 是否为真实的社群推广商品实拍图（排除群头像、小图标等）。"""
        if not raw_url:
            return False
        lower_url = raw_url.lower()
        for kw in IGNORE_IMG_KEYWORDS:
            if kw in lower_url:
                return False

        classes_chain = []
        node: Optional[Tag] = img_tag
        for _ in range(4):
            if node is None or not isinstance(node, Tag):
                break
            cls_list = node.get("class") or []
            if isinstance(cls_list, list):
                classes_chain.extend([c.lower() for c in cls_list])
            elif isinstance(cls_list, str):
                classes_chain.append(cls_list.lower())
            node = node.parent

        avatar_markers = ("avatar", "head", "group-list", "grouplist", "left-nav", "user", "icon", "logo")
        if any(any(m in c for m in avatar_markers) for c in classes_chain):
            return False

        return True

    @staticmethod
    def _clean_copy_text(raw_text: str, replacement_taoword: str = "", replacement_url: str = "") -> str:
        """
        清理从接口或页面提取的社群文案：
        - 还原 HTML 实体，保留换行与 emoji；
        - 若文案中包含原群残留的淘口令占位或下单链接，可按需替换为自己的淘口令/链接。
        """
        if not raw_text or not isinstance(raw_text, str):
            return ""
        soup = BeautifulSoup(raw_text, "html.parser")
        for tag in soup.find_all(["script", "style"]):
            tag.decompose()
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for tag in soup.find_all(["p", "div", "li", "blockquote", "pre"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        text = soup.get_text("", strip=False)
        text = html_lib.unescape(text)

        lines = [line.strip() for line in text.splitlines()]
        cleaned_lines = []
        for line in lines:
            if line in ("一键复制", "复制文案", "复制"):
                continue
            cleaned_lines.append(line)

        result = "\n".join(cleaned_lines).strip()
        result = re.sub(r"\n{3,}", "\n\n", result)

        if replacement_taoword:
            if "{淘口令}" in result:
                result = result.replace("{淘口令}", replacement_taoword)
            elif re.search(r"下单[:：]\s*$", result):
                result = f"{result}{replacement_taoword}"

        return result

    @classmethod
    def extract_from_api_data(
            cls,
            groups: list,
            taoword: str = "",
            two_in_one_url: str = "",
            max_images: int = 5,
    ) -> Tuple[List[str], str, str, List[dict]]:
        """
        从 /ItemExtension/item_material 返回的社群列表中提取第一个推广群的：
        (图片URL列表, 社群推广文案, 推广群名称, 有序素材列表)
        截断规则：最多保留 5 张社群图片，文案也只保留截止到第 5 张图片所在块的文案。
        """
        if not isinstance(groups, list) or not groups:
            return [], "", "", []

        group = groups[0]
        if not isinstance(group, dict):
            return [], "", "", []

        group_name = str(group.get("name") or "").strip()
        content = group.get("content") or []
        if not isinstance(content, list):
            return [], "", "", []

        images: List[str] = []
        texts: List[str] = []
        ordered_material: List[dict] = []
        seq = 1

        for block in content:
            if not isinstance(block, dict):
                continue

            # 若此前已集满 5 张图，后续整个素材块（包括多余的后续文案）彻底停止读取
            if len(images) >= max_images:
                break

            # 1. 块内图片（严格按原顺序，最多提取满 max_images 张）
            block_images = block.get("image") or []
            if isinstance(block_images, list):
                for img_url in block_images:
                    clean_url = cls._clean_img_url(str(img_url))
                    if clean_url:
                        images.append(clean_url)
                        ordered_material.append({
                            "sequence": seq,
                            "type": "image",
                            "url": clean_url,
                            "local_path": None,
                        })
                        seq += 1
                        if len(images) >= max_images:
                            break

            # 2. 块例文案 show_text（保留截止到第 5 张图所在块的文案）
            raw_text = block.get("show_text") or ""
            if raw_text:
                clean_text = cls._clean_copy_text(raw_text, taoword, two_in_one_url)
                if clean_text:
                    texts.append(clean_text)
                    ordered_material.append({
                        "sequence": seq,
                        "type": "text",
                        "content": clean_text,
                        "text": clean_text,
                    })
                    seq += 1

            # 3. 视频素材（若有）
            videos = block.get("video") or []
            if isinstance(videos, list):
                for v_url in videos:
                    v_url_str = str(v_url).strip()
                    if v_url_str:
                        ordered_material.append({
                            "sequence": seq,
                            "type": "video",
                            "url": v_url_str,
                        })
                        seq += 1

        copy_text = "\n\n".join(texts).strip()
        return images, copy_text, group_name, ordered_material
    @classmethod
    def extract_from_html(
            cls,
            html_text: str,
            taoword: str = "",
            two_in_one_url: str = "",
    ) -> Tuple[List[str], str, str]:
        """
        从商品详情页 HTML 中提取右侧「推广内容」第一个推广群的：
        (图片URL列表, 社群推广文案, 推广群名称)
        """
        soup = BeautifulSoup(html_text, "lxml")

        promo_container: Optional[Tag] = None
        for text_node in soup.find_all(string=re.compile(r"推广内容|精选各大社群素材")):
            parent = text_node.parent
            curr = parent
            for _ in range(6):
                if curr is None or not isinstance(curr, Tag):
                    break
                curr_text = curr.get_text(" ", strip=True)
                if "推广内容" in curr_text and (
                        "一键复制" in curr_text
                        or curr.find("img") is not None
                        or "好单推荐" not in curr_text[:200]
                ):
                    if "官方微信模板" not in curr_text:
                        promo_container = curr
                    else:
                        break
                curr = curr.parent
            if promo_container is not None:
                break

        if promo_container is None:
            for selector in (
                    ".spread-content",
                    ".promotion-content",
                    ".community-material",
                    ".group-material",
                    ".right-content",
                    ".copy-right",
            ):
                found = soup.select_one(selector)
                if found:
                    promo_container = found
                    break

        images: List[str] = []
        copy_text: str = ""
        group_name: str = ""

        if promo_container is not None:
            date_node = promo_container.find(string=re.compile(r"202\d\.\d{2}\.\d{2}"))
            if date_node and date_node.parent:
                group_item = date_node.parent.parent or date_node.parent
                raw_group_text = group_item.get_text("\n", strip=True).splitlines()
                if raw_group_text:
                    group_name = raw_group_text[0].strip()

            first_group_panel: Optional[Tag] = None
            candidate_panels = []
            for div in promo_container.find_all(["div", "ul", "li", "section"]):
                style = (div.get("style") or "").lower().replace(" ", "")
                cls_str = " ".join(div.get("class") or []).lower()
                if "display:none" in style or "hide" in cls_str or "hidden" in cls_str:
                    continue
                div_text = div.get_text(" ", strip=True)
                if "一键复制" in div_text and "推广内容" not in div_text:
                    candidate_panels.append(div)

            if candidate_panels:
                first_group_panel = candidate_panels[0]
            else:
                first_group_panel = promo_container

            for img in first_group_panel.find_all("img"):
                hidden_ancestor = False
                anc = img.parent
                while anc and anc != promo_container:
                    anc_style = (anc.get("style") or "").lower().replace(" ", "")
                    anc_cls = " ".join(anc.get("class") or []).lower()
                    if "display:none" in anc_style or re.search(r"\b(hide|hidden)\b", anc_cls):
                        hidden_ancestor = True
                        break
                    anc = anc.parent
                if hidden_ancestor:
                    continue

                raw_src = (
                        img.get("data-original")
                        or img.get("data-src")
                        or img.get("lay-src")
                        or img.get("src")
                        or ""
                )
                clean_src = cls._clean_img_url(raw_src)
                if cls._is_valid_material_img(img, clean_src) and clean_src not in images:
                    images.append(clean_src)

            copy_btn = None
            for btn in first_group_panel.find_all(["button", "a", "div", "span"]):
                if btn.get_text(strip=True) == "一键复制":
                    copy_btn = btn
                    break

            if copy_btn is not None:
                for attr in ("data-clipboard-text", "data-content", "data-text", "data-copy"):
                    if copy_btn.get(attr):
                        copy_text = cls._clean_copy_text(
                            copy_btn.get(attr), taoword, two_in_one_url
                        )
                        break

            if not copy_text:
                text_candidates = []
                for node in first_group_panel.find_all(["div", "p", "textarea", "pre"]):
                    node_str = node.get_text("\n", strip=True)
                    if not node_str or len(node_str) < 6:
                        continue
                    if len(re.findall(r"202\d\.\d{2}\.\d{2}", node_str)) >= 2:
                        continue
                    if "精选各大社群素材" in node_str or "官方微信模板" in node_str:
                        continue
                    if node.find(["ul", "table"]) is not None:
                        continue
                    cleaned = cls._clean_copy_text(node.decode_contents(), taoword, two_in_one_url)
                    if cleaned and cleaned not in ("一键复制", "推广内容"):
                        text_candidates.append(cleaned)

                if text_candidates:
                    copy_text = max(text_candidates, key=len)

        if not images and not copy_text:
            js_imgs, js_text = cls._extract_from_embedded_js(html_text, taoword, two_in_one_url)
            if js_imgs:
                images = js_imgs
            if js_text:
                copy_text = js_text

        return images, copy_text, group_name

    @classmethod
    def _extract_from_embedded_js(
            cls, html_text: str, taoword: str = "", two_in_one_url: str = ""
    ) -> Tuple[List[str], str]:
        """如果页面通过内嵌 script 变量渲染社群素材，尝试从 script 块中解析。"""
        images: List[str] = []
        copy_text: str = ""

        for script_match in re.finditer(r"<script[^>]*>(.*?)</script>", html_text, re.S | re.I):
            script_body = script_match.group(1)
            if "推广内容" not in script_body and "group" not in script_body.lower() and "material" not in script_body.lower():
                continue
            for json_match in re.finditer(r"(\[\s*\{.*?\}\s*\])", script_body, re.S):
                try:
                    arr = json.loads(json_match.group(1))
                except Exception:
                    continue
                if not isinstance(arr, list) or not arr or not isinstance(arr[0], dict):
                    continue
                first = arr[0]
                raw_txt = (
                        first.get("content")
                        or first.get("copy_text")
                        or first.get("text")
                        or first.get("fq_content")
                        or ""
                )
                raw_pics = (
                        first.get("images")
                        or first.get("img")
                        or first.get("itempic")
                        or first.get("pics")
                        or []
                )
                if isinstance(raw_pics, str):
                    raw_pics = [u.strip() for u in raw_pics.split(",") if u.strip()]
                if raw_txt or raw_pics:
                    copy_text = cls._clean_copy_text(str(raw_txt), taoword, two_in_one_url)
                    for u in raw_pics:
                        cu = cls._clean_img_url(str(u))
                        if cu and cu not in images:
                            images.append(cu)
                    if images or copy_text:
                        return images, copy_text

        return images, copy_text


class ActivityCrawler:
    """活动商品列表拉取、详情页社群素材抓取、素材下载、汇总 JSON 落盘主流程。"""

    def __init__(self, converter: HaoDanKuConverter, force: bool = False):
        self.converter = converter
        self.force = force
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": f"https://www.haodanku.com/Activity/index?id={ACTIVITY_ID}",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        os.makedirs(MATERIAL_DIR, exist_ok=True)
        os.makedirs(DATA_DIR, exist_ok=True)

        # 加载唯一的汇总商品数据（以字符串 "id" 为唯一标识映射）
        self.all_items_map: Dict[str, dict] = self._load_all_items()

    # ---------------- 汇总 JSON 读写管理 ----------------
    def _load_all_items(self) -> Dict[str, dict]:
        """加载汇总 JSON 文件（data/all_items.json）。"""
        items_map: Dict[str, dict] = {}
        if not os.path.exists(ALL_ITEMS_JSON):
            return items_map

        try:
            with open(ALL_ITEMS_JSON, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            if isinstance(raw_data, list):
                for entry in raw_data:
                    if isinstance(entry, dict) and entry.get("id") is not None:
                        items_map[str(entry["id"])] = entry
            elif isinstance(raw_data, dict):
                if isinstance(raw_data.get("items"), list):
                    for entry in raw_data["items"]:
                        if isinstance(entry, dict) and entry.get("id") is not None:
                            items_map[str(entry["id"])] = entry
                else:
                    for k, v in raw_data.items():
                        if isinstance(v, dict):
                            v["id"] = str(v.get("id") or k)
                            items_map[str(v["id"])] = v
            logger.info("[汇总数据] 已加载历史商品 %d 条 (%s)", len(items_map), ALL_ITEMS_JSON)
        except Exception as e:
            logger.warning("[汇总数据] 读取 %s 失败，将重新创建: %s", ALL_ITEMS_JSON, e)

        return items_map

    def _save_all_items(self) -> None:
        """将所有商品数据原子写入汇总 JSON 文件（data/all_items.json）。"""
        all_list = list(self.all_items_map.values())
        tmp_path = ALL_ITEMS_JSON + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(all_list, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, ALL_ITEMS_JSON)

    # ---------------- 列表拉取 ----------------
    def fetch_page(self, page_no: int) -> dict:
        """拉取指定页的商品数据，返回 data 对象。"""
        params = {
            "cat_id": 0,
            "type": SORT_TYPE,
            "page_no": page_no,
            "page_size": PAGE_SIZE,
            "activity_id": ACTIVITY_ID,
            "startprice": "",
            "endprice": "",
            "start_tkrates": "",
            "end_tkrates": "",
            "coupon": "",
            "start_endprice": "",
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        last_err = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                resp = self.session.get(
                    LIST_API, params=params, headers=headers, timeout=15
                )
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

    # ---------------- 详情页社群素材抓取 ----------------
    def fetch_community_material(
            self, hdk_id: str, taoword: str = "", two_in_one_url: str = "", fallback_itemid: str = ""
    ) -> Tuple[List[str], str, str, List[dict]]:
        """提取右侧「推广内容」第一个推广群（最新）的图片 URL 列表、文案、群名称与按原始顺序排列的素材项。"""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": f"{ITEM_DETAIL_URL}?id={hdk_id}",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }

        # 1. 优先调用 /detail/item_info 获取素材接口所必须的 itemid
        api_item_id = fallback_itemid
        for attempt in range(1, MAX_RETRY + 1):
            try:
                resp = self.session.get(ITEM_INFO_API, params={"id": hdk_id}, headers=headers, timeout=15)
                if resp.status_code == 200:
                    info_json = resp.json()
                    if str(info_json.get("status")) == "1" and isinstance(info_json.get("data"), dict):
                        fetched_id = info_json["data"].get("itemid")
                        if fetched_id:
                            api_item_id = str(fetched_id).strip()
                            break
            except Exception as e:
                logger.warning("[商品信息获取重试] id=%s 第 %d/%d 次失败: %s", hdk_id, attempt, MAX_RETRY, e)
                time.sleep(1 * attempt)

        # 2. 调用 /ItemExtension/item_material 获取官方社群素材接口数据
        if api_item_id:
            for attempt in range(1, MAX_RETRY + 1):
                try:
                    resp = self.session.get(ITEM_MATERIAL_API, params={"itemid": api_item_id}, headers=headers, timeout=15)
                    if resp.status_code == 200:
                        mat_json = resp.json()
                        if str(mat_json.get("code")) == "200":
                            groups = mat_json.get("data")
                            if isinstance(groups, list) and groups:
                                return CommunityMaterialExtractor.extract_from_api_data(
                                    groups, taoword=taoword, two_in_one_url=two_in_one_url
                                )
                except Exception as e:
                    logger.warning("[社群素材获取重试] itemid=%s 第 %d/%d 次失败: %s", api_item_id, attempt, MAX_RETRY, e)
                    time.sleep(1 * attempt)

        # 3. 若接口无数据，回退尝试页面 HTML 提取作为保底方案
        try:
            detail_url = f"{ITEM_DETAIL_URL}?id={hdk_id}"
            resp = self.session.get(detail_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding or "utf-8"
                imgs, copy_txt, group_name = CommunityMaterialExtractor.extract_from_html(
                    resp.text, taoword=taoword, two_in_one_url=two_in_one_url
                )
                if imgs or copy_txt:
                    ordered = []
                    seq_i = 1
                    for u in imgs:
                        ordered.append({"sequence": seq_i, "type": "image", "url": u, "local_path": None})
                        seq_i += 1
                    if copy_txt:
                        ordered.append({"sequence": seq_i, "type": "text", "content": copy_txt, "text": copy_txt})
                    return imgs, copy_txt, group_name, ordered
        except Exception as e:
            logger.warning("[详情页HTML解析重试失败] id=%s: %s", hdk_id, e)

        return [], "", "", []

    # ---------------- 素材下载 ----------------
    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        return url

    def _download_image(self, img_url: str, record_id: str, order: int) -> str:
        """下载单张图片，按真实 Content-Type 决定扩展名，返回本地绝对路径。"""
        img_url = self._normalize_url(img_url)
        resp = self.session.get(img_url, timeout=20)
        resp.raise_for_status()

        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = CTYPE_EXT.get(ctype)
        if not ext:
            url_path = img_url.split("?")[0].lower()
            ext = next(
                (e for e in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
                 if url_path.endswith(e)),
                ".jpg",
            )
            if ext == ".jpeg":
                ext = ".jpg"

        filename = f"{record_id}_{order:02d}{ext}"
        local_path = os.path.join(MATERIAL_DIR, filename)
        with open(local_path, "wb") as f:
            f.write(resp.content)
        return local_path

    # ---------------- 单个商品处理 ----------------

    def process_item(self, item: dict) -> str:
        """
        处理单个商品，返回状态：ok / skip / fail。
        社群素材与官方图/推荐语并列拉取，互不顶替，每处理完一个商品立即同步持久化。
        """
        raw_id = item.get("id")
        itemid = (item.get("itemid") or "").strip()
        if raw_id is None or not str(raw_id).strip():
            logger.warning("[跳过异常] 商品缺少好单库 id: itemid=%s", itemid)
            return "fail"
        if not itemid:
            logger.warning("[跳过异常] 商品缺少淘系 itemid: id=%s", raw_id)
            return "fail"

        record_id = str(raw_id).strip()
        general_index = item.get("general_index")

        # 断点续跑：只在汇总 JSON 中核查是否已存在且并列字段完整
        if not self.force:
            existing = self.all_items_map.get(record_id)
            if (
                    isinstance(existing, dict)
                    and existing.get("two_in_one_url")
                    and "ordered_material" in existing
                    and "item_images" in existing
            ):
                logger.info(
                    "[已处理跳过] id=%s (itemid=%s) %s",
                    record_id, itemid, (existing.get("item_title") or "")[:20],
                )
                return "skip"

        # 1) 转链
        title = item.get("itemtitle") or item.get("itemshorttitle") or ""
        conv = self.converter.convert_item(
            itemid=itemid,
            activityid=item.get("activityid") or "",
            title=title,
        )
        if not conv.get("status"):
            logger.error("[商品处理失败] id=%s (itemid=%s): %s", record_id, itemid, conv.get("msg"))
            return "fail"

        # 2) 并列拉取 A：社群推广素材（最多 5 张图及截止文案）
        comm_imgs, comm_copy, comm_group, ordered_material = self.fetch_community_material(
            hdk_id=record_id,
            taoword=conv["taoword"] or "",
            two_in_one_url=conv["two_in_one_url"] or "",
            fallback_itemid=itemid,
        )

        seen_comm, uniq_comm_urls = set(), []
        for u in comm_imgs:
            u = self._normalize_url(u)
            if u and u not in seen_comm:
                seen_comm.add(u)
                uniq_comm_urls.append(u)
                if len(uniq_comm_urls) >= 5:
                    break

        url_to_comm_path = {}
        comm_local_paths = []
        for i, u in enumerate(uniq_comm_urls, start=1):
            try:
                # 文件名标识为 _comm_，与官方主图区分
                l_path = self._download_image(u, f"{record_id}_comm", i)
                comm_local_paths.append(l_path)
                url_to_comm_path[u] = l_path
            except Exception as e:
                logger.warning("[社群图片下载失败] id=%s -> %s: %s", record_id, u, e)

        # 回填有序素材列表中的本地路径
        for entry in ordered_material:
            if entry.get("type") == "image":
                entry["local_path"] = url_to_comm_path.get(entry.get("url"))

        # 3) 并列拉取 B：官方主图与推荐语（独立下载与保存，不被社群素材顶替）
        item_desc = (item.get("itemdesc") or "").strip()

        official_imgs = []
        if item.get("itempic"):
            official_imgs.append(item["itempic"])
        if item.get("images"):
            official_imgs.extend([u for u in item["images"].split(",") if u.strip()])

        seen_item, uniq_item_urls = set(), []
        for u in official_imgs:
            u = self._normalize_url(u)
            if u and u not in seen_item:
                seen_item.add(u)
                uniq_item_urls.append(u)
                if len(uniq_item_urls) >= 5:
                    break

        item_local_paths = []
        for i, u in enumerate(uniq_item_urls, start=1):
            try:
                # 文件名标识为 _item_，代表商品主图/轮播图
                l_path = self._download_image(u, f"{record_id}_item", i)
                item_local_paths.append(l_path)
            except Exception as e:
                logger.warning("[官方主图下载失败] id=%s -> %s: %s", record_id, u, e)

        # 4) 组装记录（两组素材并列存放，互不覆盖）
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "id": record_id,  # 商品唯一标识（好单库 ID）
            "item_id": itemid,  # 淘系加密商品 ID
            "general_index": general_index,  # 好单指数
            "item_title": item.get("itemtitle"),
            "two_in_one_url": conv["two_in_one_url"],  # 转换后的二合一链接
            "taoword": conv["taoword"],  # 淘口令

            # ===== 1. 社群素材区（独立字段） =====
            "community_group": comm_group,  # 推广群名称
            "community_content": comm_copy,  # 社群推广文案
            "community_images": comm_local_paths,  # 社群实拍图本地路径（最多5张）
            "ordered_material": ordered_material,  # 原始社群素材图文顺序列表（图文截断至前5张）

            # ===== 2. 官方素材区（并列独立字段） =====
            "item_desc": item_desc,  # 官方推荐语/文案
            "item_images": item_local_paths,  # 官方主图及轮播图本地路径（最多5张）

            # 兼容旧调用字段
            "real_content": comm_copy,  # 兼容保留
            "material_images": comm_local_paths,  # 兼容保留

            "detail_url": f"{ITEM_DETAIL_URL}?id={record_id}",
            "coupon_money": conv.get("coupon_money") or item.get("couponmoney"),
            "item_price": item.get("itemprice"),  # 原价
            "item_end_price": item.get("itemendprice"),  # 券后价
            "sale_2h": item.get("itemsale2"),  # 近2小时销量
            "tkrates": item.get("tkrates"),  # 佣金比例
            "tkmoney": item.get("tkmoney"),  # 佣金金额
            "shop_name": item.get("shopname"),
            "added_at": now_str,  # 本次加入时间
            "created_at": now_str,
        }

        # 每抓取处理完成一个商品，立刻持久化到汇总 JSON（data/all_items.json）
        self.all_items_map[record_id] = record
        self._save_all_items()

        logger.info(
            "[已保存至汇总] id=%s | 社群图: %d 张 | 官方主图: %d 张 | %s",
            record_id, len(comm_local_paths), len(item_local_paths), (record["item_title"] or "")[:24],
        )
        return "ok"

    # ---------------- 主流程 ----------------
    def run(self, pages: int, start_page: int = 1) -> dict:
        stats = {"ok": 0, "skip": 0, "fail": 0}
        end_page = start_page + pages - 1
        logger.info(
            "开始执行本轮抓取：活动 id=%s，排序=近2h销量，拉取第 %d~%d 页",
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
                    logger.error("[商品异常] id=%s (itemid=%s): %s", item.get("id"), item.get("itemid"), e)
                    stats["fail"] += 1
                time.sleep(REQUEST_GAP)

            logger.info(
                "本轮抓取完毕：成功 %d，跳过 %d，失败 %d | 汇总文件现有商品 %d 条 (%s)",
                stats["ok"], stats["skip"], stats["fail"],
                len(self.all_items_map), ALL_ITEMS_JSON,
            )
            return stats


# ==================== 5. 定时常驻运行入口 ====================
def main():
    logger.info("=== 好单库定时爬取程序已启动，设定为每 1 小时自动运行一次 ===")

    while True:
        try:
            converter = HaoDanKuConverter(
                apikey=HAODANKU_APIKEY, pid=MY_PID, tb_name=TB_NAME
            )
            crawler = ActivityCrawler(converter, force=FORCE_REFRESH)
            crawler.run(pages=FIXED_PAGES, start_page=FIXED_START_PAGE)
        except Exception as e:
            logger.error("[全局巡检异常] %s", e, exc_info=True)

        logger.info("本轮执行完成，将在 %d 秒（1小时）后自动开启下一轮抓取...", RUN_INTERVAL)
        time.sleep(RUN_INTERVAL)


if __name__ == "__main__":
    main()