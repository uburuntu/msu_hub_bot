"""Invocation-local Telegram context; feature instances never own request state."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, overload

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChosenInlineResult,
    InaccessibleMessage,
    InlineKeyboardMarkup,
    InputRichMessage,
    LinkPreviewOptions,
    Message,
    MessageEntity,
    MessageId,
    ReplyMarkupUnion,
    User,
)
from aiogram.types.base import TelegramObject
from aiogram.utils.formatting import Text

from .delivery import (
    DeliveryError,
    DeliveryProgress,
    DeliveryTarget,
    MediaSource,
    NativeResult,
    ResponsePolicy,
    Target,
    TargetKind,
    edit_response,
    rejected,
    send_response,
)
from .formatting import ResponseError, units
from .isolation import IsolationError, IsolationScope, ScopedStorage
from .outcome import Acknowledgement, InputIssue, InvocationOutcome, Presentation, attach_outcome

if TYPE_CHECKING:
    from .inputs import InputError


class ReplyOptions(TypedDict, total=False):
    policy: ResponsePolicy
    photo: MediaSource | None
    video: MediaSource | None
    audio: MediaSource | None
    document: MediaSource | None
    animation: MediaSource | None
    entities: Sequence[MessageEntity] | None
    reply_markup: ReplyMarkupUnion | None
    width: int | None
    height: int | None
    duration: int | None
    supports_streaming: bool | None
    allow_sending_without_reply: bool
    allow_remote_media: bool
    request_timeout: int | None
    progress: DeliveryProgress | None


class EditOptions(TypedDict, total=False):
    policy: ResponsePolicy
    kind: TargetKind | None
    rich_message: InputRichMessage | None
    photo: MediaSource | None
    video: MediaSource | None
    audio: MediaSource | None
    document: MediaSource | None
    animation: MediaSource | None
    entities: Sequence[MessageEntity] | None
    reply_markup: InlineKeyboardMarkup | None
    link_preview_options: LinkPreviewOptions | None
    allow_remote_media: bool
    request_timeout: int | None
    progress: DeliveryProgress | None


class Context:
    """The native event, acquired input provenance and delivery address are separate."""

    def __init__(
        self,
        bot: Bot,
        event: TelegramObject,
        *,
        data: dict[str, Any] | None = None,
        policy: ResponsePolicy | None = None,
    ) -> None:
        self.bot = bot
        self.event = event
        self.data = data if data is not None else {}
        self.policy = policy if policy is not None else ResponsePolicy()
        self.input_sources: dict[str, Message] = {}
        self.response_target: Target | None = self.message
        self.delivery_progress: DeliveryProgress | None = None
        self.has_effects = False
        self._handler_returned = False
        self._presentations: list[tuple[Literal["reply", "edit", "native"], DeliveryProgress]] = []
        self._input_issue: InputIssue | None = None
        self._isolation_released = False
        # The host bridge attests that native state loading and route selection
        # occurred under its current task's lock. Host ownership stays external.
        release = self.data.get("_teleforge_release_isolation")
        if release is not None and "_teleforge_isolation" not in self.data:
            if not callable(release):
                raise TypeError("The terminal isolation bridge must be an async callable")
            scope = IsolationScope.from_host(release)
            self.data["_teleforge_isolation"] = scope
            state = self.data.get("state")
            if isinstance(state, FSMContext):
                state.storage = ScopedStorage(state.storage, scope)
                self.data["fsm_storage"] = state.storage
        if isinstance(event, CallbackQuery | ChosenInlineResult) and event.inline_message_id:
            self.response_target = DeliveryTarget(inline_message_id=event.inline_message_id)
        elif self.response_target is None:
            chat = getattr(event, "chat", None)
            if isinstance(chat, Chat):
                self.response_target = DeliveryTarget(chat_id=chat.id)

    @property
    def user(self) -> User | None:
        actor = getattr(self.event, "from_user", None)
        if isinstance(actor, User):
            return actor
        actor = getattr(self.event, "user", None)
        return actor if isinstance(actor, User) else None

    @property
    def message(self) -> Message | InaccessibleMessage | None:
        if isinstance(self.event, Message):
            return self.event
        if isinstance(self.event, CallbackQuery):
            return self.event.message
        return None

    @property
    def actor(self) -> User | None:
        return self.user

    @property
    def outcome(self) -> InvocationOutcome:
        return InvocationOutcome(
            handler_returned=self._handler_returned,
            acknowledgement=self.acknowledgement if isinstance(self, CallbackContext) else None,
            presentations=tuple(
                Presentation(
                    kind,
                    progress.attempted_part is not None,
                    progress.confirmed_count
                    if progress.confirmed_count is not None
                    else len(progress.confirmed) or int(progress.phase == "complete"),
                    progress.uncertain,
                    progress.phase,
                )
                for kind, progress in self._presentations
            ),
            input_issue=self._input_issue,
        )

    async def release_isolation(self) -> None:
        """Promise terminal selection: no later FSM access or SkipHandler continuation."""
        scope = self.data.get("_teleforge_isolation")
        if isinstance(scope, IsolationScope):
            self._isolation_released = True
            await scope.release()
        elif "_teleforge_isolation" not in self.data and (
            self.data.get("state") is not None or self.data.get("fsm_storage") is not None
        ):
            raise IsolationError(
                "Early acknowledgement/coalescing requires terminal FSM release; "
                "use App.create_dispatcher() or a host integration with an explicit release scope"
            )
        self._isolation_released = True

    async def guide(self, issue: str | Text | InputError) -> None:
        """Explicitly present a structured user issue; developer errors should propagate."""
        from .inputs import InputError

        text: str | Text
        if isinstance(issue, InputError):
            self._input_issue = InputIssue(issue.code, tuple(sorted(issue.params.items())))
            formatter = self.data.get("_teleforge_input_formatter", str)
            if not callable(formatter):
                raise TypeError("The input issue formatter must be callable")
            text = formatter(issue)
            if not isinstance(text, str):
                raise TypeError("The input issue formatter must return a string")
        else:
            self._input_issue = InputIssue("guidance", ())
            text = issue
        can_answer = (
            isinstance(self, CallbackContext) and self.acknowledgement.owned and not self.acknowledgement.attempted
        )
        if not can_answer and not isinstance(self.message, Message):
            if isinstance(issue, InputError):
                raise issue
            raise ResponseError("Input guidance requires an available chat target or callback acknowledgement")
        try:
            if can_answer:
                assert isinstance(self, CallbackContext)
                if isinstance(text, Text):
                    text = text.render()[0]
                used = 0
                for index, character in enumerate(text):
                    used += 2 if ord(character) > 0xFFFF else 1
                    if used > 180:
                        text = text[:index]
                        break
                await self.answer(text, show_alert=True)
            else:
                await self.reply(text, to=self.message, policy=ResponsePolicy(rich=False, soft_messages=1))
        except (DeliveryError, ResponseError) as error:
            if isinstance(issue, InputError):
                issue.add_note(f"Input guidance could not be delivered ({type(error).__name__}).")
                attach_outcome(issue, self.outcome)
                raise issue from None
            raise

    @overload
    async def reply(
        self,
        text: str | Text | None = None,
        *,
        to: Target | None = None,
        fixed: Literal[True],
        **options: Unpack[ReplyOptions],
    ) -> Message: ...

    @overload
    async def reply(
        self,
        text: str | Text | None = None,
        *,
        to: Target | None = None,
        fixed: bool = False,
        **options: Unpack[ReplyOptions],
    ) -> Message | list[Message]: ...

    async def reply(
        self,
        text: str | Text | None = None,
        *,
        to: Target | None = None,
        fixed: bool = False,
        **options: Unpack[ReplyOptions],
    ) -> Message | list[Message]:
        target = to if to is not None else self.response_target
        if target is None:
            raise ResponseError("This event has no response target; supply to explicitly")
        if to is None and isinstance(target, InaccessibleMessage):
            raise ResponseError("The callback message is inaccessible; supply an explicit DeliveryTarget for a reply")
        options.setdefault("policy", self.policy)
        progress = options.get("progress") or DeliveryProgress()
        options["progress"] = self.delivery_progress = progress
        self._presentations.append(("reply", progress))
        try:
            result = await send_response(self.bot, target, text, fixed=fixed, **options)
        except BaseException as error:
            attach_outcome(error, self.outcome)
            raise
        finally:
            self.has_effects |= bool(progress.confirmed) or progress.uncertain
        self.has_effects = True
        return result

    async def edit(
        self,
        text: str | Text | None = None,
        *,
        to: Target | None = None,
        **options: Unpack[EditOptions],
    ) -> NativeResult:
        # Input acquisition may retarget reply(), but edit() always addresses the actual UI.
        target = to if to is not None else self.message
        if (
            target is None
            and isinstance(self.event, CallbackQuery | ChosenInlineResult)
            and self.event.inline_message_id
        ):
            target = DeliveryTarget(inline_message_id=self.event.inline_message_id)
        if target is None:
            raise ResponseError("This event has no editable UI; supply to explicitly")
        options.setdefault("policy", self.policy)
        progress = options.get("progress") or DeliveryProgress()
        options["progress"] = self.delivery_progress = progress
        self._presentations.append(("edit", progress))
        try:
            result = await edit_response(self.bot, target, text, **options)
        except BaseException as error:
            attach_outcome(error, self.outcome)
            raise
        finally:
            self.has_effects |= bool(progress.confirmed) or progress.uncertain
        self.has_effects = True
        return result

    async def _execute_native(self, method: TelegramMethod[Any]) -> object:
        if (
            isinstance(self, CallbackContext)
            and isinstance(method, AnswerCallbackQuery)
            and method.callback_query_id == self.query.id
        ):
            return await self._answer_method(method)
        if not method.__api_method__.startswith(("send", "editMessage", "copyMessage", "forwardMessage")):
            return await self.bot(method)
        progress = DeliveryProgress(attempted_part=0, total_parts=1, phase="sending", uncertain=True)
        self.delivery_progress = progress
        self._presentations.append(("native", progress))
        try:
            result = await self.bot(method)
        except BaseException as error:
            progress.phase = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            progress.uncertain = not isinstance(error, Exception) or not rejected(error)
            attach_outcome(error, self.outcome)
            raise
        items = result if isinstance(result, list) else [result]
        references = []
        count = 0
        for item in items:
            if isinstance(item, Message):
                references.append((item.chat.id, item.message_id))
                count += 1
            elif isinstance(item, MessageId):
                chat_id = getattr(method, "chat_id", None)
                if isinstance(chat_id, int):
                    references.append((chat_id, item.message_id))
                count += 1
        # Inline edits and already-present edits confirm one UI without a
        # returned Message. Other bool methods do not identify sent messages.
        if result is True and method.__api_method__.startswith("editMessage"):
            count = 1
        progress.confirmed = tuple(references)
        progress.confirmed_count = count
        progress.phase, progress.uncertain = "complete", False
        self.has_effects = True
        return result

    async def finish(self) -> None:
        """Normal completion hook. It must not be called while unwinding an error."""


class MessageContext(Context):
    """A message invocation; its acquired inputs need not come from that message."""

    event: Message

    def __init__(
        self, bot: Bot, event: Message, *, data: dict[str, Any] | None = None, policy: ResponsePolicy | None = None
    ) -> None:
        super().__init__(bot, event, data=data, policy=policy)

    @property
    def message(self) -> Message:
        return self.event

    @property
    def chat(self) -> Chat:
        return self.event.chat


class CallbackContext(Context):
    event: CallbackQuery

    def __init__(
        self,
        bot: Bot,
        event: CallbackQuery,
        *,
        data: dict[str, Any] | None = None,
        policy: ResponsePolicy | None = None,
    ) -> None:
        super().__init__(bot, event, data=data, policy=policy)
        self.query = event
        self._acknowledgement = Acknowledgement()

    @property
    def user(self) -> User:
        return self.query.from_user

    @property
    def actor(self) -> User:
        return self.user

    @property
    def acknowledgement(self) -> Acknowledgement:
        return self._acknowledgement

    def manual_ack(self) -> CallbackQuery:
        """Give acknowledgement ownership to a native handler; finish becomes inert."""
        self._acknowledgement = replace(self.acknowledgement, owned=False)
        return self.query

    async def answer(
        self,
        text: str | None = None,
        *,
        show_alert: bool = False,
        url: str | None = None,
        cache_time: int = 0,
        request_timeout: int | None = None,
    ) -> bool | None:
        state = self.acknowledgement
        if state.attempted:
            return None
        if text is not None:
            try:
                size = units(text)
            except UnicodeError:
                raise ResponseError("The callback notification contains invalid Unicode") from None
            if size > 200:
                raise ResponseError("Callback notifications must fit 200 UTF-16 units")
        if type(cache_time) is not int or cache_time < 0:
            raise ResponseError("Callback cache_time must be nonnegative")
        method = AnswerCallbackQuery(
            callback_query_id=self.query.id, text=text, show_alert=show_alert, url=url, cache_time=cache_time
        )
        return await self._answer_method(method, request_timeout=request_timeout)

    async def _answer_method(self, method: AnswerCallbackQuery, *, request_timeout: int | None = None) -> bool | None:
        if self.acknowledgement.attempted:
            return None
        self._acknowledgement = replace(self.acknowledgement, attempted=True, uncertain=True)
        try:
            async with asyncio.timeout(self.policy.timeout):
                result = await self.bot(method, request_timeout=request_timeout)
            if result is not True:
                raise ValueError("Telegram did not confirm the acknowledgement")
        except asyncio.CancelledError as error:
            attach_outcome(error, self.outcome)
            raise
        except Exception as error:  # noqa: BLE001 - acknowledgement write boundary
            self._acknowledgement = replace(self.acknowledgement, uncertain=not rejected(error))
            failure = DeliveryError(
                DeliveryProgress(
                    attempted_part=0, total_parts=1, phase="failed", uncertain=self.acknowledgement.uncertain
                ),
                error,
            )
            attach_outcome(failure, self.outcome)
            raise failure from None
        self._acknowledgement = replace(self.acknowledgement, confirmed=True, uncertain=False)
        return True

    async def finish(self) -> None:
        if self.acknowledgement.owned and not self.acknowledgement.attempted:
            await self.answer()


def context_for(
    bot: Bot,
    event: TelegramObject,
    *,
    data: dict[str, Any] | None = None,
    policy: ResponsePolicy | None = None,
) -> Context:
    if isinstance(event, CallbackQuery):
        return CallbackContext(bot, event, data=data, policy=policy)
    if isinstance(event, Message):
        return MessageContext(bot, event, data=data, policy=policy)
    return Context(bot, event, data=data, policy=policy)
