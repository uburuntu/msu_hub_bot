import random
from itertools import islice

from aiogram.types import Message
from aiogram import html

from common.tg.filters import MetaInfo


class Zalgo:
    num_accents_up = (1, 5)
    num_accents_down = (1, 5)
    num_accents_middle = (1, 4)
    max_accents = 6

    du = (
        ' ̍', ' ̎', ' ̄', ' ̅', ' ̿', ' ̑', ' ̆', ' ̐', ' ͒', ' ͗', ' ͑', ' ̇', ' ̈', ' ̊', ' ͂', ' ̓', ' ̈́', ' ͊', ' ͋', ' ͌', ' ̃',
        ' ̂', ' ̌', ' ͐', ' ́', ' ̋', ' ̏', ' ̽', ' ̉', ' ͣ', ' ͤ', ' ͥ', ' ͦ', ' ͧ', ' ͨ', ' ͩ', ' ͪ', ' ͫ', ' ͬ', ' ͭ', ' ͮ', ' ͯ',
        ' ̾', ' ͛', ' ͆', ' ̚',
    )
    dm = (
        ' ̕', ' ̛', ' ̀', ' ́', ' ͘', ' ̡', ' ̢', ' ̧', ' ̨', ' ̴', ' ̵', ' ̶', ' ͜', ' ͝', ' ͞', ' ͟', ' ͠', ' ͢', ' ̸', ' ̷', ' ͡',
    )
    dd = (
        '̖', ' ̗', ' ̘', ' ̙', ' ̜', ' ̝', ' ̞', ' ̟', ' ̠', ' ̤', ' ̥', ' ̦', ' ̩', ' ̪', ' ̫', ' ̬', ' ̭', ' ̮', ' ̯', ' ̰', ' ̱', ' ̲',
        ' ̳', ' ̹', ' ̺', ' ̻', ' ̼', ' ͅ', ' ͇', ' ͈', ' ͉', ' ͍', ' ͎', ' ͓', ' ͔', ' ͕', ' ͖', ' ͙', ' ͚', ' ',
    )

    @classmethod
    def zalgofy(cls, text: str) -> str:
        ri = random.randint
        result = []

        for a in text:
            if a.isalnum():
                u, m, d = ri(*cls.num_accents_up), ri(*cls.num_accents_middle), ri(*cls.num_accents_down)
                cases = [0] * u + [1] * m + [2] * d
                random.shuffle(cases)

                for case in islice(cases, cls.max_accents):
                    diacritics = cls.du if case == 0 else (cls.dm if case == 1 else cls.dd)
                    a += random.choice(diacritics).strip()

            result.append(a)

        return ''.join(result)


async def process_zalgo(_message: Message, meta: MetaInfo) -> Message | bool | None:
    target, text = meta.extract_text()
    if not text:
        return True
    return await target.reply(html.quote(Zalgo.zalgofy(text))[:4096])
