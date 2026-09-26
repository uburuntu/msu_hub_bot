"""Handled refusals and dependency failures retain accurate, content-free outcomes."""

import asyncio
import socket
from types import SimpleNamespace

import aiohttp
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendPhoto, UnpinChatMessage
from teleforge.cards import CardRefreshError
from teleforge.delivery import DeliveryError, DeliveryProgress
from teleforge.outcome import InvocationOutcome

from msu_hub_bot.telegram.errors import telegram_error_reason
from msu_hub_bot.telemetry import Boundary, Outcome, Telemetry, failure_outcome, record_handled_failure, safe_failure
from telemetry_helpers import Capture, config


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("timeout", [False, True])
def test_presentation_wrappers_preserve_typed_failure_and_delivery_facts(nested, timeout):
    cause = (
        TimeoutError("private")
        if timeout
        else TelegramForbiddenError(method=SendPhoto(chat_id=-123, photo="private-photo"), message="Forbidden: bot was blocked by the user")
    )
    delivery = DeliveryError(DeliveryProgress(attempted_part=0, total_parts=1, phase="failed", uncertain=timeout), cause)
    error = delivery
    if nested:
        error = CardRefreshError("board", outcome=InvocationOutcome(handler_returned=True))
        error.__cause__ = delivery
    assert failure_outcome(error) is (Outcome.TIMEOUT if timeout else Outcome.REJECTED)
    assert safe_failure(error)["error.reason"] == ("timeout" if timeout else "bot_blocked")
    assert "private" not in str(safe_failure(error))
    assert delivery.cause is cause and delivery.uncertain is timeout
    if nested:
        assert error.teleforge_outcome.handler_returned


def test_unrelated_exception_chains_do_not_change_failure_classification():
    error = RuntimeError("application defect")
    error.__cause__ = TimeoutError()
    assert failure_outcome(error) is Outcome.UNEXPECTED


def values(item):
    return {attribute.key: getattr(attribute.value, attribute.value.WhichOneof("value")) for attribute in item.attributes}


@pytest.mark.parametrize(
    ("description", "reason"),
    [
        ("Not enough rights to manage pinned messages in the chat", "not_enough_rights"),
        ("message to unpin not found", "message_not_found"),
        ("Failed to get HTTP URL content", "media_fetch_failed"),
        ("Wrong type of the web page content", "media_content_invalid"),
        ("Wrong HTTP URL specified", "media_url_invalid"),
        ("PHOTO_INVALID_DIMENSIONS", "photo_dimensions_invalid"),
        ("IMAGE_PROCESS_FAILED", "image_processing_failed"),
        ("wrong file identifier/HTTP URL specified: private-source", "media_reference_invalid"),
        ("message is not modified: private-content", "message_not_modified"),
    ],
)
def test_telegram_recovery_and_telemetry_use_the_same_fixed_reason(description, reason):
    error = TelegramBadRequest(method=UnpinChatMessage(chat_id=-123, message_id=7), message="Bad Request: " + description)
    assert telegram_error_reason(error) == reason
    failure = safe_failure(error)
    assert failure["error.reason"] == reason
    assert failure["http.response.status_code"] == 400
    assert "private" not in str(failure)


def test_unknown_telegram_error_is_not_a_recovery_instruction():
    error = TelegramBadRequest(method=UnpinChatMessage(chat_id=-123), message="private-unknown-detail")
    assert telegram_error_reason(error) is None
    assert safe_failure(error)["error.reason"] == "bad_request"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (aiohttp.ClientConnectorDNSError(SimpleNamespace(), socket.gaierror(-2, "private-host")), "dns_error"),
        (aiohttp.ClientConnectorError(SimpleNamespace(), OSError("private-host")), "connect_error"),
        (aiohttp.ServerDisconnectedError("private-response"), "connection_lost"),
        (aiohttp.ClientResponseError(None, (), status=502, message="private-response"), "http_error"),
    ],
)
def test_network_failures_have_fixed_reasons_without_response_or_host(error, reason):
    failure = safe_failure(error)
    assert failure["error.reason"] == reason
    assert failure_outcome(error) is Outcome.UNAVAILABLE
    assert "private" not in str(failure)


def test_guarded_media_sizes_do_not_hide_other_worker_errors():
    from msu_hub_bot.media.ffmpeg import ReverseSizeError
    from msu_hub_bot.media.sticker_media import StickerMediaError, StickerSizeError

    for error in (StickerSizeError("private"), ReverseSizeError("private")):
        assert failure_outcome(error) is Outcome.REJECTED
        assert safe_failure(error)["error.reason"] == "media_too_large"
    assert failure_outcome(StickerMediaError("Missing FFmpeg")) is Outcome.UNEXPECTED
    assert failure_outcome(ValueError("An actual bug")) is Outcome.UNEXPECTED


async def test_error_reply_does_not_turn_failed_game_publication_into_success(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), {"game.start"}, transport=capture)
    await telemetry.start()
    try:
        with telemetry.operation(Boundary.HANDLER, "game.start"):
            try:
                raise TelegramBadRequest(
                    method=SendPhoto(chat_id=-123, photo="private-photo"), message="Bad Request: PHOTO_INVALID_DIMENSIONS"
                )
            except TelegramBadRequest as error:
                record_handled_failure(error)
            with telemetry.operation(Boundary.TELEGRAM, "telegram.request", telegram_method="sendMessage"):
                pass
    finally:
        await telemetry.close()
    handler = next(span for span in capture.spans() if span.name == "bot.handler")
    assert values(handler)["outcome"] == "rejected"
    assert values(handler)["error.reason"] == "photo_dimensions_invalid"
    logs = [log for log in capture.logs() if log.body.string_value == "bot.operation.failed"]
    assert len(logs) == 1
    assert values(logs[0])["operation"] == "game.start"
    assert "private-photo" not in capture.serialized()


async def test_handled_failure_cannot_change_another_tasks_operation(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), {"game.start"}, transport=capture)

    async def unrelated():
        record_handled_failure(RuntimeError("private"))

    await telemetry.start()
    try:
        with telemetry.operation(Boundary.HANDLER, "game.start"):
            await asyncio.create_task(unrelated())
    finally:
        await telemetry.close()
    handler = next(span for span in capture.spans() if span.name == "bot.handler")
    assert values(handler)["outcome"] == "success"
    assert "private" not in capture.serialized()


@pytest.mark.parametrize(("error", "outcome"), [(RuntimeError("private"), "unexpected"), (asyncio.CancelledError(), "cancelled")])
async def test_later_failure_supersedes_a_handled_refusal(monkeypatch, error, outcome):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), {"game.start"}, transport=capture)
    await telemetry.start()
    try:
        with pytest.raises(type(error)):
            with telemetry.operation(Boundary.HANDLER, "game.start"):
                record_handled_failure(
                    TelegramBadRequest(method=SendPhoto(chat_id=-123, photo="private"), message="PHOTO_INVALID_DIMENSIONS")
                )
                raise error
    finally:
        await telemetry.close()
    handler = next(span for span in capture.spans() if span.name == "bot.handler")
    assert values(handler)["outcome"] == outcome
    assert values(handler)["error.reason"] == outcome
    assert "http.response.status_code" not in values(handler)
    assert "private" not in capture.serialized()


def test_storage_outages_are_distinct_from_storage_protocol_defects():
    from msu_hub_bot.storage.errors import RepositoryFailure, RepositoryProtocolError, RepositoryUnavailable

    unavailable = RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
    timeout = RepositoryUnavailable(RepositoryFailure.TIMEOUT)
    assert failure_outcome(unavailable) is Outcome.UNAVAILABLE
    assert safe_failure(unavailable)["error.reason"] == "storage_unavailable"
    assert failure_outcome(timeout) is Outcome.TIMEOUT
    assert safe_failure(timeout)["error.reason"] == "timeout"
    assert failure_outcome(RepositoryProtocolError()) is Outcome.UNEXPECTED
