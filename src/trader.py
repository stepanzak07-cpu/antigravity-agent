"""
Модуль исполнения ордеров и автотрейдинга CS2 (Steam Order Bot).
Поддерживает два режима работы:
1. Dry-Run (Симуляция) — безопасное тестирование снайпер-стратегии без риска для реальных средств.
2. Live — боевое выставление реальных Buy-ордеров на торговой площадке Steam через API.
"""

import os
import uuid
from datetime import datetime
from typing import Optional
import httpx
from urllib.parse import quote

from src.strategy import TradeSignal, STEAM_FEE_PCT


class SteamOrderBot:
    """
    Торговый бот для выставления и управления Buy-ордерами на торговой площадке Steam.
    """
    CREATE_ORDER_URL = "https://steamcommunity.com/market/createbuyorder/"
    CANCEL_ORDER_URL = "https://steamcommunity.com/market/cancelbuyorder/"

    def __init__(
        self,
        dry_run: bool = True,
        initial_balance: float = 200.0,
        currency_code: int = 1,  # 1 = USD, 5 = RUB
        currency_symbol: str = "$",
        steam_login_secure: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        self.dry_run = dry_run
        self.initial_balance = initial_balance
        self.available_balance = initial_balance
        self.currency_code = currency_code
        self.currency_symbol = currency_symbol

        self.steam_login_secure = steam_login_secure or os.getenv("STEAM_LOGIN_SECURE")
        self.session_id = session_id or os.getenv("STEAM_SESSION_ID")

        # Реестр активных ордеров и сделок
        self.active_orders: list[dict] = []
        self.filled_orders: list[dict] = []
        self.total_profit_realized: float = 0.0

    def place_order_from_signal(self, signal: TradeSignal, live: bool = False) -> dict:
        """
        Выставление ордера на основе сигнала торговой стратегии.
        """
        return self.place_buy_order(
            item_name=signal.item_name,
            target_price=signal.target_buy_price,
            quantity=signal.quantity,
            breakeven_price=signal.breakeven_sell_price,
            expected_target=signal.expected_profit_target,
            live=live,
        )

    def place_buy_order(
        self,
        item_name: str,
        target_price: float,
        quantity: int = 1,
        breakeven_price: Optional[float] = None,
        expected_target: Optional[float] = None,
        live: bool = False,
    ) -> dict:
        """
        Выставляет Buy-ордер (в симуляции или на реальном рынке).
        """
        is_live_mode = live and not self.dry_run
        total_cost = round(target_price * quantity, 2)

        if total_cost > self.available_balance:
            return {
                "success": False,
                "error": f"Недостаточно средств: требуется {self.currency_symbol}{total_cost:.2f}, доступно {self.currency_symbol}{self.available_balance:.2f}",
                "item_name": item_name,
            }

        # Режим 1: Dry-Run (Симуляция)
        if not is_live_mode:
            order_id = f"DRY-{uuid.uuid4().hex[:8].upper()}"
            self.available_balance -= total_cost

            order_data = {
                "order_id": order_id,
                "item_name": item_name,
                "price": target_price,
                "quantity": quantity,
                "total_cost": total_cost,
                "breakeven_price": breakeven_price or round(target_price / (1.0 - STEAM_FEE_PCT), 2),
                "expected_target": expected_target or round(target_price * 1.25, 2),
                "status": "ACTIVE",
                "mode": "DRY_RUN",
                "created_at": datetime.now(),
            }
            self.active_orders.append(order_data)
            return {
                "success": True,
                "order_id": order_id,
                "mode": "DRY_RUN",
                "item_name": item_name,
                "price": target_price,
                "quantity": quantity,
                "total_cost": total_cost,
            }

        # Режим 2: Боевой (Live Steam Market API)
        if not self.steam_login_secure:
            return {
                "success": False,
                "error": "STEAM_LOGIN_SECURE не задан. Авторизация в Steam невозможна.",
                "item_name": item_name,
            }

        if not self.session_id:
            return {
                "success": False,
                "error": "STEAM_SESSION_ID не задан в .env. Для реального выставления ордеров требуется кука sessionid из браузера.",
                "item_name": item_name,
            }

        try:
            cookies = {
                "steamLoginSecure": self.steam_login_secure,
                "sessionid": self.session_id,
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": f"https://steamcommunity.com/market/listings/730/{quote(item_name)}",
                "Origin": "https://steamcommunity.com",
            }

            # Цена в копейках/центах Steam (целое число)
            price_total_cents = int(round(total_cost * 100))
            payload = {
                "sessionid": self.session_id,
                "currency": self.currency_code,
                "appid": 730,
                "market_hash_name": item_name,
                "price_total": str(price_total_cents),
                "quantity": str(quantity),
                "billing_state": "",
                "save_my_address": "0",
            }

            with httpx.Client(timeout=20.0, cookies=cookies) as client:
                resp = client.post(self.CREATE_ORDER_URL, data=payload, headers=headers)
                resp_json = resp.json()

                if resp_json.get("success") == 1:
                    real_order_id = str(resp_json.get("buy_orderid"))
                    self.available_balance -= total_cost
                    order_data = {
                        "order_id": real_order_id,
                        "item_name": item_name,
                        "price": target_price,
                        "quantity": quantity,
                        "total_cost": total_cost,
                        "breakeven_price": breakeven_price,
                        "expected_target": expected_target,
                        "status": "ACTIVE",
                        "mode": "LIVE_STEAM",
                        "created_at": datetime.now(),
                    }
                    self.active_orders.append(order_data)
                    return {
                        "success": True,
                        "order_id": real_order_id,
                        "mode": "LIVE_STEAM",
                        "item_name": item_name,
                        "price": target_price,
                        "quantity": quantity,
                        "total_cost": total_cost,
                    }
                else:
                    return {
                        "success": False,
                        "error": resp_json.get("message", f"Ошибка Steam API: {resp_json}"),
                        "item_name": item_name,
                    }

        except Exception as e:
            return {
                "success": False,
                "error": f"Сетевая ошибка при отправке ордера: {e}",
                "item_name": item_name,
            }

    def check_simulated_fills(self, current_market_prices: dict[str, float]) -> list[dict]:
        """
        Проверка исполнения активных ордеров в симуляции:
        Если текущая рыночная цена или пролив коснулся цены нашего Buy-ордера,
        ордер признается исполненным (FILLED), фиксируя виртуальный закуп.
        """
        fills = []
        still_active = []

        for ord_info in self.active_orders:
            item = ord_info["item_name"]
            cur_price = current_market_prices.get(item)
            if cur_price is not None and cur_price <= ord_info["price"] * 1.02:
                # Ордер сработал на паническом проливе
                ord_info["status"] = "FILLED"
                ord_info["filled_at"] = datetime.now()
                # Рассчитываем чистую прибыль при возврате к целевой цене
                target = ord_info["expected_target"]
                net_revenue = (target * (1.0 - STEAM_FEE_PCT)) * ord_info["quantity"]
                profit = net_revenue - ord_info["total_cost"]
                ord_info["projected_net_profit"] = round(profit, 2)
                self.total_profit_realized += profit
                self.filled_orders.append(ord_info)
                fills.append(ord_info)
            else:
                still_active.append(ord_info)

        self.active_orders = still_active
        return fills

    def get_portfolio_status(self) -> dict:
        """
        Возвращает агрегированный статус портфеля автобота.
        """
        reserved = sum(o["total_cost"] for o in self.active_orders)
        return {
            "initial_balance": self.initial_balance,
            "available_cash": round(self.available_balance, 2),
            "reserved_in_orders": round(reserved, 2),
            "active_orders_count": len(self.active_orders),
            "filled_orders_count": len(self.filled_orders),
            "total_profit_realized": round(self.total_profit_realized, 2),
            "dry_run": self.dry_run,
        }
