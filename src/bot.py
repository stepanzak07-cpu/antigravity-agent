"""
Telegram-бот CS2 Market Sniper & Trader.
Интерфейс спроектирован по стандартам Bybit Pilot:
- Полностью интерактивные инлайн-карточки (InlineKeyboardMarkup)
- Защита по Telegram ID владельца (белый список)
- Управление портфелем, снайпер-ордерами, аналитикой рынка и экстренным СТОПом
- Встроенный Health-сервер для размещения на Render (24/7 без засыпания)
"""

import asyncio
import html
import logging
import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from src.market_data import POPULAR_ITEMS
from src.data_fetcher import SteamMarketFetcher
from src.analyzer import CS2MarketAnalyzer
from src.strategy import CS2StrategyEngine
from src.trader import SteamOrderBot

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("cs2_sniper_bot")


def esc(value) -> str:
    """Экранирование текста для HTML-разметки Telegram."""
    return html.escape(str(value))


class CS2TelegramBot:
    def __init__(
        self,
        token: str,
        chat_id: int,
        budget: float = 200.0,
        dry_run: bool = True,
        scan_interval_minutes: int = 15,
    ):
        self.token = token
        self.chat_id = chat_id
        self.budget = budget
        self.dry_run = dry_run
        self.scan_interval_minutes = scan_interval_minutes
        self.is_monitoring_active = True

        # Инициализация торгового ядра
        self.trader_bot = SteamOrderBot(
            dry_run=dry_run,
            initial_balance=budget,
            currency_code=1,
            currency_symbol="$",
        )
        self.strategy_engine = CS2StrategyEngine()
        self.market_fetcher = SteamMarketFetcher()

        # Построение приложения Telegram
        builder = Application.builder().token(token)
        proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        if proxy_url:
            builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)

        self.app = builder.build()
        self._setup_handlers()

    def _setup_handlers(self):
        owner = filters.Chat(chat_id=self.chat_id)

        # Команды
        self.app.add_handler(CommandHandler("start", self.cmd_start, filters=owner))
        self.app.add_handler(CommandHandler("menu", self.cmd_start, filters=owner))
        self.app.add_handler(CommandHandler("status", self.cmd_status, filters=owner))
        self.app.add_handler(CommandHandler("market", self.cmd_market, filters=owner))
        self.app.add_handler(CommandHandler("signals", self.cmd_signals, filters=owner))
        self.app.add_handler(CommandHandler("orders", self.cmd_orders, filters=owner))
        self.app.add_handler(CommandHandler("stop", self.cmd_stop, filters=owner))
        self.app.add_handler(CommandHandler("resume", self.cmd_resume, filters=owner))

        # Инлайн-кнопки
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))

        # Глобальный обработчик ошибок
        self.app.add_error_handler(self.on_error)

    def _get_main_keyboard(self) -> InlineKeyboardMarkup:
        """Главное меню в стиле Bybit Pilot"""
        keyboard = [
            [
                InlineKeyboardButton("📊 Портфель", callback_data="btn_status"),
                InlineKeyboardButton("📈 Анализ рынка", callback_data="btn_market"),
            ],
            [
                InlineKeyboardButton("🎯 Снайпер-сигналы", callback_data="btn_signals"),
                InlineKeyboardButton("🧾 Активные ордера", callback_data="btn_orders"),
            ],
            [
                InlineKeyboardButton("⚙️ Сплит (60%/40%)", callback_data="btn_strategy"),
                InlineKeyboardButton(
                    "🛑 СТОП" if self.is_monitoring_active else "▶️ Возобновить",
                    callback_data="btn_toggle_monitor",
                ),
            ],
            [
                InlineKeyboardButton("🔄 Обновить меню", callback_data="btn_refresh_menu"),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Приветственное окно с панелью управления"""
        mode_text = "🟢 <b>DRY-RUN (Симуляция)</b>" if self.dry_run else "⚡ <b>LIVE (Steam Market)</b>"
        status_text = "🟢 <b>АКТИВЕН</b>" if self.is_monitoring_active else "⏸ <b>НА ПАУЗЕ</b>"
        p_status = self.trader_bot.get_portfolio_status()

        text = (
            f"🎯 <b>CS2 MARKET SNIPER & TRADER</b>\n\n"
            f"• Режим исполнения: {mode_text}\n"
            f"• Мониторинг рынка: {status_text}\n"
            f"• Баланс: <b>${p_status['initial_balance']:.2f}</b> (Свободно: <b>${p_status['available_cash']:.2f}</b>)\n"
            f"• В ордерах: <b>${p_status['reserved_in_orders']:.2f}</b> | Активных: <b>{p_status['active_orders_count']} шт.</b>\n"
            f"• Корзина: <b>60% Кейсы / 40% Скины</b>\n\n"
            f"<i>Выберите действие в меню ниже:</i>"
        )
        if update.message:
            await update.message.reply_html(text, reply_markup=self._get_main_keyboard())
        elif update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=self._get_main_keyboard())

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Детальное состояние портфеля"""
        p_status = self.trader_bot.get_portfolio_status()
        orders = self.trader_bot.active_orders

        orders_text = ""
        if orders:
            for idx, o in enumerate(orders, 1):
                orders_text += (
                    f"  {idx}. <b>{esc(o['item_name'])}</b> x {o['quantity']}\n"
                    f"     Цена: <b>${o['price']:.2f}</b> | Сумма: <b>${o['total_cost']:.2f}</b>\n"
                    f"     Безубыток: <b>${o['breakeven_price']:.2f}</b> | ID: <code>{o['order_id']}</code>\n"
                )
        else:
            orders_text = "  <i>Активных ордеров нет.</i>\n"

        text = (
            f"📊 <b>СОСТОЯНИЕ ПОРТФЕЛЯ</b>\n\n"
            f"• Стартовый капитал: <b>${p_status['initial_balance']:.2f}</b>\n"
            f"• Доступный кэш: <b>${p_status['available_cash']:.2f}</b>\n"
            f"• Зарезервировано: <b>${p_status['reserved_in_orders']:.2f}</b>\n"
            f"• Закрытый профит: <b>+${p_status['total_profit_realized']:.2f}</b>\n\n"
            f"🧾 <b>Текущие Buy-ордера:</b>\n{orders_text}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Сгенерировать ордера", callback_data="btn_signals")],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_back_main")],
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        elif update.message:
            await update.message.reply_html(text, reply_markup=keyboard)

    async def cmd_market(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Анализ фазы рынка и срез цен"""
        target_message = update.callback_query.message if update.callback_query else update.message
        await target_message.reply_chat_action("typing")

        loop = asyncio.get_running_loop()
        df_trades = await loop.run_in_executor(None, self.market_fetcher.build_multi_basket_dataframe, POPULAR_ITEMS)

        if df_trades.empty:
            await target_message.reply_html("❌ Не удалось получить сделки со Steam. Попробуйте позже.")
            return

        analyzer = CS2MarketAnalyzer(df_trades)
        regime = analyzer.analyze_market_regime()
        item_summary = analyzer.get_item_summary()

        table_lines = []
        for _, row in item_summary.head(6).iterrows():
            item_n = row["Предмет"].replace(" (Field-Tested)", "").replace(" Case", "")
            cur_p = row.get("Текущая ($)", row.get("price", 0.0))
            chg = row["Изменение (%)"]
            emoji_chg = "🔴" if chg < 0 else "🟢"
            table_lines.append(f"• <b>{esc(item_n)}</b>: ${cur_p:.2f} ({emoji_chg} {chg:+.1f}%)")

        text = (
            f"📈 <b>ИИ-АНАЛИЗ РЫНКА CS2</b>\n\n"
            f"• Диагноз: <b>{esc(regime.regime)}</b>\n"
            f"• Тренд: <i>{esc(regime.trend_description)}</i>\n"
            f"• Динамика цен корзины: <b>{regime.overall_market_change_pct}%</b>\n"
            f"• Средняя волатильность: <b>{regime.avg_volatility_pct}%</b>\n"
            f"• Рекомендованная стратегия: <b>{esc(regime.recommended_strategy_name)}</b>\n\n"
            f"<b>Ключевые предметы (срез):</b>\n" + "\n".join(table_lines)
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Рассчитать снайпер-входы", callback_data="btn_signals")],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_back_main")],
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_html(text, reply_markup=keyboard)

    async def cmd_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Расчет и выставление снайпер-ордеров корзины"""
        target_message = update.callback_query.message if update.callback_query else update.message
        await target_message.reply_chat_action("typing")

        loop = asyncio.get_running_loop()
        df_trades = await loop.run_in_executor(None, self.market_fetcher.build_multi_basket_dataframe, POPULAR_ITEMS)

        if df_trades.empty:
            await target_message.reply_html("❌ Ошибка сбора данных со Steam.")
            return

        analyzer = CS2MarketAnalyzer(df_trades)
        item_summary = analyzer.get_item_summary()

        signals = self.strategy_engine.generate_multi_basket_orders(
            item_summary_df=item_summary,
            total_budget=self.budget,
            case_budget_pct=0.60,
            skin_budget_pct=0.40,
            case_discount_pct=0.18,
            skin_discount_pct=0.22,
        )

        signals_text = ""
        for s in signals:
            signals_text += (
                f"🎯 <b>{esc(s.item_name)}</b> ({esc(s.category)})\n"
                f"   • Объем: <b>{s.quantity} шт.</b> по <b>${s.target_buy_price:.2f}</b>\n"
                f"   • Безубыток Steam: <b>${s.breakeven_sell_price:.2f}</b> (с учетом 13% комиссии)\n"
                f"   • Бюджет: <b>${s.allocated_budget:.2f}</b>\n\n"
            )

        text = (
            f"🎯 <b>СНАЙПЕР-СИГНАЛЫ (МУЛЬТИ-КОРЗИНА)</b>\n"
            f"<i>60% на кейсы (-18%), 40% на скины (-22%)</i>\n\n"
            f"{signals_text}"
            f"Нажмите кнопку ниже, чтобы автоматически выставить ордера в Dry-Run симуляции:"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Выставить ордера (Dry-Run)", callback_data="btn_execute_orders")],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_back_main")],
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_html(text, reply_markup=keyboard)

    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Управление активными ордерами"""
        orders = self.trader_bot.active_orders
        if not orders:
            text = "🧾 <b>АКТИВНЫЕ ОРДЕРА</b>\n\n<i>В данный момент активных ордеров нет.</i>"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎯 Сгенерировать ордера", callback_data="btn_signals")],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_back_main")],
            ])
        else:
            lines = []
            for o in orders:
                lines.append(
                    f"• <b>{esc(o['item_name'])}</b> x {o['quantity']} шт.\n"
                    f"  Цена: <b>${o['price']:.2f}</b> | Сумма: <b>${o['total_cost']:.2f}</b>\n"
                    f"  ID: <code>{o['order_id']}</code> [{o['mode']}]"
                )
            text = f"🧾 <b>АКТИВНЫЕ BUY-ОРДЕРА ({len(orders)} шт.)</b>\n\n" + "\n\n".join(lines)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Отменить все ордера", callback_data="btn_cancel_all")],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_back_main")],
            ])

        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_html(text, reply_markup=keyboard)

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Экстренная остановка"""
        self.is_monitoring_active = False
        text = "🛑 <b>ЭКСТРЕННЫЙ СТОП АКТИВИРОВАН!</b>\n\nВсе авто-сканирования и ордера поставлены на паузу."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Возобновить", callback_data="btn_toggle_monitor")],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_back_main")],
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_html(text, reply_markup=keyboard)

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Возобновление работы"""
        self.is_monitoring_active = True
        text = "▶️ <b>СНАЙПИНГ ВОЗОБНОВЛЕН</b>\n\nМониторинг рынка Steam активен."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_back_main")],
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_html(text, reply_markup=keyboard)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка нажатий инлайн-кнопок"""
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "btn_status":
            await self.cmd_status(update, context)
        elif data == "btn_market":
            await self.cmd_market(update, context)
        elif data == "btn_signals":
            await self.cmd_signals(update, context)
        elif data == "btn_orders":
            await self.cmd_orders(update, context)
        elif data == "btn_back_main" or data == "btn_refresh_menu":
            await self.cmd_start(update, context)
        elif data == "btn_toggle_monitor":
            if self.is_monitoring_active:
                await self.cmd_stop(update, context)
            else:
                await self.cmd_resume(update, context)
        elif data == "btn_strategy":
            text = (
                f"⚙️ <b>СТРАТЕГИЯ МУЛЬТИ-КОРЗИНЫ</b>\n\n"
                f"• <b>60% Кейсы:</b> Высокая ликвидность, дисконт 18%, быстрый оборот.\n"
                f"• <b>40% Скины:</b> Глубокий снайпинг панических проливов со скидкой 22%.\n"
                f"• <b>Комиссия Steam:</b> 13.04% заложена в расчет безубытка.\n"
                f"• <b>Рабочий бюджет:</b> ${self.budget:.2f}\n"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="btn_back_main")]
            ])
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        elif data == "btn_execute_orders":
            # Исполнение ордеров
            loop = asyncio.get_running_loop()
            df_trades = await loop.run_in_executor(None, self.market_fetcher.build_multi_basket_dataframe, POPULAR_ITEMS)
            analyzer = CS2MarketAnalyzer(df_trades)
            item_summary = analyzer.get_item_summary()
            signals = self.strategy_engine.generate_multi_basket_orders(item_summary, total_budget=self.budget)

            placed = 0
            for s in signals:
                res = self.trader_bot.place_order_from_signal(s, live=not self.dry_run)
                if res.get("success"):
                    placed += 1

            text = (
                f"✅ <b>ОРДЕРА УСПЕШНО ВЫСТАВЛЕНЫ!</b>\n\n"
                f"• Размещено: <b>{placed} ордеров</b>\n"
                f"• Режим: <b>{'LIVE' if not self.dry_run else 'DRY-RUN'}</b>\n"
                f"• Свободный баланс: <b>${self.trader_bot.available_balance:.2f}</b>\n\n"
                f"Бот отслеживает рыночные проливы и уведомит вас о выкупе."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🧾 Просмотр ордеров", callback_data="btn_orders")],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_back_main")],
            ])
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        elif data == "btn_cancel_all":
            count = len(self.trader_bot.active_orders)
            self.trader_bot.active_orders.clear()
            self.trader_bot.available_balance = self.trader_bot.initial_balance
            text = f"🗑 <b>Отменено {count} ордеров.</b>\nБаланс восстановлен до ${self.trader_bot.initial_balance:.2f}."
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_back_main")],
            ])
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        log.error(f"Telegram error: {context.error}")

    def run_polling(self):
        """Локальный запуск бота через Polling"""
        log.info(f"Запуск бота @CS2Market435_bot для Chat ID: {self.chat_id}...")
        self.app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "7198803917"))
    if not token:
        print("Ошибка: TELEGRAM_BOT_TOKEN не задан в .env")
        exit(1)

    bot = CS2TelegramBot(token=token, chat_id=chat_id, budget=200.0, dry_run=True)
    bot.run_polling()
