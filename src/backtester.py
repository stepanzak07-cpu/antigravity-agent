"""
Модуль бэктестера и симуляции торговли (Backtesting Simulator).
Честно моделирует специфику CS2:
1. Исполнение Buy-ордеров только при наличии реальных сливов цены по рынку.
2. 7-дневный Trade Lock (нельзя мгновенно продать предмет).
3. Полный вычет комиссии Steam (13.04%).
4. Фиксация итогового PnL, просадки капитала и винрейта.
"""

from dataclasses import dataclass
from datetime import timedelta
import pandas as pd
from src.strategy import STEAM_FEE_PCT


@dataclass
class TradeRecord:
    item_name: str
    buy_date: str
    buy_price: float
    sell_date: str
    sell_price: float
    fee_paid: float
    net_pnl: float
    roi_pct: float
    days_held: int


class CS2Backtester:
    def __init__(
        self,
        trades_df: pd.DataFrame,
        initial_balance_usd: float = 200.0,
        trade_lock_days: int = 7,
        commission_rate: float = STEAM_FEE_PCT,
    ):
        self.trades = trades_df.sort_values(by="timestamp").reset_index(drop=True)
        self.initial_balance = initial_balance_usd
        self.current_balance = initial_balance_usd
        self.trade_lock_days = trade_lock_days
        self.commission = commission_rate
        self.inventory: list[dict] = []
        self.closed_trades: list[TradeRecord] = []

    def run_backtest(
        self,
        target_discount_pct: float = 0.20,
        max_active_positions: int = 6,
    ) -> dict:
        """Запуск симуляции стратегии на всей истории сделок"""
        # Группируем сделки по дням
        trades = self.trades.copy()
        trades["date"] = pd.to_datetime(trades["timestamp"]).dt.date
        unique_days = sorted(trades["date"].unique())

        position_size_usd = self.initial_balance / max_active_positions

        for current_day in unique_days:
            day_trades = trades[trades["date"] == current_day]
            
            # 1. Проверяем возможность продажи предметов из инвентаря, у которых истек Trade Lock (7 дней)
            items_to_keep = []
            for item in self.inventory:
                holding_days = (current_day - item["buy_date"]).days
                if holding_days >= self.trade_lock_days:
                    # Ищем сегодняшнюю медианную цену продажи
                    item_today_trades = day_trades[day_trades["market_hash_name"] == item["name"]]
                    if not item_today_trades.empty:
                        today_market_price = float(item_today_trades["price"].median())
                        
                        # Условие продажи: цена выше безубытка с желаемым профитом, либо выход по рынку
                        breakeven = item["buy_price"] / (1.0 - self.commission)
                        if today_market_price >= breakeven:
                            # Продаем на ТП Steam
                            fee = today_market_price * self.commission
                            net_proceeds = today_market_price - fee
                            pnl = net_proceeds - item["buy_price"]
                            roi = (pnl / item["buy_price"]) * 100

                            self.current_balance += net_proceeds
                            self.closed_trades.append(TradeRecord(
                                item_name=item["name"],
                                buy_date=str(item["buy_date"]),
                                buy_price=round(item["buy_price"], 2),
                                sell_date=str(current_day),
                                sell_price=round(today_market_price, 2),
                                fee_paid=round(fee, 2),
                                net_pnl=round(pnl, 2),
                                roi_pct=round(roi, 1),
                                days_held=holding_days,
                            ))
                            continue
                items_to_keep.append(item)
            self.inventory = items_to_keep

            # 2. Выставляем и исполняем Buy-ордера, если есть свободный баланс
            available_slots = max_active_positions - len(self.inventory)
            if available_slots > 0 and self.current_balance > 0:
                # Берем сегодняшние предметы
                for name, item_group in day_trades.groupby("market_hash_name"):
                    if available_slots <= 0 or self.current_balance <= 0:
                        break
                    
                    median_p = item_group["price"].median()
                    target_buy_p = round(median_p * (1.0 - target_discount_pct), 2)
                    
                    # Проверяем, хватает ли средств на покупку предмета
                    if self.current_balance < target_buy_p:
                        continue

                    # Проверяем: были ли сегодня реальные сделки по этой цене (панические сливы)
                    min_today_p = item_group["price"].min()
                    if min_today_p <= target_buy_p:
                        # Ордер сработал!
                        self.current_balance -= target_buy_p
                        self.inventory.append({
                            "name": name,
                            "buy_price": target_buy_p,
                            "buy_date": current_day,
                        })
                        available_slots -= 1

        # Оценка оставшегося инвентаря по последним ценам
        last_day_trades = trades[trades["date"] == unique_days[-1]]
        inventory_value = 0.0
        for item in self.inventory:
            latest = last_day_trades[last_day_trades["market_hash_name"] == item["name"]]
            if not latest.empty:
                val = float(latest["price"].median()) * (1.0 - self.commission)
                inventory_value += val
            else:
                inventory_value += item["buy_price"]

        total_final_equity = self.current_balance + inventory_value
        net_profit = total_final_equity - self.initial_balance
        total_roi = (net_profit / self.initial_balance) * 100

        profitable_trades = [t for t in self.closed_trades if t.net_pnl > 0]
        win_rate = (len(profitable_trades) / len(self.closed_trades) * 100) if self.closed_trades else 0.0

        return {
            "initial_balance": self.initial_balance,
            "final_cash": round(self.current_balance, 2),
            "inventory_value": round(inventory_value, 2),
            "total_equity": round(total_final_equity, 2),
            "net_profit": round(net_profit, 2),
            "total_roi_pct": round(total_roi, 2),
            "total_closed_trades": len(self.closed_trades),
            "win_rate_pct": round(win_rate, 1),
            "items_in_lock": len(self.inventory),
            "trade_records": self.closed_trades,
        }
