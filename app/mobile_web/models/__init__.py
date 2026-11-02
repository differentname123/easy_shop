# -*- coding: utf-8 -*-
"""
mobile_web 数据契约模型包
"""
from app.mobile_web.models.product import ProductSummary, ProductDetail
from app.mobile_web.models.order import OrderItem, UserOrderStats
from app.mobile_web.models.user import UserProfile, RegisterOrLoginDTO

__all__ = [
    "ProductSummary",
    "ProductDetail",
    "OrderItem",
    "UserOrderStats",
    "UserProfile",
    "RegisterOrLoginDTO",
]
