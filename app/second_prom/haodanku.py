#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
[功能摘要]
好单库「近2h销量」活动商品定时抓取、淘宝联盟高佣转链、社群/官方双源素材下载与增量落盘服务。

[输入数据]
1. 静态配置：好单库 APIKEY、淘宝联盟 PID、授权淘宝昵称、活动 ID (24) 及定时周期参数。
2. 外部接口数据：
   - 活动列表接口 (LIST_API)：返回分页商品基础字典列表（含 id, itemid, itemtitle, itempic 等）。
   - 官方转链接口 (RATES_API)：输入 itemid/PID，返回二合一推广链接与淘口令。
   - 社群素材接口 (ITEM_INFO_API / ITEM_MATERIAL_API) 及详情页 HTML：返回推广群图文列表。
3. 本地状态文件：`data/all_items.json` 历史商品汇总列表（用于断点续跑校验）。

[数据流转/交互]
定时调度器 (main) -> 拉取活动页商品列表 (fetch_page) -> 断点续跑过滤 (process_item)
-> 淘联高佣转链 (convert_item) -> 拉取社群素材，接口无数据则降级解析详情页 HTML (fetch_community_material)
-> 清洗文案并注入淘口令 (_clean_copy_text) -> 分别对前5张社群实拍图与前5张官方主图去重并下载 (_download_batch_images)
-> 组装社群与官方并列素材结构 -> 临时文件原子替换写入汇总 JSON (_save_all_items)。

[输出数据]
1. 汇总数据文件：`data/all_items.json`，包含所有已处理商品的转链信息、社群素材、官方素材及价格销量元数据。
2. 本地图片文件：`material/{id}_comm_{01..05}.{ext}`（社群实拍图）与 `material/{id}_item_{01..05}.{ext}`（官方主图）。
=====================================================================
"""

import html as html_lib
import json
import os
import re
import time

import requests
from bs4 import BeautifulSoup, Tag

from common.common_utils import setup_logger, read_file_to_str, string_to_object
from common.model_api import generate_content

logger = setup_logger(app_name="haodanku")

# ==================== 1. 认证与推广配置 ====================
HAODANKU_APIKEY = "9D3E0E43E5FD"
MY_PID = "mm_328750149_3161250458_115832850163"
TB_NAME = "不同名字123"
PROMPT_FILE_PATH = "W:\project\python_project\easy_shop\prompt\种草话术生成.txt"
# ==================== 2. 调度与抓取固定参数 ====================
RUN_INTERVAL = 3600
FIXED_PAGES = 10
FIXED_START_PAGE = 1
FORCE_REFRESH = False

ACTIVITY_ID = 24
PAGE_SIZE = 20
SORT_TYPE = 1
MAX_RETRY = 3
REQUEST_GAP = 0.4
MAX_COMM_IMAGES = 9  # 社群实拍图最大保留与下载张数（原为 5）
MAX_ITEM_IMAGES = 9  # 官方主图最大保留与下载张数（原为 5）
# ==================== 3. 接口地址与本地路径配置 ====================
LIST_API = "https://www.haodanku.com/HolidayActivity/weal_list"
ITEM_DETAIL_URL = "https://www.haodanku.com/Item/details"
ITEM_INFO_API = "https://www.haodanku.com/detail/item_info"
ITEM_MATERIAL_API = "https://www.haodanku.com/ItemExtension/item_material"
RATES_API = "http://v3.api.haodanku.com/ratesurl"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MATERIAL_DIR = os.path.join(BASE_DIR, "material")
DATA_DIR = os.path.join(BASE_DIR, "data")
ALL_ITEMS_JSON = os.path.join(DATA_DIR, "all_items.json")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

CTYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}

IGNORE_IMG_KEYWORDS = (
    "headimg", "avatar", "logo", "icon", "qrcode", "loading",
    "lazy", "default", "empty", "/public/assets/", "/static/",
    "bqfh", "emoji",
)

AVATAR_CLASS_MARKERS = (
    "avatar", "head", "group-list", "grouplist", "left-nav", "user", "icon", "logo"
)


class HaoDanKuConverter:
    """好单库高佣转链器：负责将淘系商品 ID 转换为推广二合一链接与淘口令。"""

    def __init__(self, apikey, pid, tb_name):
        self.apikey = apikey
        self.pid = pid
        self.tb_name = tb_name
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def close(self):
        """释放底层 HTTP 连接池资源。"""
        self.session.close()

    def _post_with_retry(self, url, data):
        """
        发送带退避重试的 POST 请求。
        - 入参 data: dict，请求表单载荷。
        - 出参: dict，接口返回的 JSON 字典。
        """
        last_err = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                resp = self.session.post(url, data=data, timeout=15)
                return resp.json()
            except Exception as e:
                last_err = e
                logger.warning(
                    "[转链接口/重试] 请求好单库转链接口网络异常 | 进度: [%d/%d] | 目標商品: [%s] | 原因: %s",
                    attempt, MAX_RETRY, data.get("itemid"), e,
                )
                time.sleep(attempt)
        raise last_err

    def convert_item(self, itemid, activityid, title):
        """
        执行单商品高佣转链。
        - 入参: itemid (str 淘系商品ID), activityid (str 优惠券ID), title (str 商品标题，不足8字自动补齐)。
        - 出参: dict，结构如下：
          成功: {"status": True, "two_in_one_url": str, "taoword": str, "coupon_money": str|int}
          失败: {"status": False, "msg": str}
        """
        if not title or len(title) < 8:
            title = f"{title or ''}【超值爆款正品特惠推荐】"

        payload = {
            "apikey": self.apikey,
            "tb_name": self.tb_name,
            "pid": self.pid,
            "itemid": itemid,
            "get_taoword": 1,
            "title": title,
        }
        if activityid:
            payload["activityid"] = activityid

        try:
            res_json = self._post_with_retry(RATES_API, payload)
        except Exception as e:
            return {"status": False, "msg": f"转链网络请求彻底失败({e})"}

        if res_json.get("code") not in (1, 200, "1", "200"):
            err_msg = res_json.get("msg") or "接口未返回明确错误原因"
            return {"status": False, "msg": err_msg}

        data = res_json.get("data") or {}
        return {
            "status": True,
            "two_in_one_url": data.get("coupon_click_url") or data.get("item_url") or data.get("tbk_url"),
            "taoword": data.get("taoword") or data.get("new_model"),
            "coupon_money": data.get("couponmoney", 0),
        }


class CommunityMaterialExtractor:
    """社群素材解析器：支持从好单库素材接口 JSON 或商品详情页 HTML 中提取推广图文。"""

    @staticmethod
    def normalize_url(url):
        """补全协议头并去除首尾空白。"""
        if not url:
            return ""
        url = html_lib.unescape(str(url).strip())
        return f"https:{url}" if url.startswith("//") else url

    @classmethod
    def _clean_img_url(cls, url):
        """规范化图片 URL，剥离好单库缩略图后缀以还原高清大图地址。"""
        url = cls.normalize_url(url)
        if not url.startswith(("http://", "https://")):
            return ""
        url = re.sub(r"-(?:100|200|240|300|310|400|600|w\d+)$", "", url)
        return re.sub(r"_\d+x\d+(?:q\d+)?\.jpg.*$", "", url, flags=re.IGNORECASE)

    @classmethod
    def _is_valid_material_img(cls, img_tag, raw_url):
        """过滤群头像、表情包及页面静态装饰图标，仅保留真实商品实拍图。"""
        if not raw_url:
            return False
        lower_url = raw_url.lower()
        if any(kw in lower_url for kw in IGNORE_IMG_KEYWORDS):
            return False

        classes_chain = []
        node = img_tag
        for _ in range(4):
            if not isinstance(node, Tag):
                break
            cls_val = node.get("class") or []
            if isinstance(cls_val, list):
                classes_chain.extend(c.lower() for c in cls_val)
            elif isinstance(cls_val, str):
                classes_chain.append(cls_val.lower())
            node = node.parent

        return not any(any(m in c for m in AVATAR_CLASS_MARKERS) for c in classes_chain)

    @staticmethod
    def _clean_copy_text(raw_text, replacement_taoword="", replacement_url=""):
        """
        清洗 HTML 格式的社群推广文案，剥离按钮噪音文字并注入当前用户的淘口令。
        """
        # : 业务边界预警 —— 入参传入了 replacement_url (二合一链接)，但原逻辑仅对 replacement_taoword (淘口令) 做了替换，
        # 未处理文案中残留原始下单链接的替换逻辑。为保持原有业务行为，此处暂不改动替换规则，请确认是否需要补充链接替换。
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

        text = html_lib.unescape(soup.get_text("", strip=False))
        ignore_words = {"一键复制", "复制文案", "复制"}
        cleaned_lines = [
            line.strip() for line in text.splitlines()
            if line.strip() not in ignore_words
        ]

        result = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines).strip())
        if replacement_taoword:
            if "{淘口令}" in result:
                result = result.replace("{淘口令}", replacement_taoword)
            elif re.search(r"下单[:：]\s*$", result):
                result = f"{result}{replacement_taoword}"

        return result

    @classmethod
    def extract_from_api_data(cls, groups, taoword="", two_in_one_url="", max_images=MAX_COMM_IMAGES):
        """
        从社群接口返回的群列表数据中提取第 1 个推广群的图文素材。
        - 入参 groups: list[dict]，每个元素形如 {"name": str, "content": [{"image": list, "show_text": str, "video": list}]}
        - 出参: tuple (images, copy_text, group_name, ordered_material)
          - images: list[str]，高清图片 URL 列表（最多 max_images 张）
          - copy_text: str，合并后的推广文案
          - group_name: str，推广群名称
          - ordered_material: list[dict]，按序号排列的素材项，元素包含的核心 Key:
            图片项: {"sequence": int, "type": "image", "url": str, "local_path": str|None}
            文本项: {"sequence": int, "type": "text", "content": str, "text": str}
            视频项: {"sequence": int, "type": "video", "url": str}
        """
        if not isinstance(groups, list) or not groups or not isinstance(groups[0], dict):
            return [], "", "", []

        group = groups[0]
        group_name = str(group.get("name") or "").strip()
        content = group.get("content")
        if not isinstance(content, list):
            return [], "", "", []

        images, texts, ordered_material = [], [], []
        seq = 1

        # : 业务边界预警 —— 块内固定按「先图片 -> 后文案 -> 再视频」顺序提取，且当块内图片恰好凑满 max_images (5张) 时，
        # 仍会继续提取当前块的文案与视频，直到下一个 block 才触发 break。保留原截断行为。
        for block in content:
            if not isinstance(block, dict):
                continue
            if len(images) >= max_images:
                break

            block_images = block.get("image")
            if isinstance(block_images, list):
                for img_url in block_images:
                    clean_url = cls._clean_img_url(str(img_url))
                    if not clean_url:
                        continue
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

            raw_text = block.get("show_text") or ""
            clean_text = cls._clean_copy_text(raw_text, taoword, two_in_one_url) if raw_text else ""
            if clean_text:
                texts.append(clean_text)
                ordered_material.append({
                    "sequence": seq,
                    "type": "text",
                    "content": clean_text,
                    "text": clean_text,
                })
                seq += 1

            videos = block.get("video")
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

        return images, "\n\n".join(texts).strip(), group_name, ordered_material

    @classmethod
    def _find_promo_container(cls, soup):
        """在详情页 DOM 树中定位右侧「推广内容」容器节点。"""
        for text_node in soup.find_all(string=re.compile(r"推广内容|精选各大社群素材")):
            curr = text_node.parent
            for _ in range(6):
                if not isinstance(curr, Tag):
                    break
                curr_text = curr.get_text(" ", strip=True)
                if "推广内容" in curr_text and (
                    "一键复制" in curr_text
                    or curr.find("img") is not None
                    or "好单推荐" not in curr_text[:200]
                ):
                    if "官方微信模板" in curr_text:
                        break
                    return curr
                curr = curr.parent

        selectors = (
            ".spread-content", ".promotion-content", ".community-material",
            ".group-material", ".right-content", ".copy-right",
        )
        for selector in selectors:
            found = soup.select_one(selector)
            if found:
                return found
        return None

    @staticmethod
    def _is_hidden_node(node):
        """判断 DOM 节点是否处于隐藏状态。"""
        style = (node.get("style") or "").lower().replace(" ", "")
        cls_str = " ".join(node.get("class") or []).lower()
        return "display:none" in style or bool(re.search(r"\b(hide|hidden)\b", cls_str))

    @classmethod
    def extract_from_html(cls, html_text, taoword="", two_in_one_url=""):
        """
        降级方案：从商品详情页 HTML 中解析第 1 个推广群的图片列表、文案与群名称。
        - 出参: tuple (images: list[str], copy_text: str, group_name: str)
        """
        soup = BeautifulSoup(html_text, "lxml")
        promo_container = cls._find_promo_container(soup)

        images, copy_text, group_name = [], "", ""
        if promo_container is not None:
            date_node = promo_container.find(string=re.compile(r"202\d\.\d{2}\.\d{2}"))
            if date_node and date_node.parent:
                group_item = date_node.parent.parent or date_node.parent
                raw_lines = group_item.get_text("\n", strip=True).splitlines()
                if raw_lines:
                    group_name = raw_lines[0].strip()

            candidate_panels = [
                div for div in promo_container.find_all(["div", "ul", "li", "section"])
                if not cls._is_hidden_node(div)
                and "一键复制" in div.get_text(" ", strip=True)
                and "推广内容" not in div.get_text(" ", strip=True)
            ]
            first_group_panel = candidate_panels[0] if candidate_panels else promo_container

            # 提取可见的实拍图
            for img in first_group_panel.find_all("img"):
                anc, is_hidden = img.parent, False
                while anc and anc != promo_container:
                    if cls._is_hidden_node(anc):
                        is_hidden = True
                        break
                    anc = anc.parent
                if is_hidden:
                    continue

                raw_src = (
                    img.get("data-original") or img.get("data-src")
                    or img.get("lay-src") or img.get("src") or ""
                )
                clean_src = cls._clean_img_url(raw_src)
                if cls._is_valid_material_img(img, clean_src) and clean_src not in images:
                    images.append(clean_src)

            # 优先从复制按钮属性提取文案，缺失则从正文块候选中取最长文案
            copy_btn = next(
                (b for b in first_group_panel.find_all(["button", "a", "div", "span"])
                 if b.get_text(strip=True) == "一键复制"),
                None,
            )
            if copy_btn is not None:
                for attr in ("data-clipboard-text", "data-content", "data-text", "data-copy"):
                    if copy_btn.get(attr):
                        copy_text = cls._clean_copy_text(copy_btn.get(attr), taoword, two_in_one_url)
                        break

            if not copy_text:
                text_candidates = []
                for node in first_group_panel.find_all(["div", "p", "textarea", "pre"]):
                    node_str = node.get_text("\n", strip=True)
                    if (
                        not node_str or len(node_str) < 6
                        or len(re.findall(r"202\d\.\d{2}\.\d{2}", node_str)) >= 2
                        or "精选各大社群素材" in node_str or "官方微信模板" in node_str
                        or node.find(["ul", "table"]) is not None
                    ):
                        continue
                    cleaned = cls._clean_copy_text(node.decode_contents(), taoword, two_in_one_url)
                    if cleaned and cleaned not in ("一键复制", "推广内容"):
                        text_candidates.append(cleaned)
                if text_candidates:
                    copy_text = max(text_candidates, key=len)

        if not images and not copy_text:
            js_imgs, js_text = cls._extract_from_embedded_js(html_text, taoword, two_in_one_url)
            images = js_imgs or images
            copy_text = js_text or copy_text

        return images, copy_text, group_name

    @classmethod
    def _extract_from_embedded_js(cls, html_text, taoword="", two_in_one_url=""):
        """兜底解析：当页面采用内嵌 JS 变量渲染社群素材时，从 <script> 块中正则提取 JSON。"""
        for script_match in re.finditer(r"<script[^>]*>(.*?)</script>", html_text, re.S | re.I):
            script_body = script_match.group(1)
            lower_body = script_body.lower()
            if "推广内容" not in script_body and "group" not in lower_body and "material" not in lower_body:
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
                    first.get("content") or first.get("copy_text")
                    or first.get("text") or first.get("fq_content") or ""
                )
                raw_pics = (
                    first.get("images") or first.get("img")
                    or first.get("itempic") or first.get("pics") or []
                )
                if isinstance(raw_pics, str):
                    raw_pics = [u.strip() for u in raw_pics.split(",") if u.strip()]
                if not raw_txt and not raw_pics:
                    continue

                copy_text = cls._clean_copy_text(str(raw_txt), taoword, two_in_one_url)
                images = []
                for u in raw_pics:
                    cu = cls._clean_img_url(str(u))
                    if cu and cu not in images:
                        images.append(cu)
                if images or copy_text:
                    return images, copy_text

        return [], ""


class ActivityCrawler:
    """活动商品抓取主控器：统筹列表分页拉取、转链、双源素材下载与汇总 JSON 持久化。"""

    def __init__(self, converter, force=False):
        self.converter = converter
        self.force = force
        self.session = requests.Session()
        self.session.headers.update({
            **DEFAULT_HEADERS,
            "Referer": f"https://www.haodanku.com/Activity/index?id={ACTIVITY_ID}",
        })
        os.makedirs(MATERIAL_DIR, exist_ok=True)
        os.makedirs(DATA_DIR, exist_ok=True)
        self.all_items_map = self._load_all_items()

    def close(self):
        """释放当前爬虫实例及转链器的 HTTP 会话资源。"""
        self.session.close()
        self.converter.close()

    def _load_all_items(self):
        """
        从本地汇总 JSON 加载历史商品记录。
        - 出参: dict[str, dict]，以好单库商品字符串 ID 为 Key，商品完整记录字典为 Value。
        """
        if not os.path.exists(ALL_ITEMS_JSON):
            return {}

        items_map = {}
        try:
            with open(ALL_ITEMS_JSON, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            records = []
            if isinstance(raw_data, list):
                records = raw_data
            elif isinstance(raw_data, dict):
                if isinstance(raw_data.get("items"), list):
                    records = raw_data["items"]
                else:
                    for k, v in raw_data.items():
                        if isinstance(v, dict):
                            v["id"] = str(v.get("id") or k)
                            records.append(v)

            for entry in records:
                if isinstance(entry, dict) and entry.get("id") is not None:
                    items_map[str(entry["id"])] = entry

            logger.info(
                "[汇总数据/初始化] 成功加载本地历史商品库 | 历史记录数: [%d] | 路径: <%s>",
                len(items_map), ALL_ITEMS_JSON,
            )
        except Exception as e:
            logger.warning(
                "[汇总数据/初始化] 读取本地汇总文件异常，将初始化为空库重新创建 | 路径: <%s> | 可能原因: 文件损坏或格式非法(%s)",
                ALL_ITEMS_JSON, e,
            )
        return items_map

    def _save_all_items(self):
        """采用临时文件 + os.replace 原子写入汇总 JSON，防止进程中断导致数据文件损坏。"""
        tmp_path = f"{ALL_ITEMS_JSON}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(list(self.all_items_map.values()), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, ALL_ITEMS_JSON)
        except Exception as e:
            logger.error(
                "❌ [汇总数据/落盘失败] 写入商品汇总 JSON 文件时发生严重故障 | 目标路径: <%s> | 故障原因: 磁盘空间不足或文件权限受限(%s)",
                ALL_ITEMS_JSON, e,
            )
            raise

    def fetch_page(self, page_no):
        """
        拉取指定页码的活动商品列表数据。
        - 入参 page_no: int，页码。
        - 出参: dict，包含 {"items": list[dict], "total": int, "page_total": int}。
        """
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
                resp = self.session.get(LIST_API, params=params, headers=headers, timeout=15)
                res_json = resp.json()
                if res_json.get("code") == 200:
                    return res_json["data"]
                last_err = res_json.get("msg") or f"接口返回异常状态码({res_json.get('code')})"
            except Exception as e:
                last_err = e

            logger.warning(
                "[列表拉取/重试] 获取活动商品分页列表未成功 | 页码: [第%d页] | 进度: [%d/%d] | 原因: %s",
                page_no, attempt, MAX_RETRY, last_err,
            )
            time.sleep(attempt)

        raise RuntimeError(f"连续 {MAX_RETRY} 次拉取第 {page_no} 页列表均失败，最后错误: {last_err}")

    def fetch_community_material(self, hdk_id, taoword="", two_in_one_url="", fallback_itemid=""):
        """
        获取商品右侧「推广内容」首个推广群的图文素材（优先走 API，无数据则降级解析 HTML）。
        - 出参: tuple (images: list[str], copy_text: str, group_name: str, ordered_material: list[dict])
        """
        headers = {
            **DEFAULT_HEADERS,
            "Referer": f"{ITEM_DETAIL_URL}?id={hdk_id}",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }

        # 1. 请求商品详情信息接口，换取社群素材接口必需的真实 itemid
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
                logger.warning(
                    "[素材前置/重试] 换取社群接口所需的真实 itemid 异常 | 商品ID: [%s] | 进度: [%d/%d] | 原因: %s",
                    hdk_id, attempt, MAX_RETRY, e,
                )
                time.sleep(attempt)

        # 2. 调用官方社群素材接口提取结构化数据
        if api_item_id:
            for attempt in range(1, MAX_RETRY + 1):
                try:
                    resp = self.session.get(
                        ITEM_MATERIAL_API, params={"itemid": api_item_id}, headers=headers, timeout=15
                    )
                    if resp.status_code == 200:
                        mat_json = resp.json()
                        if str(mat_json.get("code")) == "200" and isinstance(mat_json.get("data"), list) and mat_json["data"]:
                            return CommunityMaterialExtractor.extract_from_api_data(
                                mat_json["data"], taoword=taoword, two_in_one_url=two_in_one_url
                            )
                    break
                except Exception as e:
                    logger.warning(
                        "[社群素材接口/重试] 请求社群素材接口网络异常 | 商品ID: [%s] | 淘系ID: [%s] | 进度: [%d/%d] | 原因: %s",
                        hdk_id, api_item_id, attempt, MAX_RETRY, e,
                    )
                    time.sleep(attempt)

        # 3. 降级方案：直接拉取详情页 HTML 解析社群素材
        try:
            resp = self.session.get(f"{ITEM_DETAIL_URL}?id={hdk_id}", headers=headers, timeout=15)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding or "utf-8"
                imgs, copy_txt, group_name = CommunityMaterialExtractor.extract_from_html(
                    resp.text, taoword=taoword, two_in_one_url=two_in_one_url
                )
                if imgs or copy_txt:
                    # : 业务边界预警 —— HTML 降级分支构建 ordered_material 时未限制 imgs 数量，
                    # 若超过 5 张图，后续下载仅处理前 5 张，第 6 张起 local_path 将保持为 None。保留原组装逻辑。
                    ordered = [
                        {"sequence": idx, "type": "image", "url": u, "local_path": None}
                        for idx, u in enumerate(imgs, start=1)
                    ]
                    if copy_txt:
                        ordered.append({
                            "sequence": len(ordered) + 1,
                            "type": "text",
                            "content": copy_txt,
                            "text": copy_txt,
                        })
                    return imgs, copy_txt, group_name, ordered
        except Exception as e:
            logger.warning(
                "[详情页降级/失败] 尝试通过详情页 HTML 兜底提取社群素材失败，该商品将仅保存官方素材 | 商品ID: [%s] | 原因: %s",
                hdk_id, e,
            )

        return [], "", "", []

    def _download_image(self, img_url, prefix_id, order):
        """下载单张图片并根据响应头 Content-Type 自动匹配扩展名，返回本地文件绝对路径。"""
        img_url = CommunityMaterialExtractor.normalize_url(img_url)
        resp = self.session.get(img_url, timeout=20)
        resp.raise_for_status()

        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = CTYPE_EXT.get(ctype)
        if not ext:
            url_path = img_url.split("?")[0].lower()
            ext = next(
                (e for e in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp") if url_path.endswith(e)),
                ".jpg",
            )
            if ext == ".jpeg":
                ext = ".jpg"

        local_path = os.path.join(MATERIAL_DIR, f"{prefix_id}_{order:02d}{ext}")
        with open(local_path, "wb") as f:
            f.write(resp.content)
        return local_path

    def _download_batch_images(self, raw_urls, prefix_id, label, max_count=MAX_COMM_IMAGES):
        """
        对图片 URL 列表进行规范化、去重、截断前 max_count 张并批量下载到本地。
        - 出参: tuple (local_paths: list[str], url_to_path: dict[str, str])
        """
        seen, uniq_urls = set(), []
        for u in raw_urls:
            norm_u = CommunityMaterialExtractor.normalize_url(u)
            if norm_u and norm_u not in seen:
                seen.add(norm_u)
                uniq_urls.append(norm_u)
                if len(uniq_urls) >= max_count:
                    break

        local_paths, url_to_path = [], {}
        for idx, u in enumerate(uniq_urls, start=1):
            try:
                l_path = self._download_image(u, prefix_id, idx)
                local_paths.append(l_path)
                url_to_path[u] = l_path
            except Exception as e:
                logger.warning(
                    "[图片下载/跳过] 下载%s单张图片失败 | 标识: [%s_%02d] | URL: <%s> | 原因: %s",
                    label, prefix_id, idx, u, e,
                )
        return local_paths, url_to_path

    @staticmethod
    def _extract_material_group(ordered_material):
        """
        根据 ordered_material 的时序结构，精准提取首组完整的「图+文」素材。

        【解析规则】
        1. 文案在前（文字1 图片1 图片2 图片3 文字2 图片4）：
           采纳开头的首个文案，及随后连续的一组图片；遇到下一个文案时立即截断。
           -> 输出: 文案="文字1", 图片=["图片1", "图片2", "图片3"]
        2. 图片在前（图片1 图片2 文字1 文字2 图片3）：
           采纳开头连续的一组图片，及随后紧跟的首个文案；采纳后立即截断结束。
           -> 输出: 文案="文字1", 图片=["图片1", "图片2"]
        3. 容错保护：
           仅有图片无文案时保留图片；仅有文案无图片时保留文案。

        :param ordered_material: list[dict]，已完成本地下载并注入 local_path 的时序素材列表
        :return: tuple (selected_text: str, selected_images: list[str]) 本地路径列表
        """
        if not ordered_material or not isinstance(ordered_material, list):
            return "", []

        # 定位第一个有效项（类型为 text 或 image）
        start_idx = -1
        first_type = None
        for idx, item in enumerate(ordered_material):
            if isinstance(item, dict) and item.get("type") in ("text", "image"):
                start_idx = idx
                first_type = item["type"]
                break

        if start_idx == -1:
            return "", []

        selected_text = ""
        selected_images = []

        if first_type == "text":
            # ================= 模式 1：文案在前，图片在后 =================
            # 1. 采纳首个文案
            first_text_node = ordered_material[start_idx]
            selected_text = (
                    first_text_node.get("content")
                    or first_text_node.get("text")
                    or ""
            ).strip()

            # 2. 收集紧随其后的连续图片，遇到下一个文本即截断结束
            collecting_images = False
            for item in ordered_material[start_idx + 1:]:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "image":
                    collecting_images = True
                    img_path = item.get("local_path")
                    if img_path:
                        selected_images.append(img_path)
                elif itype == "text":
                    # 遇到第二段文案，本组图文已完整闭合，立即终止
                    break
                else:
                    # 遇到 video 等非图文类型，若已收集完图片则视为当前组结束
                    if collecting_images:
                        break

        elif first_type == "image":
            # ================= 模式 2：图片在前，文案在后 =================
            # 1. 搜集开头的连续图片
            next_idx = start_idx
            while next_idx < len(ordered_material):
                item = ordered_material[next_idx]
                if isinstance(item, dict) and item.get("type") == "image":
                    img_path = item.get("local_path")
                    if img_path:
                        selected_images.append(img_path)
                    next_idx += 1
                else:
                    break

            # 2. 搜集紧随其后的首个文案，采纳后立即截断
            while next_idx < len(ordered_material):
                item = ordered_material[next_idx]
                if isinstance(item, dict) and item.get("type") == "text":
                    selected_text = (
                            item.get("content")
                            or item.get("text")
                            or ""
                    ).strip()
                    # 采纳该文案后立即终止，后面的文字2、图片3等全部舍弃
                    break
                elif isinstance(item, dict) and item.get("type") == "image":
                    # 图片段结束后未见文案又出现图片（异常格式），直接终止
                    break
                next_idx += 1

        return selected_text, selected_images

    @staticmethod
    def _format_price(price):
        """
        格式化价格显示，去除无效的尾随 0（例如 219.00 -> 219，20.50 -> 20.5）。
        - 入参 price: str | int | float
        - 出参: str 格式化后的价格字符串
        """
        if price is None:
            return ""
        price_str = str(price).strip()
        try:
            val = float(price_str)
            if val.is_integer():
                return str(int(val))
            return f"{val:.2f}".rstrip("0").rstrip(".")
        except (ValueError, TypeError):
            return price_str

    @classmethod
    def _build_official_recommend_desc(cls, item, coupon_money=None):
        """
        根据 item 数据动态生成结构化官方推荐语言。

        【生成规则】
        - 第1行：活动引导语（如 ★天猫双11狂欢节 官方立减85折起★）
        - 第2行：短标题 / 商品标题
        - 第3行：价格行（原价：xx元，券后价【xx元】；若无优惠券则不生成本行）
        - 第4行：商品详情原始推荐文案 itemdesc
        """
        lines = []

        # 1. 第1行：大促活动引导文案
        activity_info = item.get("activity_info")
        if isinstance(activity_info, list) and activity_info:
            act_obj = activity_info[0]
            if isinstance(act_obj, dict):
                raw_guide = act_obj.get("guide_article") or act_obj.get("name") or ""
                # 清洗制表符和多余连续空格
                clean_guide = re.sub(r"\s+", " ", str(raw_guide)).strip()
                if clean_guide:
                    lines.append(clean_guide)

        # 2. 第2行：推荐标题（优先使用短标题）
        title = (item.get("itemshorttitle") or item.get("itemtitle") or "").strip()
        if title:
            lines.append(title)

        # 3. 第3行：价格行（判断是否有优惠券，无券则不展示此行）
        coupon_val = coupon_money if coupon_money is not None else item.get("couponmoney")
        has_coupon = False
        try:
            has_coupon = float(coupon_val or 0) > 0
        except (ValueError, TypeError):
            has_coupon = False

        if has_coupon:
            p_str = cls._format_price(item.get("itemprice"))
            end_p_str = cls._format_price(item.get("itemendprice"))
            if p_str and end_p_str:
                lines.append(f"原价：{p_str}元，券后价【{end_p_str}元】")
            elif end_p_str:
                lines.append(f"券后价【{end_p_str}元】")
            elif p_str:
                lines.append(f"原价：{p_str}元")

        # 4. 第4行：商品原始推荐文案
        desc = (item.get("itemdesc") or "").strip()
        if desc:
            lines.append(desc)

        return "\n".join(lines).strip()

    def process_item(self, item):
        """
        处理单个商品全流程：
        校验 -> 断点判断(若存在则增量更新动态元数据) -> 转链 -> 社群与官方素材双路下载 -> 生成官方推荐文案 -> 提炼首组图文 -> 汇总持久化。
        - 入参 item: dict，来自列表接口的单商品原始字典。
        - 出参: str，处理结果状态码 ("ok" | "skip" | "fail")。
        """
        raw_id = item.get("id")
        itemid = (item.get("itemid") or "").strip()
        if raw_id is None or not str(raw_id).strip() or not itemid:
            logger.warning(
                "[商品校验/跳过] 商品基础数据缺失核心主键，无法处理 | 好单库ID: [%s] | 淘系ID: [%s]",
                raw_id, itemid or "空",
            )
            return "fail"

        record_id = str(raw_id).strip()
        short_title = (item.get("itemshorttitle") or item.get("itemtitle") or "")[:20]

        # ==================== 1. 断点续跑校验与元数据增量更新 ====================
        existing = self.all_items_map.get(record_id)
        is_material_complete = (
                not self.force
                and isinstance(existing, dict)
                and bool(existing.get("two_in_one_url"))
                and "ordered_material" in existing
                and "item_images" in existing
        )

        if is_material_complete:
            # 商品已有完整转链及图文素材，跳过转链与重复下载大图，但全面更新最新的动态业务字段
            coupon_val = existing.get("coupon_money") or item.get("couponmoney")
            official_desc = self._build_official_recommend_desc(item, coupon_money=coupon_val)
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")

            # 同步最新商品动态属性
            existing["item_title"] = item.get("itemtitle") or existing.get("item_title")
            existing["item_short_title"] = item.get("itemshorttitle") or existing.get("item_short_title")
            existing["general_index"] = item.get("general_index")
            existing["item_price"] = item.get("itemprice")
            existing["item_end_price"] = item.get("itemendprice")
            existing["coupon_money"] = coupon_val
            existing["sale_2h"] = item.get("itemsale2")
            existing["todaysale"] = item.get("todaysale")
            existing["itemsale"] = item.get("itemsale")
            existing["tkrates"] = item.get("tkrates")
            existing["tkmoney"] = item.get("tkmoney")
            existing["shop_name"] = item.get("shopname")
            existing["shop_type"] = item.get("shoptype")
            existing["brand_name"] = item.get("brand_name")
            existing["activity_plan"] = item.get("activity_plan")
            existing["raw_itemdesc"] = (item.get("itemdesc") or "").strip()
            existing["item_desc"] = official_desc
            existing["updated_at"] = now_str

            # 若先前未获取到社群素材，发群正文 real_content 同步更新为最新官方推荐语
            if not existing.get("community_content"):
                existing["real_content"] = official_desc

            self.all_items_map[record_id] = existing
            self._save_all_items()

            logger.info(
                "[断点续跑/更新字段] 商品素材已完整，已更新最新动态字段 | 商品ID: [%s] | 淘系ID: [%s] | "
                "2h销量: [%s] | 今日销量: [%s] | 券后价: [%s元] | 标题: 【%s】",
                record_id, itemid, item.get("itemsale2") or "-", item.get("todaysale") or "-",
                                   item.get("itemendprice") or "-", short_title,
            )
            return "skip"

        # ==================== 2. 高佣转链 ====================
        title = item.get("itemtitle") or item.get("itemshorttitle") or ""
        conv = self.converter.convert_item(
            itemid=itemid,
            activityid=item.get("activityid") or "",
            title=title,
        )
        if not conv.get("status"):
            logger.error(
                "❌ [商品转链/失败] 调用好单库转链接口未成功，终止该商品后续处理 | 商品ID: [%s] | 淘系ID: [%s] | 标题: 【%s】 | 失败原因: %s (请检查PID授权、APIKEY或商品是否已下架)",
                record_id, itemid, short_title, conv.get("msg"),
            )
            return "fail"

        # ==================== 3. 拉取并下载 A 路素材：社群实拍图与文案 ====================
        comm_imgs, comm_copy, comm_group, ordered_material = self.fetch_community_material(
            hdk_id=record_id,
            taoword=conv["taoword"] or "",
            two_in_one_url=conv["two_in_one_url"] or "",
            fallback_itemid=itemid,
        )
        comm_local_paths, url_to_comm_path = self._download_batch_images(
            comm_imgs, f"{record_id}_comm", "社群实拍图", max_count=MAX_ITEM_IMAGES
        )
        for entry in ordered_material:
            if entry.get("type") == "image":
                entry["local_path"] = url_to_comm_path.get(entry.get("url"))

        # ==================== 4. 拉取并下载 B 路素材：官方主图与轮播图 ====================
        official_imgs = [item["itempic"]] if item.get("itempic") else []
        if item.get("images"):
            official_imgs.extend(u for u in item["images"].split(",") if u.strip())
        item_local_paths, _ = self._download_batch_images(
            official_imgs, f"{record_id}_item", "官方主图", max_count=MAX_ITEM_IMAGES
        )

        # ==================== 5. 生成官方推荐语言与首组素材提炼 ====================
        coupon_val = conv.get("coupon_money") or item.get("couponmoney")
        official_desc = self._build_official_recommend_desc(item, coupon_money=coupon_val)
        selected_content, selected_material_images = self._extract_material_group(ordered_material)

        # 优先使用社群首组图文；若无社群素材，则降级采纳官方推荐文案与官方主图
        if selected_content or selected_material_images:
            final_real_content = selected_content
            final_material_images = selected_material_images
        else:
            final_real_content = official_desc
            final_material_images = item_local_paths

        # ==================== 6. 组装完整记录并原子落盘 ====================
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "id": record_id,
            "item_id": itemid,
            "general_index": item.get("general_index"),
            "item_title": item.get("itemtitle"),
            "item_short_title": item.get("itemshorttitle"),
            "two_in_one_url": conv["two_in_one_url"],
            "taoword": conv["taoword"],
            "community_group": comm_group,
            "community_content": comm_copy,
            "community_images": comm_local_paths,
            "ordered_material": ordered_material,
            "item_desc": official_desc,
            "raw_itemdesc": (item.get("itemdesc") or "").strip(),
            "item_images": item_local_paths,
            "real_content": final_real_content,
            "material_images": final_material_images,
            "detail_url": f"{ITEM_DETAIL_URL}?id={record_id}",
            "coupon_money": coupon_val,
            "item_price": item.get("itemprice"),
            "item_end_price": item.get("itemendprice"),
            "sale_2h": item.get("itemsale2"),
            "todaysale": item.get("todaysale"),
            "itemsale": item.get("itemsale"),
            "tkrates": item.get("tkrates"),
            "tkmoney": item.get("tkmoney"),
            "shop_name": item.get("shopname"),
            "shop_type": item.get("shoptype"),
            "brand_name": item.get("brand_name"),
            "activity_plan": item.get("activity_plan"),
            "added_at": now_str,
            "created_at": now_str,
            "updated_at": now_str,
        }

        self.all_items_map[record_id] = record
        self._save_all_items()

        logger.info(
            "[商品处理/完成] 转链与素材落盘成功 | 商品ID: [%s] | 淘系ID: [%s] | 券面额: [%s元] | "
            "采纳文案字数: [%d字] | 采纳图数: [%d张] | 标题: 【%s】",
            record_id, itemid, coupon_val or 0,
            len(final_real_content), len(final_material_images), short_title,
        )
        return "ok"


    def run(self, pages, start_page=1):
        """
        执行指定页数范围的批量抓取任务。
        - 出参: dict，本轮统计信息 {"ok": int, "skip": int, "fail": int}。
        """
        stats = {"ok": 0, "skip": 0, "fail": 0}
        end_page = start_page + pages - 1
        logger.info(
            "[任务启动] 开始执行好单库活动商品抓取 | 活动ID: [%s] | 排序类型: [近2h销量(%s)] | 页码范围: [第%d~%d页]",
            ACTIVITY_ID, SORT_TYPE, start_page, end_page,
        )

        for page_no in range(start_page, end_page + 1):
            try:
                data = self.fetch_page(page_no)
            except Exception as e:
                logger.error(
                    "❌ [列表拉取/失败] 活动页商品列表请求彻底失败，跳过本页 | 页码: [第%d页] | 故障原因: %s (请检查好单库网站连通性或反爬策略)",
                    page_no, e,
                )
                stats["fail"] += PAGE_SIZE
                continue

            items = data.get("items") or []
            logger.info(
                "[列表拉取/成功] 已获取分页商品列表 | 页码: [第%d/%s页] | 本页商品数: [%d] | 活动总商品数: [%s]",
                page_no, data.get("page_total", "?"), len(items), data.get("total", "?"),
            )

            for item in items:
                try:
                    status = self.process_item(item)
                    stats[status] += 1
                except Exception as e:
                    logger.error(
                        "❌ [商品处理/未捕获异常] 处理单个商品时发生意外崩溃 | 商品ID: [%s] | 淘系ID: [%s] | 故障原因: %s",
                        item.get("id"), item.get("itemid"), e, exc_info=True,
                    )
                    stats["fail"] += 1
                time.sleep(REQUEST_GAP)

            logger.info(
                "[批次汇总] 本轮抓取任务执行完毕 | 成功: [%d] | 跳过: [%d] | 失败: [%d] | 汇总库累计商品: [%d条] | 存储路径: <%s>",
                stats["ok"], stats["skip"], stats["fail"], len(self.all_items_map), ALL_ITEMS_JSON,
            )
            # : 疑似业务边界缺陷预警 —— 原代码将 `return stats` 缩进在 `for page_no` 循环体末尾！
            # 这会导致：1) 当 FIXED_PAGES > 1 时，只要第一页成功执行完就会立刻 return 退出，后续页码永远不会被抓取；
            # 2) 若所有页码的 fetch_page 均异常触发 continue，循环结束后函数会隐式返回 None。
            # 为严格遵守“不擅自改变原业务行为”原则，此处保留原缩进位置，若需支持多页抓取请将此 return 移至 for 循环外部。
        return stats


def _validate_copy_content(raw_content: str):
    """
    清洗并验证大模型返回的 JSON 结构。

    验证字段：
    - product_name: 核心品名及规格
    - verified_price: 以图片核实后的真实到手价或单价
    - conflict_resolved: 若图文有冲突，说明修正了什么（无冲突填'无'）
    - key_reason: 核心低价/排雷理由
    - wechat_copy: 微信群/朋友圈促单文案

    出参: tuple (is_valid: bool, parsed_data: dict | None, err_msg: str)
    """
    if not raw_content or not isinstance(raw_content, str):
        return False, None, "大模型返回内容为空"

    data = string_to_object(raw_content)

    required_keys = [
        "wechat_copy",
    ]
    missing_keys = [k for k in required_keys if k not in data or not str(data[k]).strip()]
    if missing_keys:
        return False, None, f"缺失必要字段或字段值为空: {missing_keys}"

    return True, data, ""


def batch_generate_wechat_copy():
    """
    扫描本地汇总商品，筛选符合条件的商品批量调用大模型生成种草话术：
    1. 扫描条件: 生成失败次数 < 3 且 尚未生成 wechat_copy
    2. 提示词组装: 基础提示词 + item_desc + community_content
    3. 图片组装: item_images 与 material_images 去重合并（过滤无效/不存在的路径）
    4. 重试机制: 单商品调用失败最多重试 3 次，全部失败则累计生成次数并落盘
    5. 成功处理: 更新 wechat_copy、记录生成时间 copy_generated_at 并立即原子落盘
    """
    if not os.path.exists(ALL_ITEMS_JSON):
        logger.warning("[话术生成/跳过] 本地商品汇总库不存在 | 路径: <%s>", ALL_ITEMS_JSON)
        return

    try:
        with open(ALL_ITEMS_JSON, "r", encoding="utf-8") as f:
            items_list = json.load(f)
    except Exception as e:
        logger.error("❌ [话术生成/读取失败] 读取本地商品文件失败: %s", e)
        return

    if not isinstance(items_list, list):
        logger.warning("[话术生成/跳过] 商品库数据非列表格式，无法遍历")
        return

    base_prompt = read_file_to_str(PROMPT_FILE_PATH)
    if not base_prompt:
        logger.error("❌ [话术生成/终止] 未读取到基础提示词模版，请检查文件路径: <%s>", PROMPT_FILE_PATH)
        return

    # 扫描符合条件的商品
    candidates = []
    for item in items_list:
        if not isinstance(item, dict):
            continue
        attempts = item.get("copy_gen_attempts", 0)
        has_copy = bool(item.get("wechat_copy") and str(item.get("wechat_copy")).strip())
        if attempts < 3 and not has_copy:
            candidates.append(item)

    logger.info(
        "[话术生成/扫描] 扫描完毕 | 库内总数: [%d] | 待生成任务数: [%d]",
        len(items_list), len(candidates),
    )

    if not candidates:
        return

    def _save_current_state():
        """原子保存当前全量商品列表到磁盘"""
        tmp_path = f"{ALL_ITEMS_JSON}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f_out:
                json.dump(items_list, f_out, ensure_ascii=False, indent=2)
            os.replace(tmp_path, ALL_ITEMS_JSON)
        except Exception as err:
            logger.error("❌ [话术生成/落盘失败] 保存商品数据异常: %s", err)

    LLM_MAX_RETRIES = 3

    for idx, item in enumerate(candidates, start=1):
        item_id = item.get("id") or item.get("item_id")
        short_title = (item.get("item_short_title") or item.get("item_title") or "")[:20]

        # 1. 组装输入文本: 基础 Prompt + 官方描述 + 社群素材
        item_desc = item.get("item_desc") or ""
        community_content = item.get("community_content") or ""
        prompt_text = (
            f"{base_prompt}\n\n"
            f"【商品官方信息与描述】\n{item_desc}\n\n"
            f"【社群素材文案】\n{community_content}"
        )

        # 2. 组装图片路径: item_images + material_images 去重并校验本地存在性
        combined_imgs = (item.get("item_images") or []) + (item.get("material_images") or [])
        file_paths = []
        seen_paths = set()
        for p in combined_imgs:
            if p and p not in seen_paths:
                seen_paths.add(p)
                if os.path.exists(p):
                    file_paths.append(p)

        logger.info(
            "[话术生成/处理中] 开始生成商品文案 | 进度: [%d/%d] | 商品ID: [%s] | 关联图数: [%d张] | 标题: 【%s】",
            idx, len(candidates), item_id, len(file_paths), short_title,
        )

        success = False
        last_error = ""

        # 3. 循环重试 3 次
        for attempt in range(1, LLM_MAX_RETRIES + 1):
            try:
                result = generate_content(
                    prompt=prompt_text,
                    preset_model_group="low",
                    file_paths=file_paths,
                )
                if result.get("status") != "✅ 成功":
                    detail = "；".join(str(e) for e in (result.get("error_history") or []))
                    raise RuntimeError(detail or result.get("content") or "模型调用返回非成功状态")

                raw_content = result.get("content", "")
                is_valid, parsed_data, err_msg = _validate_copy_content(raw_content)
                if not is_valid:
                    raise ValueError(err_msg)

                # 4. 生成成功：更新 wechat_copy 及生成时间
                item["wechat_copy"] = parsed_data["wechat_copy"]
                item["copy_generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                # 保存扩展结构以便审计追溯（可选）
                item["copy_meta"] = {
                    "product_name": parsed_data.get("product_name"),
                    "verified_price": parsed_data.get("verified_price"),
                    "conflict_resolved": parsed_data.get("conflict_resolved"),
                    "key_reason": parsed_data.get("key_reason"),
                }
                _save_current_state()

                logger.info(
                    "✅ [话术生成/成功] 商品文案生成并落盘 | 商品ID: [%s] | 重试轮次: [%d/%d] | 文案字数: [%d字]",
                    item_id, attempt, LLM_MAX_RETRIES, len(item["wechat_copy"]),
                )
                success = True
                break

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "[话术生成/重试] 接口调用或格式校验失败 | 商品ID: [%s] | 轮次: [%d/%d] | 原因: %s",
                    item_id, attempt, LLM_MAX_RETRIES, last_error,
                )
                if attempt < LLM_MAX_RETRIES:
                    time.sleep(2 ** attempt)

        # 5. 3次全失败：增加尝试次数并保存
        if not success:
            item["copy_gen_attempts"] = item.get("copy_gen_attempts", 0) + 1
            item["copy_gen_last_error"] = last_error
            _save_current_state()
            logger.error(
                "❌ [话术生成/彻底失败] 商品文案3次尝试均失败 | 商品ID: [%s] | 当前累计失败次数: [%d] | 最终原因: %s",
                item_id, item["copy_gen_attempts"], last_error,
            )

        time.sleep(0.5)


def main():
    """常驻定时调度入口：每间隔 RUN_INTERVAL 秒串行执行「数据抓取」与「种草话术生成」。"""
    logger.info(
        "[守护进程/启动] 好单库定时抓取与文案生成服务已启动 | 巡检周期: [%d秒] | 单次抓取页数: [%d页] | 强制刷新: [%s]",
        RUN_INTERVAL, FIXED_PAGES, FORCE_REFRESH,
    )

    while True:
        crawler = None
        # 1. 抓取与双源素材落地
        try:
            converter = HaoDanKuConverter(
                apikey=HAODANKU_APIKEY, pid=MY_PID, tb_name=TB_NAME
            )
            crawler = ActivityCrawler(converter, force=FORCE_REFRESH)
            crawler.run(pages=FIXED_PAGES, start_page=FIXED_START_PAGE)
        except Exception as e:
            logger.error(
                "❌ [抓取流程/异常] 定时抓取主流程发生未预期的顶层异常 | 故障原因: %s (将在下个周期自动尝试恢复)",
                e, exc_info=True,
            )
        finally:
            if crawler is not None:
                crawler.close()

        # # 2. 串行执行种草话术生成
        # try:
        #     logger.info("[守护进程/流水线] 抓取流程完成，开始串行触发种草话术生成任务")
        #     batch_generate_wechat_copy()
        # except Exception as e:
        #     logger.error(
        #         "❌ [话术流程/异常] 种草话术生成流程发生未预期的异常 | 故障原因: %s",
        #         e, exc_info=True,
        #     )
        #
        # logger.info(
        #     "[守护进程/休眠] 本轮抓取与话术生成任务已全部结束，等待进入下一轮 | 休眠时长: [%d秒]",
        #     RUN_INTERVAL,
        # )
        time.sleep(RUN_INTERVAL)


if __name__ == "__main__":
    # batch_generate_wechat_copy()

    main()