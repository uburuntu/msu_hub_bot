"""Mini App adapters for verified chat settings and paused repost targets."""

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, field_validator

from msu_hub_bot.community.preferences import Preferences
from msu_hub_bot.community.reposts import RepostCreate, Reposts, RepostUpdate, SourcePreview, source_url
from msu_hub_bot.reminders.models import DEFAULT_TIMEZONE, valid_zone
from msu_hub_bot.storage.application import APPLICATION, ApplicationDocuments, ChatPreferences, VkDocument
from msu_hub_bot.storage.features import Conflict, Record

from .access import AccessDenied, ChatAccess, chat_access
from .links import Destination

if TYPE_CHECKING:
    from .server import WebServer


class PreferencesChange(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    etag: UUID | None
    timezone: str = Field(max_length=128)
    _zone = field_validator("timezone")(valid_zone)


class SettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    etag: UUID | None
    auto_speech_recognition: bool | None = None
    auto_video_links: bool | None = None
    with_nsfw: bool | None = None


class CommunityAPI:
    def __init__(self, server: WebServer, reposts: Reposts) -> None:
        self.server, self.reposts = server, reposts
        self.preferences = Preferences(server.reminders.store)
        self.documents = ApplicationDocuments(server.reminders.store, server.database.get_chat)

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/community", self.community)
        app.router.add_get("/api/preferences", self.get_preferences)
        app.router.add_patch("/api/preferences", self.update_preferences)
        app.router.add_get("/api/chats/{chat_id}/settings", self.settings)
        app.router.add_patch("/api/chats/{chat_id}/settings", self.change_settings)
        app.router.add_get("/api/reposts", self.list_reposts)
        app.router.add_post("/api/reposts", self.create_repost)
        app.router.add_post("/api/reposts/preview", self.preview_repost)
        app.router.add_patch("/api/reposts/{key}", self.update_repost)

    async def access(self, request: web.Request) -> ChatAccess:
        from .server import USER

        access = await chat_access(
            self.server.bot, self.server.links, request[USER].id, request.query.get("launch"), now=self.server.clock()
        )
        if "chat_id" in request.match_info and int(request.match_info["chat_id"]) != access.destination.chat_id:
            raise AccessDenied()
        return access

    async def destination(self, request: web.Request, *, admin: bool = False) -> Destination:
        return (await self.access(request)).require(admin=admin)

    async def _preferences(self, user_id: int) -> dict[str, object]:
        row = await self.preferences.get(user_id)
        return {"timezone": row.value.timezone if row else DEFAULT_TIMEZONE, "etag": row.etag if row else None}

    async def community(self, request: web.Request) -> web.Response:
        from .server import USER

        access = await self.access(request)
        return web.json_response(
            {
                "context": {
                    "chat_id": access.destination.chat_id,
                    "thread_id": access.destination.thread_id,
                    "label": await self.server._label(request[USER].id, access.destination),
                },
                "access": {"member": access.member, "admin": access.admin, "bot_admin": access.bot_admin},
                "preferences": await self._preferences(request[USER].id),
                "automatic_reposts": False,
            }
        )

    async def get_preferences(self, request: web.Request) -> web.Response:
        from .server import USER

        return web.json_response(await self._preferences(request[USER].id))

    async def update_preferences(self, request: web.Request) -> web.Response:
        from .server import USER

        body = await self.server._body(request, PreferencesChange)
        row = await self.preferences.update(request[USER].id, body.timezone, str(body.etag) if body.etag else None)
        return web.json_response({"timezone": row.value.timezone, "etag": row.etag})

    async def _settings(self, chat_id: int) -> tuple[Record[ChatPreferences] | None, ChatPreferences]:
        row = await self.documents.settings.get(APPLICATION, str(chat_id))
        if row:
            return row, row.value
        chat = await self.server.database.get_chat(chat_id)
        return None, ChatPreferences.model_validate(ApplicationDocuments._seed(chat) if chat else {})

    @staticmethod
    def _settings_payload(row: Record[ChatPreferences] | None, value: ChatPreferences) -> dict[str, object]:
        return {
            "etag": row.etag if row else None,
            "values": {
                "auto_speech_recognition": value.auto_speech_recognition,
                "auto_video_links": value.auto_video_links,
                "with_nsfw": value.with_nsfw,
            },
        }

    async def settings(self, request: web.Request) -> web.Response:
        destination = await self.destination(request)
        return web.json_response(self._settings_payload(*await self._settings(destination.chat_id)))

    async def change_settings(self, request: web.Request) -> web.Response:
        destination = await self.destination(request, admin=True)
        body = await self.server._body(request, SettingsChange)
        row, value = await self._settings(destination.chat_id)
        if (row.etag if row else None) != (str(body.etag) if body.etag else None):
            raise Conflict()
        value = value.model_copy(deep=True)
        for name in ("auto_speech_recognition", "auto_video_links", "with_nsfw"):
            if name in body.model_fields_set:
                if (changed := getattr(body, name)) is None:
                    raise ValueError("Settings cannot be cleared")
                setattr(value, name, changed)
        tx = self.documents.store.transaction("settings", APPLICATION, operation_id=uuid4().hex)
        if row:
            tx.expect(row)
        else:
            tx.expect_absent("chats", str(destination.chat_id))
        tx.put(self.documents.settings, str(destination.chat_id), value)
        result = await ApplicationDocuments._commit(tx)
        row = self.documents.settings.decode(result.records[0], APPLICATION)
        return web.json_response(self._settings_payload(row, row.value))

    @staticmethod
    def _repost(row: Record[VkDocument]) -> dict[str, object]:
        value = row.value
        return {
            "key": row.key,
            "etag": row.etag,
            "owner_id": value.owner_id,
            "source_url": source_url(value.owner_id),
            "chat_id": value.chat_id,
            "thread_id": value.thread_id,
            "title": value.title or value.description or "",
            "with_reposts": value.with_reposts,
            "with_header": value.with_header,
            "include_keywords": value.include_keywords,
            "exclude_keywords": value.exclude_keywords,
            "last_post_id": value.last_post_id,
            "is_suspended": True,
            "archived": value.archived,
        }

    async def list_reposts(self, request: web.Request) -> web.Response:
        destination = await self.destination(request, admin=True)
        rows = await self.reposts.list(destination.chat_id, destination.thread_id)
        return web.json_response({"items": [self._repost(row) for row in rows], "automatic_posting": False})

    async def create_repost(self, request: web.Request) -> web.Response:
        from .server import USER

        destination = await self.destination(request, admin=True)
        row = await self.reposts.create(
            request[USER].id, destination.chat_id, destination.thread_id, await self.server._body(request, RepostCreate)
        )
        return web.json_response(self._repost(row), status=201)

    async def update_repost(self, request: web.Request) -> web.Response:
        from .server import USER

        destination = await self.destination(request, admin=True)
        row = await self.reposts.update(
            request[USER].id,
            destination.chat_id,
            destination.thread_id,
            request.match_info["key"],
            await self.server._body(request, RepostUpdate),
        )
        return web.json_response(self._repost(row))

    async def preview_repost(self, request: web.Request) -> web.Response:
        await self.destination(request, admin=True)
        return web.json_response(await self.reposts.preview(await self.server._body(request, SourcePreview)))
