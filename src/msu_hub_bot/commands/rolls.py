import asyncio
import random
import re
import string
from collections import defaultdict
from typing import Final, Tuple

from aiogram import html
from aiogram.enums import DiceEmoji
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold, hcode
from aiogram.utils.formatting import Code, Text

from msu_hub_bot.telegram.callbacks import CallbackCommandBase
from msu_hub_bot.telegram.command_api import Argument, MetaCommand
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.utils import one_liner, outdated, parse_int, percent_chance


def get_roll(digits: int) -> Tuple[str, str]:
    def roll_level(text: str) -> int:
        level = 1

        if not text:
            return level

        last = text[-1]
        for c in text[-2::-1]:
            if c == last:
                level += 1
            else:
                break

        return level

    roll_names = {
        1: "",
        2: "дабл",
        3: "трипл",
        4: "квадрипл",
        5: "пентипл",
        6: "секстипл",
        7: "септипл",
        8: "oктипл",
    }

    roll = str(random.randrange(0, 10**digits)).zfill(digits)
    name = roll_names.get(roll_level(roll), "нихуясе")

    return roll, name


@MetaCommand("roll", "ролл", digits=Argument(clamp=(1, 100)))
async def process_roll(digits: int = 3) -> Text:
    roll, name = get_roll(digits)
    return Text(Code(roll), f" — {name}" if name else "")


async def process_random(message: Message, meta: MetaInfo) -> Message | bool:
    args = meta.arguments
    if len(args) > 1:
        n1 = min(max(int(args[0]), 0), 10**100) if args[0].isdigit() else 0
        n2 = min(max(int(args[1]), 0), 10**100) if args[1].isdigit() else 100
        begin, end = min(n1, n2), max(n1, n2)
    else:
        begin = 0
        end = min(max(int(args[0]), 0), 10 * 100) if len(args) > 0 and args[0].isdigit() else 100

    number = random.randint(begin, end)
    return await message.reply(hcode(number))


class RandomCallback(CallbackData, prefix="random"):
    begin: int
    end: int
    count: int


class Randoms(CallbackCommandBase):
    callback_data = RandomCallback

    @classmethod
    def keyboard(cls, begin: int, end: int, count: int) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder().row(
            InlineKeyboardButton(text="RANDOM 🎲", callback_data=RandomCallback(begin=begin, end=end, count=count).pack()),
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    async def process(cls, message: Message, meta: MetaInfo) -> Message | bool:
        args = meta.arguments
        if len(args) > 1:
            n1 = min(max(int(args[0]), 1), 10**10) if args[0].isdigit() else 1
            n2 = min(max(int(args[1]), 1), 10**10) if args[1].isdigit() else 100
            count = min(max(int(args[2]), 1), 3) if len(args) > 2 and args[2].isdigit() else 1
        else:
            n1 = 1
            n2 = min(max(int(args[0]), 1), 10 * 10) if len(args) > 0 and args[0].isdigit() else 100
            count = 1

        begin, end = min(n1, n2), max(n1, n2)
        target = message.reply_to_message if message.reply_to_message else message
        return await target.reply(hbold("Машина рандома") + f": числа с {begin} по {end}", reply_markup=cls.keyboard(begin, end, count))

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: RandomCallback) -> Message | bool:
        message = query.message
        if not isinstance(message, Message):
            return await query.answer("Эта кнопка уже недоступна.")

        if outdated(message.date + cls.cache_time_long_td):
            await query.answer(text="♻️ Этот генератор слишком стар, он закрывается", show_alert=True)
            return await message.edit_reply_markup(reply_markup=None)

        begin, end, count = callback_data.begin, callback_data.end, callback_data.count
        result = [str(random.randint(begin, end)) for _ in range(count)]
        await query.answer(text=f"🎲 Выпало: {', '.join(result)}", cache_time=cls.cache_time_long)

        text_part = f"\n— {query.from_user.mention_html()}: {', '.join(hcode(r) for r in result)}"
        text = await cls.cached_text(message, text_part)

        lock = cls.lock(cls.cache_key(message))

        if lock.locked():
            return True

        async with lock:
            await asyncio.sleep(1.0)
            return await message.edit_text(text, reply_markup=cls.keyboard(begin, end, count))


class RollCallback(CallbackData, prefix="roll"):
    digits: int
    count: int


class Rolls(CallbackCommandBase):
    callback_data = RollCallback

    @classmethod
    def keyboard(cls, digits: int, count: int) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder().row(
            InlineKeyboardButton(text="ROLL 🎲", callback_data=RollCallback(digits=digits, count=count).pack()),
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    async def process(cls, message: Message, meta: MetaInfo) -> Message | bool:
        args = meta.arguments
        digits = min(max(int(args[0]), 1), 10) if len(args) > 0 and args[0].isdigit() else 3
        count = min(max(int(args[1]), 1), 3) if len(args) > 1 and args[1].isdigit() else 1

        target = message.reply_to_message if message.reply_to_message else message
        return await target.reply(hbold("Машина рандома") + f": числа длины {digits}", reply_markup=cls.keyboard(digits, count))

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: RollCallback) -> Message | bool:
        roll_emojis: Final = ("😎", "🤪", "😏", "😯", "😮", "😲", "🤤", "🤠", "👽")

        message = query.message
        if not isinstance(message, Message):
            return await query.answer("Эта кнопка уже недоступна.")

        if outdated(message.date + cls.cache_time_long_td):
            await query.answer(text="♻️ Этот генератор слишком стар, он закрывается", show_alert=True)
            return await message.edit_reply_markup(reply_markup=None)

        digits, count = callback_data.digits, callback_data.count

        rolls = []
        for _ in range(count):
            roll, name = get_roll(digits)
            if name:
                text = f"{random.choice(roll_emojis)} {hbold(name.title())} у {query.from_user.mention_html()}: {hcode(roll)}"
                target = message.reply_to_message if message.reply_to_message else message
                await target.reply(text)
            rolls.append(roll)

        await query.answer(text=f"🎲 Выпало: {', '.join(rolls)}", cache_time=cls.cache_time_long)

        text_part = f"\n— {query.from_user.mention_html()}: {', '.join(hcode(r) for r in rolls)}"
        text = await cls.cached_text(message, text_part)

        lock = cls.lock(cls.cache_key(message))

        if lock.locked():
            return True

        async with lock:
            await asyncio.sleep(1.0)
            return await message.edit_text(text, reply_markup=cls.keyboard(digits, count))


async def process_truth(_message: Message, meta: MetaInfo) -> Message | bool:
    answers: Final = ("да", "нет", "это не важно", "да, хотя зря", "никогда", "100%", "1 из 100")

    target = meta.reply_target()
    return await target.reply(random.choice(answers))


pattern_or = re.compile(r"\b(?:или)|(?:or)\b", re.IGNORECASE)


async def process_or(_message: Message, meta: MetaInfo) -> Message | bool:
    target, text = meta.extract_text()
    if not text:
        return True

    choices = []
    for c in pattern_or.split(one_liner(text)):
        if c := c.strip(string.whitespace + "?"):
            choices.append(c)

    if len(choices) < 2:
        return True

    text = random.choice(choices)
    return await target.reply(html.quote(text))


async def process_mash(_message: Message, meta: MetaInfo) -> Message | bool:
    target, text = meta.extract_text()
    if not text:
        return True

    def mash(t: str) -> str:
        x = list(t)
        random.shuffle(x)
        return "".join(x)

    def repl(match: re.Match[str]) -> str:
        return match.group(1) + mash(match.group(2)) + match.group(3)

    result = re.sub(r"\b(\w)(\w+)(\w)\b", repl, text, flags=re.MULTILINE)
    return await target.reply(html.quote(result))


async def process_d6(message: Message, meta: MetaInfo) -> Message | bool:
    d6: Final = tuple(enumerate(("⚀", "⚁", "⚂", "⚃", "⚄", "⚅"), start=1))

    args = meta.arguments
    count = (parse_int(args[0], 2, 1, 10) or 2) if args else 2

    result = random.choices(d6, k=count)
    dices_sum = sum(r[0] for r in result)
    dices = " ".join(r[1] for r in result)

    return await message.reply(text=f"{dices} | {dices_sum} ({count * len(d6)})")


async def process_dice(message: Message) -> Message | bool:
    dices = (DiceEmoji.DICE, DiceEmoji.DART, DiceEmoji.BASKETBALL, DiceEmoji.FOOTBALL, DiceEmoji.SLOT_MACHINE)
    return await message.reply_dice(emoji=random.choice(dices))


async def process_others_dice(message: Message) -> Message | bool:
    dices_max: Final[defaultdict[str, tuple[int, ...]]] = defaultdict(
        lambda: (6,), {DiceEmoji.BASKETBALL: (5,), DiceEmoji.FOOTBALL: (5,), DiceEmoji.SLOT_MACHINE: (1, 22, 43, 64)}
    )
    dices_success: Final[defaultdict[str, tuple[int, ...]]] = defaultdict(
        lambda: (6,), {DiceEmoji.BASKETBALL: (4, 5, 6), DiceEmoji.FOOTBALL: (3, 4, 5, 6), DiceEmoji.SLOT_MACHINE: (1, 22, 43, 64)}
    )

    joy_emojis: Final = ("😇", "😎", "🤩", "🥳", "😏", "☺️", "🙂", "🙃", "💪🏻", "✨")
    sad_emojis: Final = ("🤨", "😒", "😞", "😔", "😟", "😕", "🙁", "☹️", "😣", "😫", "😩", "😬")
    parts_1: Final = ("молодец", "круто", "неплохо", "недурно", "класс", "ништяк", "чертяка", "чётко")
    parts_1_special: Final[defaultdict[str, tuple[str, ...]]] = defaultdict(
        tuple,
        {
            DiceEmoji.DART: ("в яблочко",),
            DiceEmoji.BASKETBALL: ("трёхочковый",),
            DiceEmoji.FOOTBALL: ("гол!",),
            DiceEmoji.SLOT_MACHINE: ("ты и есть однорукий бандит",),
        },
    )
    parts_2: Final = ("👍🏻", "😮", "😀", "👍🏿", "😯", "😧", "😋", "😊", "☺️", "🙂", "🙃", "💪🏻", "✊🏻", "👀", "🔥", "⭐️", "✨")

    if message.forward_origin is not None:
        return True

    d = message.dice
    if d is None or d.emoji == DiceEmoji.DICE:
        return True

    if d.value not in dices_max[d.emoji]:
        return True

    part_1, part_2 = random.choice(parts_1 + parts_1_special[d.emoji]), random.choice(parts_2)
    await asyncio.sleep(3.0)
    await message.reply(f"{part_1.capitalize()} {part_2}")

    if percent_chance(10.0):
        await message.answer("Я тоже так могу:")
        msg = await message.answer_dice(emoji=d.emoji)
        if msg.dice is None:
            return True
        reactions = joy_emojis if msg.dice.value in dices_success[msg.dice.emoji] else sad_emojis
        await asyncio.sleep(3.0)
        await msg.reply("".join(random.sample(reactions, k=3)))

    return True
