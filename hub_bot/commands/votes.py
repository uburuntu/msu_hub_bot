from aiogram.types import Message, InputPollOption, MessageOriginUser, MessageOriginChat, MessageOriginChannel, MessageOriginHiddenUser

from common.tg.filters import MetaInfo
from common.utils import one_liner, shorten


async def process_votes(_message: Message, meta: MetaInfo) -> Message | bool | None:
    target, text = meta.extract_text()
    args = meta.arguments

    if not text and not args:
        return True

    if text:
        origin = target.forward_origin
        if isinstance(origin, MessageOriginUser):
            name = origin.sender_user.full_name
        elif isinstance(origin, MessageOriginChat):
            name = origin.sender_chat.full_name
        elif isinstance(origin, MessageOriginChannel):
            name = origin.chat.full_name
        elif isinstance(origin, MessageOriginHiddenUser):
            name = origin.sender_user_name
        else:
            name = target.from_user.full_name if target.from_user else target.chat.full_name

        text = f'{name}: {one_liner(text)}'

    else:
        text = 'Голосование'

    options = ['👍🏻', '👎🏻']
    if args:
        options = [a[:100] for a in args][:10]

    return await target.reply_poll(
        question=shorten(text, width=140, placeholder=' [...] '),
        options=[InputPollOption(text=option) for option in options + ['🤔']],
        is_anonymous=False,
    )
