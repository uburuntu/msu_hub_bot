"""Image fallbacks must not make selected media depend on unrelated API calls."""

from unittest.mock import AsyncMock

import pytest
from aiogram.types import UserProfilePhotos

from common.tg.utils import extract_image
from telegram_helpers import make_bot, make_message

PHOTO = {"file_id": "selected-photo", "file_unique_id": "unique", "width": 100, "height": 100}


@pytest.mark.parametrize("reply_photo", [False, True])
async def test_selected_image_never_requests_profile_photos(monkeypatch, reply_photo):
    bot = make_bot()
    profile = AsyncMock(side_effect=RuntimeError("Profile photos unavailable"))
    monkeypatch.setattr(bot, "get_user_profile_photos", profile)
    image_message = make_message(bot, message_id=2, photo=[PHOTO])
    message = make_message(bot, text="/filter", reply_to_message=image_message) if reply_photo else image_message

    target, media = await extract_image(message, with_profile_photo=True)

    assert target == image_message
    assert media.file_id == "selected-photo"
    profile.assert_not_awaited()


@pytest.mark.parametrize("reply_has_photo", [False, True])
async def test_profile_fallback_preserves_reply_then_author_order(monkeypatch, reply_has_photo):
    bot = make_bot()
    available = UserProfilePhotos(total_count=1, photos=[[PHOTO]])
    empty = UserProfilePhotos(total_count=0, photos=[])
    responses = [available] if reply_has_photo else [empty, available]
    profile = AsyncMock(side_effect=responses)
    monkeypatch.setattr(bot, "get_user_profile_photos", profile)
    reply = make_message(bot, message_id=2, from_user={"id": 43, "is_bot": False, "first_name": "Reply author"})
    message = make_message(bot, text="/filter", reply_to_message=reply)

    target, media = await extract_image(message, with_profile_photo=True)

    assert target == (reply if reply_has_photo else message)
    assert media.file_id == "selected-photo"
    assert [call.kwargs["user_id"] for call in profile.await_args_list] == ([43] if reply_has_photo else [43, 42])
