# 文件路径建议: app/taobao_utils.py
from datetime import datetime
import hashlib
import time
from typing import Optional
import requests

from common.common_utils import get_config, setup_logger

logger = setup_logger("taobao_utils")

# ----------------- 1. 默认配置信息 -----------------
DEFAULT_APP_KEY = get_config("myself_taobao_app_key")
DEFAULT_APP_SECRET = get_config("myself_taobao_app_secret")
DEFAULT_ADZONE_ID = get_config("myself_taobao_pid")
GATEWAY_URL = "https://eco.taobao.com/router/rest"


def generate_sign(params: dict, app_secret: str) -> str:
    """按淘宝开放平台规范生成 MD5 签名"""
    valid_items = [
        (k, str(v)) for k, v in params.items() if v is not None and k != "sign"
    ]
    sorted_items = sorted(valid_items, key=lambda x: x[0])
    query_str = (
        app_secret + "".join(f"{k}{v}" for k, v in sorted_items) + app_secret
    )
    return hashlib.md5(query_str.encode("utf-8")).hexdigest().upper()


def format_top_error(res: dict) -> str:
    """格式化淘宝开放平台错误详情，完整保留 code / sub_code / request_id"""
    if not isinstance(res, dict):
        return f"响应格式非字典: {type(res)}"
    err = res.get("error_response", {})
    if not isinstance(err, dict):
        return f"未知错误结构: {err}"

    code = err.get("code", "-")
    msg = err.get("msg", "-")
    sub_code = err.get("sub_code", "-")
    sub_msg = err.get("sub_msg", "-")
    req_id = err.get("request_id", "-")
    return f"code={code} | msg={msg} | sub_code={sub_code} | sub_msg={sub_msg} | request_id={req_id}"


def top_execute(
    method: str,
    biz_params: dict,
    app_key: Optional[str] = None,
    app_secret: Optional[str] = None,
    session: Optional[requests.Session] = None,
    timeout: int = 15,
    max_retries: int = 2,
) -> dict:
    """执行淘宝开放平台 (TOP) API 请求（带耗时监控、完整错误透传与超时重试）"""
    use_app_key = app_key or DEFAULT_APP_KEY
    use_app_secret = app_secret or DEFAULT_APP_SECRET

    if not use_app_key or not use_app_secret:
        err_msg = "未配置淘宝联盟 app_key 或 app_secret"
        logger.error("[TOP请求/配置缺失] method=%s | %s", method, err_msg)
        return {"error_response": {"code": "CONFIG_ERROR", "sub_msg": err_msg}}

    requester = session or requests

    for attempt in range(1, max_retries + 1):
        start_time = time.perf_counter()
        try:
            params = {
                "method": method,
                "app_key": use_app_key,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "format": "json",
                "v": "2.0",
                "sign_method": "md5",
            }
            params.update(biz_params)
            params["sign"] = generate_sign(params, use_app_secret)

            response = requester.post(GATEWAY_URL, data=params, timeout=timeout)
            cost_ms = (time.perf_counter() - start_time) * 1000
            response.raise_for_status()

            try:
                data = response.json()
            except ValueError as je:
                raw_preview = response.text[:300] if response.text else "<empty>"
                logger.error(
                    "[TOP请求/JSON解析失败] method=%s | 耗时=%.0fms | 状态码=%s | 原始响应预览=%s",
                    method, cost_ms, response.status_code, raw_preview,
                )
                return {
                    "error_response": {
                        "code": "JSON_DECODE_ERROR",
                        "sub_msg": f"JSON解析失败: {je}, 原始响应: {raw_preview}",
                    }
                }

            # 记录网关返回的耗时附加信息（便于上层打印）
            if isinstance(data, dict):
                data["_cost_ms"] = cost_ms

            return data

        except requests.exceptions.Timeout as te:
            cost_ms = (time.perf_counter() - start_time) * 1000
            logger.warning(
                "[TOP请求/超时] method=%s | 尝试=%d/%d | 耗时=%.0fms | 参数=%s | 异常=%s",
                method, attempt, max_retries, cost_ms, biz_params, te,
            )
            if attempt == max_retries:
                return {
                    "error_response": {
                        "code": "NETWORK_TIMEOUT",
                        "sub_msg": f"请求超时({timeout}s)，已重试{max_retries}次: {te}",
                    }
                }
            time.sleep(0.5 * attempt)

        except requests.exceptions.RequestException as re:
            cost_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "[TOP请求/网络异常] method=%s | 尝试=%d/%d | 耗时=%.0fms | 异常=%s",
                method, attempt, max_retries, cost_ms, re,
            )
            if attempt == max_retries:
                return {
                    "error_response": {
                        "code": "REQUEST_EXCEPTION",
                        "sub_msg": f"网络请求异常: {re}",
                    }
                }
            time.sleep(0.5 * attempt)

        except Exception as e:
            cost_ms = (time.perf_counter() - start_time) * 1000
            logger.exception(
                "[TOP请求/未知异常] method=%s | 耗时=%.0fms | 异常=%s",
                method, cost_ms, e,
            )
            return {"error_response": {"code": "UNKNOWN_ERROR", "sub_msg": str(e)}}

    return {"error_response": {"code": "MAX_RETRIES_EXCEEDED", "sub_msg": "超出最大重试次数"}}


def search_items(
    keyword: str,
    desire_count: int = 0,
    app_key: Optional[str] = None,
    app_secret: Optional[str] = None,
    adzone_id: Optional[str] = None,
) -> list:
    """使用 27939 权限包 (taobao.tbk.dg.material.optional.upgrade) 搜索淘宝联盟商品

    :param keyword: 搜索关键词
    :param desire_count: 需要的商品数量，如果是 0 则查询所有数据（受 max_page_limit 保护）
    :param app_key: 可选覆盖配置中的 app_key
    :param app_secret: 可选覆盖配置中的 app_secret
    :param adzone_id: 可选覆盖配置中的推广位 pid (adzone_id)
    :return: 商品列表（发生任何异常至少返回已获取列表或空列表 []，保证不崩溃）
    """
    res = search_taobao_goods_by_keyword(
        search_key=keyword,
        limit_count=desire_count,
        app_key=app_key,
        app_secret=app_secret,
        adzone_id=adzone_id,
    )
    if res.get("error"):
        logger.warning(
            "[search_items/告警透传] 关键词: [%s] | 状态: %s | 已获取条数: %d | 错误详情: %s",
            keyword, res.get("status"), len(res.get("data", [])), res.get("error"),
        )
    return res.get("data", [])


def search_taobao_goods_by_keyword(
    search_key: str,
    limit_count: int = 0,
    app_key: Optional[str] = None,
    app_secret: Optional[str] = None,
    adzone_id: Optional[str] = None,
    max_page_limit: int = 100,
) -> dict:
    """与主应用风格对齐的搜索封装函数（带全链路分页监控与防误判机制）

    :return: {"status": "success" | "partial_success" | "error", "data": list, "error": str}
    """
    all_results = []
    use_adzone_id = adzone_id or DEFAULT_ADZONE_ID

    if not search_key or not str(search_key).strip():
        logger.error("[淘宝搜索/参数错误] 搜索关键词为空")
        return {"status": "error", "data": [], "error": "搜索关键词为空"}
    if limit_count < 0:
        logger.error("[淘宝搜索/参数错误] limit_count=%d 不能小于 0", limit_count)
        return {"status": "error", "data": [], "error": "limit_count 不能小于 0"}
    if not use_adzone_id:
        logger.error("[淘宝搜索/配置错误] adzone_id (推广位PID) 未配置")
        return {"status": "error", "data": [], "error": "未配置淘宝联盟推广位 adzone_id"}

    clean_key = str(search_key).strip()
    page_no = 1
    error_msg = ""
    stop_reason = "未知原因"
    total_start_time = time.perf_counter()

    logger.info(
        "[淘宝搜索/开始] 关键词: [%s] | 目标条数: %s | 最大页数限制: %d | adzone_id: %s",
        clean_key, "不限(全量)" if limit_count == 0 else limit_count, max_page_limit, use_adzone_id,
    )

    try:
        with requests.Session() as session:
            while True:
                if limit_count == 0:
                    current_page_size = 100
                else:
                    remain = limit_count - len(all_results)
                    if remain <= 0:
                        stop_reason = f"已达到目标数量 ({limit_count} 条)"
                        break
                    # 注意：淘宝后置过滤常导致每页少返回 1~5 条，若余量接近 100 或较小，适当多请求一点并在末尾切片，避免多翻一页
                    current_page_size = min(max(remain + 5, 20), 100) if remain < 100 else 100

                biz_params = {
                    "q": clean_key,
                    "adzone_id": use_adzone_id,
                    "page_no": page_no,
                    "page_size": current_page_size,
                }
                res = top_execute(
                    "taobao.tbk.dg.material.optional.upgrade",
                    biz_params,
                    app_key=app_key,
                    app_secret=app_secret,
                    session=session,
                )

                cost_ms = res.pop("_cost_ms", 0.0) if isinstance(res, dict) else 0.0

                # 1. 校验 TOP 网关错误响应
                if not isinstance(res, dict) or "error_response" in res:
                    error_msg = format_top_error(res)
                    stop_reason = f"第 {page_no} 页接口报错中止 ({error_msg})"
                    logger.error(
                        "[淘宝搜索/接口报错] 关键词: [%s] | 页码: %d | 耗时: %.0fms | 详情: %s",
                        clean_key, page_no, cost_ms, error_msg,
                    )
                    break

                # 2. 解析业务响应体与元数据
                resp_body = res.get("tbk_dg_material_optional_upgrade_response", {})
                request_id = resp_body.get("request_id", "-")
                total_results = int(resp_body.get("total_results", 0) or 0)
                page_items = resp_body.get("result_list", {}).get("map_data", [])

                # 3. 校验当前页数据是否为空
                if not page_items or not isinstance(page_items, list):
                    stop_reason = (
                        f"第 {page_no} 页返回空列表，数据已见底 "
                        f"(total_results={total_results}, request_id={request_id})"
                    )
                    logger.info(
                        "[淘宝搜索/见底] 关键词: [%s] | 页码: %d | 平台匹配总数: %d | request_id: %s | 响应结构keys: %s",
                        clean_key, page_no, total_results, request_id, list(resp_body.keys()),
                    )
                    break

                current_got = len(page_items)
                all_results.extend(page_items)

                logger.info(
                    "[淘宝搜索/分页进度] 关键词: [%s] | 第 %d 页 | 请求/返回: %d/%d 条 "
                    "| 累计获取: %d 条 | 平台total_results: %d | 耗时: %.0fms | req_id: %s",
                    clean_key, page_no, current_page_size, current_got,
                    len(all_results), total_results, cost_ms, request_id,
                )

                # 4. 判断是否达到调用方指定的目标数量
                if limit_count > 0 and len(all_results) >= limit_count:
                    stop_reason = f"已满足目标数量 ({len(all_results)} >= {limit_count})"
                    break

                # 5. 核心修复：不能用 current_got < current_page_size 判断见底！
                # 因为淘宝后置过滤经常使 100 条请求返回 95~99 条。
                # 只有当平台明确返回 total_results 且当前分页理论扫描位置已覆盖 total_results 时，才判定为最后一页
                if total_results > 0 and (page_no * current_page_size) >= total_results:
                    stop_reason = (
                        f"已扫描完平台全部匹配结果 "
                        f"(当前页码 {page_no} * 每页 {current_page_size} >= total_results {total_results})"
                    )
                    break

                page_no += 1
                if page_no > max_page_limit:
                    stop_reason = f"达到最大翻页上限保护 (max_page_limit={max_page_limit})"
                    break

                # 多页循环时微量休眠，防止触发高频流控
                time.sleep(0.15)

        if limit_count > 0:
            all_results = all_results[:limit_count]

        total_cost_s = time.perf_counter() - total_start_time
        logger.info(
            "[淘宝搜索/结束汇总] 关键词: [%s] | 最终条数: %d | 共请求页数: %d | 总耗时: %.2fs | 停止原因: %s",
            clean_key, len(all_results), page_no, total_cost_s, stop_reason,
        )

        # 状态判定：全失败为 error，中途报错但已有部分数据为 partial_success（或兼容原逻辑）
        if error_msg and not all_results:
            return {"status": "error", "data": [], "error": error_msg}
        return {"status": "success", "data": all_results, "error": error_msg}

    except Exception as e:
        total_cost_s = time.perf_counter() - total_start_time
        logger.exception(
            "[淘宝搜索/未捕获异常] 关键词: [%s] | 已获取: %d 条 | 耗时: %.2fs | 异常: %s",
            clean_key, len(all_results), total_cost_s, e,
        )
        return {
            "status": "success" if all_results else "error",
            "data": all_results,
            "error": str(e),
        }


if __name__ == "__main__":
    keyword = "前行者F61无线键盘鼠标套装电竞游戏专用笔记本电脑办公蓝牙小型"
    logger.info(f"=== 正在搜索关键词: 【{keyword}】 ===")
    # 建议先测试指定条数（如 500 条），因为 desire_count=0 会连续翻满 100 页（约需 5~10 分钟）
    items = search_items(keyword, desire_count=500)
    logger.info(f"=== 搜索结果数量: {len(items)} ===")