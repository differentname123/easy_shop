# -*- coding: utf-8 -*-
""":authors:
    zhuxiaohu
:create_date:
    2026/8/22 14:27
:last_date:
    2026/8/22 14:27
:description:
    基于最新拼多多官方规范，使用 search + detail 级联查询商品完整详情
"""
from common.common_utils import get_config


if __name__ == "__main__":
    pdd_client_id = get_config("pdd_client_id")
    pdd_client_secret = get_config("pdd_client_secret")
    pdd_pid = get_config("pdd_pid")
    pdd_custom_parameters = get_config("pdd_custom_parameters")
