"""Same-origin Mini App API over authenticated, author-owned feature services."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage
from aiogram.types import ChatMemberRestricted, LinkPreviewOptions
from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from msu_hub_bot.community.reposts import RepostError, Reposts
from msu_hub_bot.feedback.models import FeedbackAccessDenied, FeedbackError, FeedbackNotFound
from msu_hub_bot.feedback.service import FeedbackService
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.reminders import ReminderService, Schedule, parse_schedule
from msu_hub_bot.reminders.models import Recurrence, Reminder, ReminderError
from msu_hub_bot.reminders.presentation import confirmation, keyboard
from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.features import Conflict, FeatureError, Record
from msu_hub_bot.storage.errors import RepositoryError
from msu_hub_bot.telemetry import Boundary, Outcome, Telemetry, failure_outcome

from .auth import AuthenticationError, WebUser, authenticate
from .links import Destination, LaunchError, WebAppLinks
from .access import AccessDenied
from .community import CommunityAPI
from .feedback import FeedbackAPI

logger = logging.getLogger(__name__)
USER = web.RequestKey("user", WebUser)
REQUEST_TIMEOUT = 15


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class RecurrenceInput(Recurrence):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Creation(Input):
    request_id: UUID
    text: str = Field(min_length=1, max_length=6000)
    schedule: str = Field(min_length=1, max_length=256)
    timezone: str = Field(default="Europe/Moscow", min_length=1, max_length=128)
    launch: str | None = Field(default=None, max_length=64)
    recurrence: RecurrenceInput | None = None


class Revision(Input):
    etag: UUID


class Reschedule(Revision):
    schedule: str = Field(min_length=1, max_length=256)
    timezone: str = Field(default="Europe/Moscow", min_length=1, max_length=128)
    text: str = Field(default="", max_length=6000)
    recurrence: RecurrenceInput | None = None


def error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


def _error_response(exc: Exception, *, feedback: bool) -> web.Response:
    """Translate expected API failures before the observed request completes."""
    if isinstance(exc, AuthenticationError):
        return error(401, "authentication", "Сессия закончилась. Закрой и открой приложение через бота.")
    if isinstance(exc, LaunchError):
        return error(422, "launch", str(exc))
    if isinstance(exc, (AccessDenied, FeedbackAccessDenied)):
        return error(403, "access", str(exc))
    if isinstance(exc, FeedbackNotFound):
        return error(404, "not_found", str(exc))
    if isinstance(exc, FeedbackError):
        return error(422, "feedback", str(exc))
    if isinstance(exc, RepostError):
        return error(422, "repost", str(exc))
    if isinstance(exc, Conflict):
        return error(409, "conflict", "Запись уже изменилась или существует. Обнови её и попробуй снова.")
    if isinstance(exc, ReminderError):
        return error(422, "reminder", str(exc))
    if isinstance(exc, (ValidationError, ValueError, json.JSONDecodeError, RecursionError)):
        message = "Проверь данные отзыва." if feedback else "Проверь текст, время и часовой пояс."
        return error(422, "input", message)
    if isinstance(exc, (TimeoutError, RepositoryError, FeatureError, TelegramAPIError)):
        return error(503, "unavailable", "Не удалось подтвердить ответ. Попробуй ещё раз.")
    if isinstance(exc, web.HTTPException):
        return error(exc.status, "request", "Запрос недоступен.")
    logger.error("Mini App request failed")
    return error(500, "unexpected", "Не получилось выполнить запрос. Попробуй позже.")


def _http_outcome(status: int, exc: Exception | None = None) -> Outcome:
    if exc is not None and failure_outcome(exc) is Outcome.TIMEOUT:
        return Outcome.TIMEOUT
    if status == 500:
        return Outcome.UNEXPECTED
    if status >= 500:
        return Outcome.UNAVAILABLE
    return Outcome.REJECTED if status >= 400 else Outcome.SUCCESS


class WebServer:
    def __init__(
        self,
        bot: Bot,
        reminders: ReminderService,
        database: BotRepository,
        links: WebAppLinks,
        telemetry: Telemetry,
        *,
        port: int = 8081,
        static_path: Path | None = None,
        vk_api: VkApi | None = None,
        settings_changed: Callable[[int], Awaitable[None]] | None = None,
        feedback: FeedbackService | None = None,
    ) -> None:
        self.bot, self.reminders, self.database, self.links, self.telemetry = bot, reminders, database, links, telemetry
        self.port = port
        self.settings_changed = settings_changed
        self.static_path = static_path or Path(__file__).with_name("static")
        endpoint = urlsplit(links.url)
        self.origin = f"{endpoint.scheme}://{endpoint.netloc}"
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.runner: web.AppRunner | None = None
        self.accepting = True
        self._requests = asyncio.Semaphore(32)
        self._label_queries = asyncio.Semaphore(8)
        self.community = CommunityAPI(self, Reposts(reminders.store, vk_api))
        self.feedback = FeedbackAPI(self, feedback)

    @web.middleware
    async def _boundary(self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        response: web.StreamResponse
        try:
            if not self.accepting:
                response = error(503, "unavailable", "Бот перезапускается. Попробуй через минуту.")
            elif request.path.startswith("/api/"):
                origin = request.headers.get("Origin")
                if origin is not None and origin != self.origin:
                    response = error(403, "origin", "Открой приложение через Telegram.")
                else:
                    auth = request.headers.get("Authorization", "")
                    if not auth.startswith("tma "):
                        raise AuthenticationError()
                    user = authenticate(auth[4:], self.bot.token, now=self.clock())
                    request[USER] = user
                    with self.telemetry.context(user_id=user.id):
                        with self.telemetry.operation(Boundary.WEB, "web.request") as span:
                            try:
                                async with asyncio.timeout(REQUEST_TIMEOUT), self._requests:
                                    response = await handler(request)
                            except Exception as exc:
                                response = _error_response(exc, feedback=request.path.startswith("/api/feedback"))
                                span.fail(exc, outcome=_http_outcome(response.status, exc))
                            else:
                                span.set_outcome(_http_outcome(response.status))
                            span.http_status(response.status)
            else:
                response = await handler(request)
        except Exception as exc:
            response = _error_response(exc, feedback=request.path.startswith("/api/feedback"))
        response.headers.update(
            {
                "Cache-Control": "no-store" if request.path.startswith("/api/") else "no-cache",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org",
            }
        )
        return response

    def application(self) -> web.Application:
        app = web.Application(middlewares=[self._boundary], client_max_size=32 * 1024)
        app.router.add_get("/api/session", self._session)
        app.router.add_get("/api/reminders", self._list)
        app.router.add_post("/api/reminders", self._create)
        app.router.add_get("/api/reminders/{key}", self._get)
        app.router.add_post("/api/reminders/{key}/{action:reschedule|cancel|retry}", self._change)
        self.community.register(app)
        self.feedback.register(app)
        app.router.add_get("/", self._index)
        if (self.static_path / "assets").is_dir():
            app.router.add_static("/assets", self.static_path / "assets", show_index=False, follow_symlinks=False)
        return app

    async def start(self) -> None:
        if self.runner is not None:
            raise RuntimeError("Mini App listener is already running")
        if not (self.static_path / "index.html").is_file():
            raise RuntimeError("Mini App assets are missing from the release")
        self.accepting = True
        self.runner = web.AppRunner(self.application(), access_log=None, shutdown_timeout=15)
        try:
            await self.runner.setup()
            await web.TCPSite(self.runner, host="0.0.0.0", port=self.port).start()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self.accepting = False
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def _index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self.static_path / "index.html")

    async def _label(self, user_id: int, destination: Destination) -> str:
        if destination.chat_id == user_id:
            return "Личные сообщения" + (f" · тема {destination.thread_id}" if destination.thread_id else "")
        async with self._label_queries:
            chat = await self.database.get_chat(destination.chat_id)
        label = chat.full_name if chat is not None else None
        return (label or "Чат") + (f" · тема {destination.thread_id}" if destination.thread_id else "")

    async def _session(self, request: web.Request) -> web.Response:
        user = request[USER]
        destination = self.links.destination(user.id, request.query.get("launch"), now=self.clock())
        return web.json_response(
            {
                "user": {"id": user.id, "name": user.name},
                "context": {
                    "chat_id": destination.chat_id,
                    "thread_id": destination.thread_id,
                    "label": await self._label(user.id, destination),
                },
                "default_timezone": await self.community.preferences.timezone(user.id),
                "capabilities": {"feedback_review": self.feedback.allowed(user.id)},
                "now": self.clock().isoformat(),
            }
        )

    def _item(self, record: Record[Reminder], *, label: str | None = None) -> dict[str, object]:
        return {
            **record.value.model_dump(mode="json"),
            "recurrence": record.value.recurrence.model_dump(exclude_none=True) if record.value.recurrence else None,
            "key": record.key,
            "etag": record.etag,
            "created_at": record.created_at.isoformat(),
            **({"destination_label": label} if label is not None else {}),
        }

    async def _list(self, request: web.Request) -> web.Response:
        user = request[USER]
        limit = int(request.query.get("limit", "50"))
        if not 1 <= limit <= 100:
            raise ValueError
        records = await self.reminders.list(user.id, after=request.query.get("after"), limit=limit)
        destinations = list(dict.fromkeys(Destination(record.value.chat_id, record.value.thread_id) for record in records))
        tasks = [asyncio.create_task(self._label(user.id, destination)) for destination in destinations]
        try:
            labels = dict(zip(destinations, await asyncio.gather(*tasks), strict=True))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return web.json_response(
            {
                "items": [
                    self._item(record, label=labels[Destination(record.value.chat_id, record.value.thread_id)]) for record in records
                ],
                "next_cursor": records[-1].key if len(records) == limit else None,
            }
        )

    async def _get(self, request: web.Request) -> web.Response:
        record = await self.reminders.get(request[USER].id, request.match_info["key"])
        return web.json_response(self._item(record))

    async def _body[M: BaseModel](self, request: web.Request, model: type[M]) -> M:
        if request.content_type != "application/json":
            raise ValueError

        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON field")
                result[key] = value
            return result

        return model.model_validate(await request.json(loads=lambda value: json.loads(value, object_pairs_hook=unique)))

    def _schedule(self, expression: str, text: str, timezone: str) -> Schedule:
        parsed = parse_schedule(expression, now=self.clock(), default_timezone=timezone)
        if parsed.text:
            raise ReminderError("В поле времени укажи только дату или интервал; текст напоминания — отдельно.")
        return Schedule(due_at=parsed.due_at, timezone=parsed.timezone, text=text)

    async def _confirmation(self, record: Record[Reminder]) -> None:
        try:
            async with asyncio.timeout(5):
                await self.bot(
                    SendMessage(
                        chat_id=record.value.chat_id,
                        message_thread_id=record.value.thread_id,
                        text=confirmation(record),
                        parse_mode=None,
                        reply_markup=keyboard(record),
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    ),
                    request_timeout=5,
                )
        except Exception:
            # The reminder is committed; an uncertain acknowledgement is never resent.
            logger.warning("Mini App reminder acknowledgement unavailable")

    async def _can_write(self, user_id: int, destination: Destination) -> bool:
        if destination.chat_id > 0:
            return destination.chat_id == user_id
        member = await self.bot.get_chat_member(destination.chat_id, user_id)
        return member.status in {"creator", "administrator", "member", "restricted"} and not (
            isinstance(member, ChatMemberRestricted) and (not member.is_member or not member.can_send_messages)
        )

    async def _create(self, request: web.Request) -> web.Response:
        user, body = request[USER], await self._body(request, Creation)
        destination = self.links.destination(user.id, body.launch, now=self.clock())
        source_id = self.links.request_message_id(user.id, str(body.request_id))
        old = await self.reminders.get_creation(
            author_id=user.id, chat_id=destination.chat_id, thread_id=destination.thread_id, source_message_id=source_id
        )
        if old is not None:
            return web.json_response(self._item(old))
        if not await self._can_write(user.id, destination):
            return error(403, "membership", "Создать напоминание можно в своём чате.")
        timezone = body.timezone if "timezone" in body.model_fields_set else await self.community.preferences.timezone(user.id)
        schedule = self._schedule(body.schedule, body.text, timezone)
        schedule.recurrence = body.recurrence
        record, created = await self.reminders.create_with_status(
            author_id=user.id,
            author_name=user.name,
            chat_id=destination.chat_id,
            thread_id=destination.thread_id,
            source_message_id=source_id,
            schedule=schedule,
        )
        if created:
            await self._confirmation(record)
        return web.json_response(self._item(record), status=201 if created else 200)

    async def _change(self, request: web.Request) -> web.Response:
        user, key = request[USER], request.match_info["key"]
        action = cast(Literal["reschedule", "cancel", "retry"], request.match_info["action"])
        body = await self._body(request, Reschedule if action == "reschedule" else Revision)
        current = await self.reminders.get(user.id, key)
        if current.etag != str(body.etag):
            raise Conflict
        if action != "cancel" and not await self._can_write(user.id, Destination(current.value.chat_id, current.value.thread_id)):
            return error(403, "membership", "Изменить или повторить напоминание можно, пока ты можешь писать в этот чат.")
        if isinstance(body, Reschedule):
            timezone = body.timezone if "timezone" in body.model_fields_set else current.value.timezone
            schedule = self._schedule(body.schedule, body.text or current.value.text, timezone)
            if "recurrence" in body.model_fields_set:
                schedule.recurrence = body.recurrence
            record = await self.reminders.reschedule(user.id, key, schedule, expected_etag=str(body.etag))
        elif action == "cancel":
            record = await self.reminders.cancel(user.id, key, expected_etag=str(body.etag))
        else:
            record = await self.reminders.retry(user.id, key, expected_etag=str(body.etag))
        return web.json_response(self._item(record))
