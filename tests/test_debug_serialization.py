"""Diagnostics preserve received Telegram fields without framework defaults."""

import html
import json

import pytest
from aiogram.methods import SendDocument, SendMessage

from msu_hub_bot.commands import control, debug
from telegram_helpers import make_bot, make_message


@pytest.mark.parametrize("echo", [False, True])
@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize(
    "preview", [{"is_disabled": True}, {"is_disabled": False, "show_above_text": False}, {"url": "https://example.org"}]
)
async def test_diagnostics_and_echo_keep_partial_link_preview_options(echo, large, preview):
    bot = make_bot()
    source = make_message(
        bot,
        message_id=123456789,
        date=1700000000,
        chat={"id": -1001234567890, "type": "supergroup"},
        from_user={"id": 9876543210, "is_bot": False, "first_name": "Synthetic"},
        text="https://example.org " + ("🙂" * 3000 if large else "hello"),
        link_preview_options=preview,
    )
    try:
        if echo:
            await control.process_echo(source)
        else:
            await debug.process_json(make_message(bot, text="/json", reply_to_message=source))
        sent = bot.session.methods[0]
        if large:
            assert isinstance(sent, SendDocument)
            payload = json.loads(sent.document.data)
        else:
            assert isinstance(sent, SendMessage)
            payload = json.loads(html.unescape(sent.text.removeprefix("<pre>").removesuffix("</pre>")))
        assert payload["link_preview_options"] == preview
        assert payload["date"] == 1700000000
        assert payload["message_id"] == 123456789
        assert payload["chat"]["id"] == -1001234567890
        assert payload["from"]["id"] == 9876543210 and payload["from"]["is_bot"] is False
        assert "from_user" not in payload
        assert payload["text"] == source.text
        assert len(bot.session.methods) == (2 if echo else 1)
        if echo:
            assert bot.session.methods[1].text == source.text
    finally:
        await bot.session.close()
