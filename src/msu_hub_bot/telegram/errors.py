"""Fixed Telegram error reasons shared by recovery policy and diagnostics."""

from aiogram.exceptions import TelegramAPIError
from teleforge.cards import CardRefreshError
from teleforge.delivery import DeliveryError


def failure_cause(error: BaseException) -> BaseException:
    """Classify known presentation wrappers without discarding their delivery facts."""
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        cause = error.cause if isinstance(error, DeliveryError) else error.__cause__ if isinstance(error, CardRefreshError) else None
        if cause is None:
            break
        error = cause
    return error


_REASONS = {
    "chat not found": "chat_not_found",
    "private chat not found": "chat_not_found",
    "the group chat was deleted": "chat_deleted",
    "message to edit not found": "message_not_found",
    "message to delete not found": "message_not_found",
    "message to unpin not found": "message_not_found",
    "message can't be edited": "message_not_editable",
    "message can't be deleted": "message_not_deletable",
    "bot was blocked by the user": "bot_blocked",
    "bot was kicked from the supergroup chat": "bot_removed",
    "bot was kicked from the group chat": "bot_removed",
    "bot was kicked from the channel chat": "bot_removed",
    "bot is not a member of the group chat": "bot_removed",
    "bot is not a member of the supergroup chat": "bot_removed",
    "bot is not a member of the channel chat": "bot_removed",
    "user is deactivated": "user_deactivated",
    "have no rights to send a message": "not_enough_rights",
    "not enough rights to send text messages to the chat": "not_enough_rights",
    "not enough rights to send photos to the chat": "not_enough_rights",
    "not enough rights to manage pinned messages in the chat": "not_enough_rights",
    "query is too old and response timeout expired or query id is invalid": "query_expired",
    "message caption is too long": "caption_too_long",
    "message is too long": "text_too_long",
    "wrong http url specified": "media_url_invalid",
    "wrong type of the web page content": "media_content_invalid",
    "failed to get http url content": "media_fetch_failed",
    "photo_invalid_dimensions": "photo_dimensions_invalid",
    "image_process_failed": "image_processing_failed",
}


def telegram_error_reason(error: TelegramAPIError) -> str | None:
    """Match known descriptions without retaining request values or unknown text."""
    message = error.message.casefold().removeprefix("bad request: ").removeprefix("forbidden: ")
    if reason := _REASONS.get(message):
        return reason
    if message == "message is not modified" or message.startswith("message is not modified: "):
        return "message_not_modified"
    if message.startswith("can't parse entities"):
        return "invalid_entities"
    if message.startswith("wrong file identifier/http url specified"):
        return "media_reference_invalid"
    return None
