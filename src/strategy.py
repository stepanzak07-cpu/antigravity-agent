"""
Модуль торговых стратегий для рынка скинов CS2.
Включает:
1. DeepBuyOrdersStrategy — расстановка лимитных ордеров на выкуп паники со скидкой 15-30%.
2. StaggeredDCAStrategy — ступенчатый закуп расходников (кейсов) на просадках.
3. Моделирование комиссии Steam (13.04%) и порога безубыточности (Break-Even).
"""

from dataclasses import dataclass
import pandas as pd


# Реальная комиссия торговой площадки Steam:
# Steam удерживает 5% комиссии Steam + 10% комиссии игры CS2 (в сумме ~15% от цены покупателя,
# что равно 13.04% вычета из суммы, которую вводит продавец).
STEAM_FEE_PCT = 0.1304
STEAM_BUYER_MARKUP = 0.1500  # Сколько платит покупатель поверх цены продавца


@dataclass
class TradeSignal:
    item_name: str
    category: str
    action: str  # 'PLACE_BUY_ORDER', 'WAIT', 'TAKE_PROFIT'
    target_buy_price: float
    breakeven_sell_price: float
    expected_profit_target: float
    quantity: int
    allocated_budget: float
    rationale: str


class CS2StrategyEngine:
    def __init__(self, commission_rate: float = STEAM_FEE_PCT):
        self.commission = commission_rate

    def calculate_breakeven_price(self, buy_price: float) -> float:
        """Минимальная цена продажи в Steam, чтобы выйти в 0 после комиссии"""
        # Если покупатель платит P_sell, продавец получает P_sell * (1 - commission)
        # P_sell * (1 - 0.1304) = buy_price  =>  P_sell = buy_price / (1 - 0.1304)
        return round(buy_price / (1.0 - self.commission), 2)

    def generate_multi_basket_orders(
        self,
        item_summary_df: pd.DataFrame,
        total_budget: float = 200.0,
        case_budget_pct: float = 0.60,
        skin_budget_pct: float = 0.40,
        case_discount_pct: float = 0.18,
        skin_discount_pct: float = 0.22,
        currency_symbol: str = "$",
    ) -> list[TradeSignal]:
        """
        Формирует сбалансированную корзину Buy-ордеров:
        - 60% бюджета на ликвидные расходники (кейсы, капсулы): быстрый оборот, минимальный риск.
        - 40% бюджета на проверенные скины: увеличенный спред паники.
        """
        signals = []
        cases_budget = total_budget * case_budget_pct
        skins_budget = total_budget * skin_budget_pct

        # Разделяем датафрейм на кейсы/капсулы и скины
        # Колонки могут быть либо на русском ('Категория'), либо на английском ('category')
        cat_col = "Категория" if "Категория" in item_summary_df.columns else "category"
        name_col = "Предмет" if "Предмет" in item_summary_df.columns else "market_hash_name"
        price_col = [c for c in item_summary_df.columns if "Текущая" in c or "price" in c.lower() or "current" in c.lower()][0]

        cases_df = item_summary_df[item_summary_df[cat_col].isin(["case", "sticker_capsule"])].copy()
        skins_df = item_summary_df[~item_summary_df[cat_col].isin(["case", "sticker_capsule"])].copy()

        # 1. Распределение бюджета по кейсам (топ-3 кейса)
        if not cases_df.empty:
            num_cases = min(3, len(cases_df))
            budget_per_case = cases_budget / max(1, num_cases)
            for _, row in cases_df.head(num_cases).iterrows():
                item_name = row[name_col]
                cur_price = float(row[price_col])
                buy_price = round(max(0.03, cur_price * (1.0 - case_discount_pct)), 2)
                breakeven = self.calculate_breakeven_price(buy_price)
                qty = max(1, int(budget_per_case // buy_price))
                cost = round(buy_price * qty, 2)
                net_profit_per_unit = (cur_price * (1.0 - self.commission)) - buy_price
                roi_pct = (net_profit_per_unit / buy_price) * 100 if buy_price > 0 else 0

                signals.append(TradeSignal(
                    item_name=item_name,
                    category="Расходник (Кейс)",
                    action="PLACE_BUY_ORDER",
                    target_buy_price=buy_price,
                    breakeven_sell_price=breakeven,
                    expected_profit_target=cur_price,
                    quantity=qty,
                    allocated_budget=cost,
                    rationale=(
                        f"Скидка {int(case_discount_pct * 100)}% от текущей {currency_symbol}{cur_price:.2f}. "
                        f"Кол-во: {qty} шт. Безубыток: {currency_symbol}{breakeven:.2f}. "
                        f"Ожидаемый чистый профит: +{roi_pct:.1f}% за вычетом 13% комиссии Steam."
                    )
                ))

        # 2. Распределение бюджета по скинам (топ-2 скина)
        if not skins_df.empty:
            num_skins = min(2, len(skins_df))
            budget_per_skin = skins_budget / max(1, num_skins)
            for _, row in skins_df.head(num_skins).iterrows():
                item_name = row[name_col]
                cur_price = float(row[price_col])
                buy_price = round(max(0.03, cur_price * (1.0 - skin_discount_pct)), 2)
                breakeven = self.calculate_breakeven_price(buy_price)
                qty = max(1, int(budget_per_skin // buy_price))
                cost = round(buy_price * qty, 2)
                net_profit_per_unit = (cur_price * (1.0 - self.commission)) - buy_price
                roi_pct = (net_profit_per_unit / buy_price) * 100 if buy_price > 0 else 0

                signals.append(TradeSignal(
                    item_name=item_name,
                    category="Скин (Ликвидный)",
                    action="PLACE_BUY_ORDER",
                    target_buy_price=buy_price,
                    breakeven_sell_price=breakeven,
                    expected_profit_target=cur_price,
                    quantity=qty,
                    allocated_budget=cost,
                    rationale=(
                        f"Глубокий снайпинг со скидкой {int(skin_discount_pct * 100)}% (текущая {currency_symbol}{cur_price:.2f}). "
                        f"Кол-во: {qty} шт. Безубыток: {currency_symbol}{breakeven:.2f}. "
                        f"Ожидаемый ROI: +{roi_pct:.1f}%."
                    )
                ))

        return signals

    def generate_falling_market_orders(
        self,
        item_summary_df: pd.DataFrame,
        budget_usd: float = 200.0,
        max_items: int = 5,
        target_discount_pct: float = 0.20,  # Закупка на 20% ниже рынка
    ) -> list[TradeSignal]:
        """
        Генерация плана закупки на падающем рынке.
        Правила отбора:
        1. Приоритет отдается ликвидным расходникам (кейсы, капсулы), так как они гарантированно
           восстанавливаются после вымывания предложения.
        2. Buy-ордера выставляются с глубоким дисконтом (18-25%), чтобы перекрыть комиссию 13%
           и зафиксировать чистую прибыль при отскоке к обычной цене.
        """
        signals = []
        allocated_per_item = budget_usd / max_items

        # Сортируем: сначала кейсы и расходники, затем скины с наибольшим объемом сделок
        priority_df = item_summary_df.sort_values(
            by=["Категория", "Сделок всего"],
            ascending=[True, False]
        )

        for _, row in priority_df.head(max_items).iterrows():
            item_name = row["Предмет"]
            current_price = row["Текущая ($)"]
            suggested_buy_order = round(current_price * (1.0 - target_discount_pct), 2)
            
            # Защита от минимального порога цены в Steam ($0.03)
            suggested_buy_order = max(0.03, suggested_buy_order)
            
            breakeven = self.calculate_breakeven_price(suggested_buy_order)
            # Цель фиксации прибыли: возврат к медианной текущей цене
            net_profit_at_current = (current_price * (1.0 - self.commission)) - suggested_buy_order
            roi_pct = (net_profit_at_current / suggested_buy_order) * 100 if suggested_buy_order > 0 else 0

            signals.append(TradeSignal(
                item_name=item_name,
                category=row.get("Категория", "skin"),
                action="PLACE_BUY_ORDER",
                target_buy_price=suggested_buy_order,
                breakeven_sell_price=breakeven,
                expected_profit_target=current_price,
                quantity=max(1, int(allocated_per_item // suggested_buy_order)),
                allocated_budget=round(suggested_buy_order * max(1, int(allocated_per_item // suggested_buy_order)), 2),
                rationale=(
                    f"Текущая цена ${current_price:.2f}. "
                    f"Buy-ордер со скидкой {int(target_discount_pct * 100)}% (${suggested_buy_order:.2f}). "
                    f"Безубыток: ${breakeven:.2f}. "
                    f"Чистый профит при отскоке: +{roi_pct:.1f}% (уже после вычета 13.04% комиссии Steam)."
                )
            ))

        return signals
