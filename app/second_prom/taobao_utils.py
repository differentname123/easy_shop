import json
import logging
import re
import urllib.parse
import requests

# ==================== 1. 日志格式配置 (排查必备) ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger("TBK_Convert")

# ==================== 2. 你的专属配置信息 ====================
HAODANKU_APIKEY = "9D3E0E43E5FD"  # 你的好单库专属 APIKEY
MY_PID = "mm_328750149_3161250458_115832850163"  # 你的淘宝联盟 PID

# 请填写你在好单库推广中心 (http://publish.haodanku.com/tbauth/index.html) 授权的淘宝账号昵称
# 必须与授权列表中显示的昵称完全一致！
TB_NAME = "不同名字123"  # 截图中的自有账号授权昵称


class HaoDanKuConverter:

  def __init__(self, apikey: str, pid: str, tb_name: str):
    self.apikey = apikey
    self.pid = pid
    self.tb_name = tb_name
    self.session = requests.Session()
    # 模拟真实手机 Safari UA，防止访问 s.click 发生反爬拦截
    self.session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X)"
            " AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5"
            " Mobile/15E148 Safari/604.1"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

  def resolve_short_url(self, short_url: str) -> str:
    """追踪穿透 s.click.taobao.com 短链接重定向"""
    logger.info(f"[短链穿透] 检测到推广短链，正在追踪重定向: {short_url}")
    try:
      resp = self.session.get(short_url, allow_redirects=True, timeout=10)
      final_url = resp.url
      logger.info(f"[短链穿透] 重定向到达终点: {final_url}")

      # 情况1: 包含 tu 参数（淘宝常见中间跳板链接）
      if "tu=" in final_url:
        parsed = urllib.parse.urlparse(final_url)
        queries = urllib.parse.parse_qs(parsed.query)
        if "tu" in queries:
          decoded_tu = urllib.parse.unquote(queries["tu"][0])
          logger.info(f"[短链穿透] 从 tu 参数中提取出真实目标: {decoded_tu}")
          return decoded_tu

      # 情况2: 页面存在 JS 重定向脚本
      if "location.href" in resp.text:
        match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", resp.text)
        if match:
          js_url = match.group(1)
          logger.info(f"[短链穿透] 从 JS 脚本中提取出目标 URL: {js_url}")
          return js_url

      return final_url
    except Exception as e:
      logger.error(f"[短链穿透] 解析短链接出错: {e}")
      return short_url

  def parse_input_features(self, raw_text: str):
    """
    智能分析输入文案或纯链接，提取出：
    1. 商品ID (支持普通纯数字ID 以及 uland加密字符ID)
    2. 优惠券ID (activityId，避免丢券)
    3. 商品标题 (用于生成淘口令)
    """
    logger.info("[特征分析] 开始解析输入文案/链接特征...")
    item_id = None
    activity_id = None
    title = ""

    # 1. 提取商品标题（抓取第一行非 URL 内容）
    lines = [
        line.strip()
        for line in raw_text.splitlines()
        if line.strip() and not line.strip().startswith("http")
    ]
    if lines:
      title = lines[0][:40]

    # 好单库规定：若返回淘口令，title 必填且必须大于8个字符
    if len(title) < 8:
      title = f"{title}【超值爆款正品特惠推荐】"
    logger.info(f"[特征分析] 锁定商品标题: {title}")

    # 2. 如果包含 s.click 短链，先进行重定向解析
    sclick_match = re.search(
        r"https?://s\.click\.taobao\.com/[a-zA-Z0-9]+", raw_text
    )
    search_scope = raw_text
    if sclick_match:
      short_url = sclick_match.group(0)
      resolved_url = self.resolve_short_url(short_url)
      search_scope += f"\n{resolved_url}"

    # 3. 提取优惠券 activityId (32位十六进制字符串)
    coupon_match = re.search(
        r"activityId=([a-zA-Z0-9]{20,40})", search_scope, re.IGNORECASE
    )
    if coupon_match:
      activity_id = coupon_match.group(1)
      logger.info(f"[特征分析] 锁定优惠券 activityId: {activity_id}")

    # 4. 提取商品 ID
    # 规则 A: uland 淘系加密 ID (如: Vkp4rv8u6tw5R3kwYkh2XYieUy-OJzVevmuZrXNm2B2SN)
    uland_item_match = re.search(
        r"uland\.(?:taobao|tmall)\.com/item/edetail\?.*?id=([a-zA-Z0-9_\-]+)",
        search_scope,
        re.IGNORECASE,
    )
    if uland_item_match:
      item_id = uland_item_match.group(1)
      logger.info(f"[特征分析] 锁定淘系混淆商品ID (加密ID): {item_id}")
    else:
      # 规则 B: 普通数字商品 ID (如: id=688401422805)
      num_item_match = re.search(
          r"[?&](?:id|itemId|item_id)=(\d{8,16})", search_scope, re.IGNORECASE
      )
      if num_item_match:
        item_id = num_item_match.group(1)
        logger.info(f"[特征分析] 锁定常规数字商品ID: {item_id}")

    return item_id, activity_id, title

  def convert_link(self, raw_text: str) -> dict:
    """请求好单库 ratesurl 转链接口"""
    item_id, activity_id, title = self.parse_input_features(raw_text)

    if not item_id:
      logger.error("[转链终止] 未能从内容中提取到有效的商品 ID！")
      return {
          "status": False,
          "msg": "未识别到有效的商品ID，请检查输入链接是否正确！",
      }

    # 请求好单库官方【淘联商品转链】接口
    api_url = "http://v3.api.haodanku.com/ratesurl"
    payload = {
        "apikey": self.apikey,
        "tb_name": self.tb_name,
        "pid": self.pid,
        "itemid": item_id,
        "get_taoword": 1,  # 1 为同时生成淘口令
        "title": title,
    }

    # 关键防亏券机制：如果原链接带券，强行绑定原券；如果不带券，好单库会自动抓取全网最大券
    if activity_id:
      payload["activityid"] = activity_id

    logger.info(
        f"[发起转链] 请求好单库 API, itemid={item_id}, activityid={activity_id}"
    )

    try:
      response = self.session.post(api_url, data=payload, timeout=10)
      res_json = response.json()
      logger.info(f"[接口响应] 好单库返回: {json.dumps(res_json, ensure_ascii=False)}")

      # 好单库标准成功码为 1 或 200
      if res_json.get("code") in [1, 200, "1", "200"]:
        data = res_json.get("data", {})
        # coupon_click_url 是二合一长链接（同时完成领券和下单绑定）
        my_coupon_url = data.get("coupon_click_url") or data.get("item_url")
        my_taoword = data.get("taoword") or data.get("new_model")
        coupon_money = data.get("couponmoney", 0)

        logger.info(
            f"[转链成功] 成功生成二合一领券链接! 券面额: {coupon_money} 元"
        )
        return {
            "status": True,
            "my_coupon_url": my_coupon_url,
            "my_taoword": my_taoword,
            "coupon_money": coupon_money,
            "title": title,
        }
      else:
        err_msg = res_json.get("msg") or "未知错误"
        logger.error(f"[好单库报错] 转链失败: {err_msg}")
        return {"status": False, "msg": err_msg}

    except Exception as e:
      logger.error(f"[请求异常] 网络或服务异常: {e}", exc_info=True)
      return {"status": False, "msg": str(e)}

  def rebuild_post_content(self, original_post: str, convert_res: dict) -> str:
    """将原线报文案中的链接替换为你自己的高佣二合一领券链接"""
    if not convert_res.get("status"):
      return f"转链失败，原因：{convert_res.get('msg')}"

    my_url = convert_res["my_coupon_url"]
    my_tpwd = convert_res["my_taoword"]

    # 将原帖中的所有旧链接替换为你自己的二合一链接
    cleaned_post = re.sub(
        r"https?://[^\s\u4e00-\u9fa5]+", my_url, original_post
    )

    # 组装输出文本
    final_text = (
        f"{cleaned_post}\n"
        f"-----------------------------------------\n"
        f"【领券+下单】：{my_url}\n"
        f"【复制淘口令】：{my_tpwd}"
    )
    return final_text


# ==================== 3. 运行测试入口 ====================
if __name__ == "__main__":
  converter = HaoDanKuConverter(
      apikey=HAODANKU_APIKEY, pid=MY_PID, tb_name=TB_NAME
  )

  # 测试示例：包含加密商品ID和优惠券ID的完整线报
  sample_input = """
    3CE12色哑光珠光眼影盘
    原价319.00元，券后价【111.91元】
    领券：https://uland.taobao.com/quan/detail?sellerId=4730117177653490850&activityId=6779888a46b04c3ead7be8c03ddd06c1
    下单:https://uland.TaoBao.com/item/edetail?id=Vkp4rv8u6tw5R3kwYkh2XYieUy-OJzVevmuZrXNm2B2SN
    券后119，淘金币后96.92，**后87 （319-官方立减39-券161-24.15淘金币-7.09限时**） 3CE终于做大盘眼影了，外形很有质感，粉质一如既往的好，细腻不浮粉，配色百搭，水
    """

  # 执行转换
  res = converter.convert_link(sample_input)

  if res["status"]:
    print("\n" + "=" * 20 + " 转链成功输出 " + "=" * 20)
    print(converter.rebuild_post_content(sample_input, res))
  else:
    print(f"\n转换失败: {res['msg']}")