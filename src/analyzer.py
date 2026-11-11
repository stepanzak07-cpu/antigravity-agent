"""
Модуль аналитики и ИИ-детекции рынка (Market Intelligence & Regime Detection).
Выполняет:
1. Агрегацию потока сделок в дневные метрики (OHLCV, медианы, процентили).
2. Определение фазы рынка (падающий/медвежий, растущий, боковик/консолидация).
3. Расчет скользящих средних, момента (momentum) и степени перепроданности.
4. Выявление аномалий и дисконтов для закупки.
"""

from dataclasses import dataclass
import pandas as pd
import numpy as np


@dataclass
class MarketRegimeReport:
    regime: str  # 'BEARISH_PANIC', 'BEARISH_GRIND', 'BULLISH', 'SIDEWAYS'
    trend_description: str
    overall_market_change_pct: float
    avg_volatility_pct: float
    items_analyzed: int
    most_stable_category: str
    panic_discount_avg_pct: float
    recommended_strategy_name: str


class CS2MarketAnalyzer:
    def __init__(self, trades_df: pd.DataFrame):
        self.trades = trades_df.copy()
        self._prepare_data()

    def _prepare_data(self):
        """Подготовка временных меток и агрегация"""
        self.trades["date"] = pd.to_datetime(self.trades["timestamp"]).dt.date
        
        # Дневная агрегация по каждому предмету
        daily_stats = self.trades.groupby(["market_hash_name", "category", "date"]).agg(
            open_price=("price", "first"),
            high_price=("price", "max"),
            low_price=("price", "min"),
            close_price=("price", "last"),
            median_price=("price", "median"),
            q10_price=("price", lambda x: np.percentile(x, 10)),  # Уровень панических сливов / buy-ордеров
            q90_price=("price", lambda x: np.percentile(x, 90)),
            volume=("price", "count"),
        ).reset_index()

        daily_stats.sort_values(by=["market_hash_name", "date"], inplace=True)
        self.daily_stats = daily_stats

    def analyze_market_regime(self) -> MarketRegimeReport:
        """
        ИИ-детекция макро-тренда рынка на основе всех проанализированных сделок.
        Определяет, находится ли рынок скинов в фазе падения.
        """
        # Считаем сводный индекс рынка (среднее взвешенное изменение цен)
        changes = []
        volatilities = []
        panic_discounts = []
        category_performance = {}

        for item_name, item_df in self.daily_stats.groupby("market_hash_name"):
            if len(item_df) < 5:
                continue
            
            first_median = item_df["median_price"].iloc[0]
            last_median = item_df["median_price"].iloc[-1]
            pct_change = ((last_median - first_median) / first_median) * 100
            changes.append(pct_change)
            
            # Дневная волатильность
            pct_daily_vols = (item_df["high_price"] - item_df["low_price"]) / item_df["median_price"] * 100
            volatilities.append(pct_daily_vols.mean())
            
            # Средний дисконт между медианой и 10-м процентилем (глубина слива по buy-ордерам)
            item_discounts = (item_df["median_price"] - item_df["q10_price"]) / item_df["median_price"] * 100
            panic_discounts.append(item_discounts.mean())

            cat = item_df["category"].iloc[0]
            category_performance.setdefault(cat, []).append(pct_change)

        avg_market_change = float(np.mean(changes)) if changes else 0.0
        avg_vol = float(np.mean(volatilities)) if volatilities else 0.0
        avg_panic_discount = float(np.mean(panic_discounts)) if panic_discounts else 0.0

        # Определяем наиболее устойчивую категорию
        cat_averages = {k: np.mean(v) for k, v in category_performance.items()}
        best_category = max(cat_averages, key=cat_averages.get) if cat_averages else "case"

        # Классификация режима рынка
        if avg_market_change <= -15.0:
            if avg_vol > 12.0:
                regime = "BEARISH_PANIC"
                desc = "Резкий медвежий обвал (панические распродажи, сливы в buy-ордера)"
                strategy = "DEEP_BUY_ORDERS"  # Расстановка глубоких ордеров на выкуп паники
            else:
                regime = "BEARISH_GRIND"
                desc = "Умеренно падающий рынок (медленное сползание цен, фаза истощения продавцов)"
                strategy = "STAGGERED_DCA"    # Ступенчатое усреднение в ликвидных расходниках
        elif avg_market_change >= 10.0:
            regime = "BULLISH"
            desc = "Растущий рынок (хайп, рост объемов, обновление максимумов)"
            strategy = "MOMENTUM_BREAKOUT"
        else:
            regime = "SIDEWAYS"
            desc = "Боковик / Консолидация цен в узком диапазоне"
            strategy = "MEAN_REVERSION"

        return MarketRegimeReport(
            regime=regime,
            trend_description=desc,
            overall_market_change_pct=round(avg_market_change, 2),
            avg_volatility_pct=round(avg_vol, 2),
            items_analyzed=len(changes),
            most_stable_category=best_category,
            panic_discount_avg_pct=round(avg_panic_discount, 2),
            recommended_strategy_name=strategy,
        )

    def get_item_summary(self) -> pd.DataFrame:
        """Сводная таблица по всем предметам с ключевыми показателями"""
        records = []
        for item_name, item_df in self.daily_stats.groupby("market_hash_name"):
            first_p = item_df["median_price"].iloc[0]
            last_p = item_df["median_price"].iloc[-1]
            min_p = item_df["low_price"].min()
            max_p = item_df["high_price"].max()
            current_discount_from_ath = ((max_p - last_p) / max_p) * 100
            
            # Средняя цена 10% самых дешевых сделок (идеальный вход по buy-ордерам)
            target_buy_order_price = item_df["q10_price"].tail(7).median()
            
            records.append({
                "Предмет": item_name,
                "Категория": item_df["category"].iloc[0],
                "Старт ($)": round(first_p, 2),
                "Текущая ($)": round(last_p, 2),
                "Изменение (%)": round(((last_p - first_p) / first_p) * 100, 1),
                "Просадка от пика (%)": round(current_discount_from_ath, 1),
                "Целевой Buy-Order ($)": round(target_buy_order_price, 2),
                "Сделок всего": item_df["volume"].sum(),
            })

        return pd.DataFrame(records).sort_values(by="Изменение (%)", ascending=True)
