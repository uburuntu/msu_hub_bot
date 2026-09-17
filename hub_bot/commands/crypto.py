from contextlib import suppress
from typing import Tuple, List

from aiogram.exceptions import TelegramBadRequest
import ccxt.async_support as ccxt
import pendulum
from common.caching import cached_async
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold, hitalic

from common.tg.callbacks import CallbackCommandBase
from common.tg.filters import MetaInfo
from common.tg.runtime import gather_complete


class CryptoCallback(CallbackData, prefix="crypto", sep=":"):
    ticker: str


class Crypto(CallbackCommandBase):
    callback_data = CryptoCallback
    tickers = ("BTC", "ETH", "XRP", "BNB", "DOGE")

    @classmethod
    def keyboard(cls, tickers: List[str]) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder()
        for ticker in tickers:
            keyboard.add(InlineKeyboardButton(text=ticker, callback_data=CryptoCallback(ticker=ticker).pack()))
        keyboard.adjust(3)
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    @cached_async(ttl=10, noself=True)
    async def text(cls, ticker: str, crypto_exchange: ccxt.Exchange) -> str:
        text = f"⚖️ {hbold(ticker)} with 24h and 7d diffs\n\n"

        async def prices(e: ccxt.Exchange, symbol: str) -> Tuple[int, int, int]:
            diff_1d = (pendulum.now("UTC") - pendulum.duration(days=1)).int_timestamp * 1000
            diff_7d = (pendulum.now("UTC") - pendulum.duration(days=7)).int_timestamp * 1000
            curr, *prev = await gather_complete(
                e.fetch_ohlcv(symbol, timeframe="1m", limit=1),
                e.fetch_ohlcv(symbol, timeframe="1m", limit=1, since=diff_1d),
                e.fetch_ohlcv(symbol, timeframe="1m", limit=1, since=diff_7d),
            )
            return curr[0][4], prev[0][0][4], prev[1][0][4]

        def line(p: Tuple[int, int, int], symbol: str) -> str:
            curr_p, *prev_ps = p
            percent_1 = hbold(f"{abs(1.0 - (curr_p / prev_ps[0])) * 100:.3f}")
            change_1 = f"📈 +{percent_1}%" if curr_p >= prev_ps[0] else f"📉 -{percent_1}%"
            percent_2 = hbold(f"{abs(1.0 - (curr_p / prev_ps[1])) * 100:.3f}")
            change_2 = f"📈 +{percent_2}%" if curr_p >= prev_ps[1] else f"📉 -{percent_2}%"
            return f"— {symbol} {hbold(f'{curr_p:.8f}'.rstrip('0'))} | {change_1} | {change_2}\n"

        if ticker == "BTC":
            usd = await prices(crypto_exchange, f"{ticker}/USDT")
            text += line(usd, "$")
        else:
            try:
                usd, btc = await gather_complete(
                    prices(crypto_exchange, f"{ticker}/USDT"),
                    prices(crypto_exchange, f"{ticker}/BTC"),
                )
                text += line(usd, "$")
                text += line(btc, "₿")
            except ccxt.BadSymbol:
                usd = await prices(crypto_exchange, f"{ticker}/USDT")
                text += line(usd, "$")

        return text

    @classmethod
    async def check_tickers(cls, tickers: List[str], crypto_exchange: ccxt.Exchange) -> List[str]:
        markets = await crypto_exchange.load_markets()
        symbols = {f"{t}/USDT": True for t in tickers}
        return [s.partition("/")[0] for s in symbols if markets.get(s, {}).get("active", False)][:42]

    @classmethod
    async def process(cls, message: Message, meta: MetaInfo, crypto_exchange: ccxt.Exchange) -> Message | bool | None:
        command = meta.keyword.lstrip("/#")
        target, text = meta.extract_text()
        tickers = text and [t.upper() for t in text.split()] or [command.upper()]
        if tickers == ["CRYPTO"]:
            tickers = list(cls.tickers)

        reply = await message.reply(hitalic("🔄 Updating tickers..."))

        checked_tickers = await cls.check_tickers(tickers, crypto_exchange)
        if not checked_tickers:
            return await reply.edit_text(f"🤷🏻‍♂️ Unable to find ticker(s): {', '.join(hbold(t) for t in tickers)}")

        text = await cls.text(checked_tickers[0], crypto_exchange)
        return await reply.edit_text(text, reply_markup=cls.keyboard(checked_tickers))

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: CryptoCallback, crypto_exchange: ccxt.Exchange) -> Message | bool | None:
        message = query.message
        if not isinstance(message, Message) or not message.reply_markup:
            return await query.answer("Эта кнопка уже недоступна.")
        await query.answer("✅ Updating", cache_time=5)

        tickers = [button.text for line in message.reply_markup.inline_keyboard for button in line]

        text = await cls.text(callback_data.ticker, crypto_exchange)
        if message.html_text != text:
            with suppress(TelegramBadRequest):
                return await message.edit_text(text, reply_markup=cls.keyboard(tickers))

        return True
