"""Private review entry points and bounded administrator notifications."""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from aiogram.filters import CommandObject

from msu_hub_bot.commands.app import is_app_start, process_app_start
from msu_hub_bot.feedback.models import FeedbackMessage, FeedbackReport, SelectedFeedbackContext
from msu_hub_bot.feedback.presentation import notification_method, render_report, text_size
from msu_hub_bot.web.links import WebAppLinks, feedback_report_id
from telegram_helpers import make_bot, make_message

REPORT_ID = "0123456789abcdef"
NOW = datetime(2030, 1, 1, tzinfo=UTC)


def test_notification_keeps_selected_context_private_and_fits_telegram():
    report = FeedbackReport(
        report_id=REPORT_ID,
        author_id=42,
        author_name="😀" * 128,
        created_at=NOW,
        description="😀" * 2000,
        destination_chat_id=-456,
        ui_digest="a" * 64,
        context=SelectedFeedbackContext(
            reply=FeedbackMessage(
                chat_id=-123,
                message_id=7,
                sent_at=NOW,
                author_name="private-context-author",
                text="private-context-body",
            )
        ),
    )
    report = report.model_copy(update={"rendered_text": render_report(report)})
    links = WebAppLinks("synthetic", "https://app.example")
    links.username = "test_bot"
    method = notification_method(report, button=links.feedback_button(REPORT_ID))
    assert method.chat_id == -456
    assert text_size(method.text) <= 4096
    assert REPORT_ID in method.text and "ID 42" in method.text
    assert "private-context" not in method.text
    assert "private-context-body" in report.rendered_text
    assert method.parse_mode is None and method.link_preview_options.is_disabled
    assert method.reply_markup.inline_keyboard[0][0].web_app is None
    assert method.reply_markup.inline_keyboard[0][0].url == f"https://t.me/test_bot?start=feedback_{REPORT_ID}"
    assert notification_method(report).reply_markup is None


@pytest.mark.parametrize("argument", [None, "feedback_", "feedback_abc", "feedback_" + REPORT_ID + "/other", "feedback_" + "F" * 16])
def test_feedback_links_reject_malformed_report_ids(argument):
    assert feedback_report_id(argument) is None
    message = make_message(chat={"id": 42, "type": "private"})
    assert not is_app_start(message, CommandObject(command="start", args=argument))


def test_feedback_link_availability_and_private_routing():
    links = WebAppLinks("synthetic", "https://app.example")
    assert links.feedback_button(REPORT_ID) is None
    links.username = "test_bot"
    assert links.feedback_button(REPORT_ID) is not None
    assert WebAppLinks("synthetic", "").feedback_button(REPORT_ID) is None
    command = CommandObject(command="start", args="feedback_" + REPORT_ID)
    assert is_app_start(make_message(chat={"id": 42, "type": "private"}), command)
    assert not is_app_start(make_message(), command)
    assert is_app_start(make_message(chat={"id": 42, "type": "private"}), CommandObject(command="start", args="app_launch"))
    for method in (links.feedback_button, links.private_feedback_button):
        with pytest.raises(ValueError):
            method("bad")


@pytest.mark.parametrize("authorized", [False, True])
async def test_feedback_start_requires_reviewer_and_only_routes_to_a_private_webapp(authorized):
    bot = make_bot()
    try:
        feedback = Mock()
        feedback.is_reviewer.return_value = authorized
        message = make_message(bot, chat={"id": 42, "type": "private"})
        await process_app_start(
            message,
            CommandObject(command="start", args="feedback_" + REPORT_ID),
            WebAppLinks("synthetic", "https://app.example"),
            feedback,
        )
        sent = bot.session.methods[-1]
        feedback.is_reviewer.assert_called_once_with(42)
        if authorized:
            assert sent.reply_markup.inline_keyboard[0][0].web_app.url == f"https://app.example/?feedback={REPORT_ID}"
        else:
            assert sent.reply_markup is None
            assert "владельцу" in sent.text
        assert len(bot.session.methods) == 1
    finally:
        await bot.session.close()


@pytest.mark.parametrize("fields", [{}, {"from_user": {"id": 42, "is_bot": True, "first_name": "Bot"}}])
async def test_feedback_start_does_not_offer_webapp_to_groups_or_bot_users(fields):
    bot = make_bot()
    try:
        feedback = Mock()
        feedback.is_reviewer.return_value = True
        message = make_message(bot, **fields)
        await process_app_start(
            message,
            CommandObject(command="start", args="feedback_" + REPORT_ID),
            WebAppLinks("synthetic", "https://app.example"),
            feedback,
        )
        assert bot.session.methods[-1].reply_markup is None
        feedback.is_reviewer.assert_not_called()
    finally:
        await bot.session.close()
