from msu_hub_bot.settings import settings

from aiogram.types import Message
from aiogram.utils.markdown import hbold, hcode, hitalic
from airtable import Airtable

error_stickers = f"""{hbold("Типы ошибок со стикерами")}:

— вы не начали чат с ботом; в этом случае начните с ним личную беседу (@msu_hub_bot) и повторите чуть позже

— вы недавно удаляли созданный ботом стикерпак; надо подождать пока сервера Telegram прогрузят эту информацию и повторить позже

— количество стикеров не может быть больше 120 в обычном паке, и больше 50 в анимированном паке; вы можете удалить ненужные стикеры через @Stickers или создать новый пак, привязав его к какому-нибудь чату

— в один пак нельзя добавлять анимации разного FPS: только 30 или только 60

{hbold("Что может показаться ошибкой")}:

— бот прислал вам свежий стикер, но в паке он не отобразился; это известная задержка, связанная с кешем стикеров на клиентах Telegram — вы можете открыть Telegram на другом устройстве или просто подождать
"""


async def process_error_stickers(message: Message) -> Message:
    return await message.reply(error_stickers, disable_web_page_preview=True)


donate = f"""👋🏻 {hbold("Привет")}! Я — @rm_bk — создатель этого бота.

В разработку бота вложено много {hbold("времени")}, {hbold("любви")} и, в том числе, {hbold("денег")}.

Если у вас есть {hbold("желание")} и {hbold("возможность")} поддержать его развитие, то буду рад помощи в оплате счетов на облачные ресурсы:
— через Tinkoff: https://www.tinkoff.ru/sl/AnB1Ci01XCD  
— через Revolut: https://revolut.me/rmbk
— как-то еще

По желанию ник, имя или сайт будут упомянуты в списке /supporters 🎈
"""


async def process_donate(message: Message) -> Message:
    return await message.reply(donate, disable_web_page_preview=True)


async def process_supporters(message: Message) -> Message:
    raw = Airtable(
        settings.require("supporters_base"), settings.require("supporters_table"), settings.require("supporters_api_key")
    ).get_all()
    supporters = {s["fields"]["Name"]: s["fields"].get("Details") for s in sorted(raw, key=lambda x: x["fields"]["ID"])}
    anonymous = supporters.pop("Поддержавшие анонимно", 0)
    supporters_list = "— " + "\n— ".join(f"{hitalic(name)}" + (f" | {details}" if details else "") for name, details in supporters.items())

    text = f"🎖 {hbold('Поддержавшие развитие бота')}:\n\n{supporters_list}\n\n🏅 {hbold('Поддержавшие анонимно')}: {hcode(anonymous)}"
    return await message.reply(text, disable_web_page_preview=True)
