"""VK boundaries use synthetic provider responses and never make network calls."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from msu_hub_bot.providers.vk import api as module
from msu_hub_bot.providers.vk.api import API_VERSION, MAX_ATTEMPTS, VkApi, VkError, VkErrorApi


class Response:
    def __init__(self, body, *, status=200, content_length=None):
        self.body, self.status, self.content_length = body, status, content_length
        self.content = self
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i : i + size]


def session_for(api, response):
    api.session = SimpleNamespace(post=MagicMock(return_value=response), close=AsyncMock())
    return api.session


async def test_version_body_bounds_redirects_and_session_ownership():
    api = VkApi("synthetic-token")
    response = Response(json.dumps({"response": {"items": []}}).encode())
    session = session_for(api, response)
    assert await api.request("wall.get", owner_id=-10) == {"items": []}
    session.post.assert_called_once_with(
        "https://api.vk.com/method/wall.get",
        data={"owner_id": -10, "access_token": "synthetic-token", "v": API_VERSION},
        allow_redirects=False,
    )
    assert response.closed
    await api.close()
    session.close.assert_awaited_once()
    untouched = VkApi("synthetic-token")
    await untouched.close()
    assert "session" not in untouched.__dict__


@pytest.mark.parametrize(
    "body,status,length", [(b"x" * 1025, 200, None), (b"{}", 200, 1025), (b"{}", 302, None), (b"[]", 200, None), (b"no-json", 200, None)]
)
async def test_bad_responses_are_bounded_safe_and_closed(monkeypatch, body, status, length):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 1024)
    api = VkApi("synthetic-token")
    response = Response(body, status=status, content_length=length)
    session_for(api, response)
    with pytest.raises(VkError):
        await api.request("wall.get", owner_id=-10)
    assert response.closed


@pytest.mark.parametrize("code,attempts", [(6, MAX_ATTEMPTS), (5, 1), (15, 1)])
async def test_retries_are_finite_and_do_not_retain_provider_error_data(monkeypatch, code, attempts):
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    api = VkApi("synthetic-token")
    api._rate_limit = AsyncMock()
    response = Response(
        json.dumps(
            {"error": {"error_code": code, "error_msg": "secret-token", "request_params": [{"access_token": "secret-token"}]}}
        ).encode()
    )
    session = session_for(api, response)
    with pytest.raises(VkErrorApi) as caught:
        await api.request("wall.get", owner_id=-10)
    assert session.post.call_count == attempts
    assert caught.value.error_code == code
    assert "secret-token" not in repr(caught.value) + str(vars(caught.value))
    assert not hasattr(caught.value, "params") and not hasattr(caught.value, "full_error")


async def test_whole_request_deadline_and_cancellation(monkeypatch):
    monkeypatch.setattr(module, "REQUEST_TIMEOUT", 0.01)
    api = VkApi("synthetic-token")
    api._rate_limit = asyncio.Event().wait
    with pytest.raises(VkError):
        await api.request("wall.get")
    task = asyncio.create_task(api.request("wall.get"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("method,params", [("../wall.get", {}), ("wall.get", {"access_token": "override"}), ("wall.get", {"v": "5.124"})])
async def test_endpoint_and_credentials_cannot_be_overridden(method, params):
    api = VkApi("synthetic-token")
    with pytest.raises(VkError):
        await api.request(method, **params)
    assert "session" not in api.__dict__


def wall(*posts):
    return {"items": list(posts)}


def post(**kwargs):
    return {"id": 1, "owner_id": -10, "text": "Public", **kwargs}


@pytest.mark.parametrize("privacy", [1, 2, "0", None])
async def test_private_or_unconfirmed_wall_is_never_requested(privacy):
    api = VkApi("synthetic-token")
    group = {"id": 10, "is_closed": privacy} if privacy is not None else {"id": 10}
    api.request = AsyncMock(return_value={"groups": [group]})
    with pytest.raises(VkError):
        await api.get_wall_post("-10_1")
    api.request.assert_awaited_once_with("groups.getById", group_ids="10")


async def test_private_copy_history_is_not_exposed_by_public_outer_post():
    api = VkApi("synthetic-token")
    api.request = AsyncMock(
        side_effect=[
            {"groups": [{"id": 10, "is_closed": 0}]},
            wall(post(copy_history=[post(owner_id=20)])),
            [{"id": 20, "is_closed": True}],
        ]
    )
    items, _ = await api.get_wall_post("-10_1")
    assert items == []
    assert api.request.await_args_list[-1].args == ("users.get",)


@pytest.mark.parametrize(
    "private_field",
    [{"friends_only": 1}, {"is_archived": True}, {"donut": {"is_donut": True}}, {"is_deleted": True}, {"post_type": "postpone"}],
)
async def test_restricted_post_flags_cannot_pass_public_wall_guard(private_field):
    api = VkApi("synthetic-token")
    api.request = AsyncMock(side_effect=[{"groups": [{"id": 10, "is_closed": 0}]}, wall(post(**private_field))])
    assert (await api.get_wall_post("-10_1"))[0] == []


async def test_unrequested_provider_posts_rejected():
    api = VkApi("synthetic-token")
    api.request = AsyncMock(side_effect=[{"groups": [{"id": 10, "is_closed": 0}]}, wall(post(id=2))])
    with pytest.raises(VkError):
        await api.get_wall_post("-10_1")


@pytest.mark.parametrize("identifier", ["-10_1?access_key=private", "1_2," * 11, "1_0", "0_1", "", "../1_1"])
async def test_post_identifiers_have_bounded_canonical_grammar(identifier):
    api = VkApi("synthetic-token")
    with pytest.raises(VkError):
        await api.get_wall_post(identifier)
    assert "session" not in api.__dict__


@pytest.mark.parametrize("post_type", ["post", "copy", "photo", "video", "clip"])
async def test_documented_published_formats_pass_all_public_source_guards(post_type):
    api = VkApi("synthetic-token")
    api.request = AsyncMock(side_effect=[{"groups": [{"id": 10, "is_closed": 0}]}, wall(post(post_type=post_type))])
    assert len((await api.get_wall_post("-10_1"))[0]) == 1


@pytest.mark.parametrize("post_type", ["postpone", "suggest", "post_ads", "reply", "unknown-future-format"])
async def test_unpublished_and_non_wall_formats_stay_excluded(post_type):
    api = VkApi("synthetic-token")
    api.request = AsyncMock(side_effect=[{"groups": [{"id": 10, "is_closed": 0}]}, wall(post(post_type=post_type))])
    assert (await api.get_wall_post("-10_1"))[0] == []


@pytest.mark.parametrize("nested", [False, True])
async def test_every_copied_source_is_checked_even_after_first_public_copy(nested):
    api = VkApi("synthetic-token")
    private = post(owner_id=30, text="Private geo or attachment", geo={"coordinates": "55 37"})
    public = post(owner_id=20)
    copies = [public, private]
    if nested:
        public["copy_history"] = [private]
        copies = [public]
    api.request = AsyncMock(
        side_effect=[
            {"groups": [{"id": 10, "is_closed": 0}]},
            wall(post(copy_history=copies)),
            [{"id": 20, "is_closed": False}],
            [{"id": 30, "is_closed": True}],
        ]
    )
    assert (await api.get_wall_post("-10_1"))[0] == []
    assert api.request.await_args_list[-1].kwargs["user_ids"] == "30"


@pytest.mark.parametrize("copied", [False, True])
async def test_private_post_access_key_is_rejected_even_on_an_open_wall(copied):
    protected = post(access_key="synthetic-private-post-key")
    item = post(copy_history=[protected | {"owner_id": 20}]) if copied else protected
    api = VkApi("synthetic-token")
    api.request = AsyncMock(side_effect=[{"groups": [{"id": 10, "is_closed": 0}]}, wall(item)])
    assert (await api.get_wall_post("-10_1"))[0] == []
    assert api.request.await_count == 2


async def test_public_attachment_access_key_is_not_treated_as_a_private_post():
    item = post(
        access_key="",
        attachments=[
            {
                "type": "photo",
                "photo": {"access_key": "synthetic-public-photo-key", "sizes": [{"url": "https://sun9.userapi.com/public.jpg"}]},
            }
        ],
    )
    api = VkApi("synthetic-token")
    api.request = AsyncMock(side_effect=[{"groups": [{"id": 10, "is_closed": 0}]}, wall(item)])
    items, extended = await api.get_wall_post("-10_1")
    from msu_hub_bot.providers.vk.posts import VkPost

    assert VkPost(items[0], extended).for_publish(with_webpreview=False)[2] == ["https://sun9.userapi.com/public.jpg"]
