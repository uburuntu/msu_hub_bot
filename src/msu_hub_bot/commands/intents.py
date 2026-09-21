"""On-demand natural-language aliases for a small set of reply commands."""

import re
import time
from collections.abc import Callable
from typing import cast

from aiogram import Bot
from aiogram.enums import ContentType
from aiogram.types import Message
from cachetools import TTLCache

from msu_hub_bot.commands.externals import process_bg, process_topdf, process_which_anime
from msu_hub_bot.commands.song import process_song
from msu_hub_bot.commands.tesseract import process_image_to_text
from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES
from msu_hub_bot.providers.jev import MAX_REQUEST_LENGTH, JevClient, JevError, JevErrorReason, ReplyMetadata
from msu_hub_bot.settings import MissingIntegration
from msu_hub_bot.telegram.chat_actioner import ChatActioner
from msu_hub_bot.telegram.command_api import invoke_command
from msu_hub_bot.telegram.extraction import SimpleExtractor
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telemetry import Boundary, Outcome, Provider, Telemetry

COMMANDS = {
    "pdf": ("process_topdf", "документ в PDF — /pdf"),
    "text": ("process_image_to_text", "текст с картинки — /text"),
    "bg": ("process_bg", "убрать фон — /bg"),
    "song": ("process_song", "узнать песню — /song"),
    "anime": ("process_which_anime", "узнать аниме по кадру — /anime"),
}
HELP = "Пока умею выбрать одну задачу:\n" + "\n".join(f"• {hint}" for _, hint in COMMANDS.values())
CommandResult = Message | list[Message] | bool


class IntentCommands:
    def __init__(
        self,
        client: JevClient,
        *,
        telemetry: Telemetry,
        confidence: float = 0.8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.telemetry = telemetry
        self.confidence = confidence
        self._recent: TTLCache[int, bool] = TTLCache(maxsize=4096, ttl=5, timer=clock)
        self._active: set[int] = set()

    async def handle(self, message: Message, request: str, bot: Bot, cpu_executor: TPExecutor) -> CommandResult:
        source = message.reply_to_message
        if message.content_type != ContentType.TEXT or not message.text or message.from_user is None:
            return True
        if source is None:
            return await message.reply("Ответь на нужное сообщение и позови меня: например, «@msu_hub_bot сделай PDF».\n\n" + HELP)
        if source.chat.id != message.chat.id or (
            source.message_thread_id is not None
            and message.message_thread_id is not None
            and source.message_thread_id != message.message_thread_id
        ):
            return await message.reply("Ответь на исходное сообщение в этом чате и теме.")
        if not request:
            return await message.reply("Добавь, что нужно сделать: например, «вытащи текст» или «убери фон».\n\n" + HELP)
        if len(request) > MAX_REQUEST_LENGTH:
            return await message.reply("Напиши просьбу покороче — до 1500 символов, одна задача за раз.")

        # Check this exact reply's attachments; no profile or nested-reply fallback.
        image = await SimpleExtractor.image(source)
        document = await SimpleExtractor.document(source)
        audio = source.voice or source.audio
        video = source.video_note or source.video
        metadata = ReplyMetadata(
            image=image is not None,
            document=document is not None,
            audio=audio is not None,
            video=video is not None,
            mime_type=document.mime_type
            if document
            and document.mime_type
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,47}/[A-Za-z0-9][A-Za-z0-9.+_-]{0,47}", document.mime_type)
            else None,
        )
        if not (metadata.image or metadata.document or metadata.audio or metadata.video):
            return await message.reply("В ответе нужен файл, картинка или запись со звуком.\n\n" + HELP)
        # Command handlers prefer invocation media. This entry only accepts a
        # text request, so their real reply remains the single possible source.
        if (
            await SimpleExtractor.image(message) is not None
            or await SimpleExtractor.document(message) is not None
            or (message.audio or message.voice or message.video or message.video_note)
        ):
            return await message.reply("Напиши просьбу отдельным текстовым ответом на нужное вложение.")

        user_id = message.from_user.id
        if user_id in self._active:
            return await message.reply("Ещё выполняю твою предыдущую просьбу — подожди немного.")
        if user_id in self._recent:
            return await message.reply("Дай мне пару секунд между просьбами 🙂")
        if len(self._active) >= 4:
            return await message.reply("Сейчас занят несколькими просьбами. Попробуй чуть позже.")
        self._recent[user_id] = True
        self._active.add(user_id)
        try:
            async with ChatActioner(message, "typing"):
                with self.telemetry.operation(Boundary.PROVIDER, "jev.classify", provider=Provider.JEV) as observation:
                    try:
                        decision = await self.client.classify(request, metadata)
                    except JevError as error:
                        observation.set_outcome(Outcome.TIMEOUT if error.reason is JevErrorReason.TIMEOUT else Outcome.UNAVAILABLE)
                        return await message.reply("Не получилось разобрать просьбу. Попробуй ещё раз или используй команду.\n\n" + HELP)
                    observation.intent_result(
                        decision.command, decision.confidence, decision.input_tokens, decision.output_tokens, decision.cost
                    )

            if decision.command == "none":
                return await message.reply("Не нашёл подходящую задачу. Попроси об одном действии.\n\n" + HELP)
            if decision.confidence < self.confidence:
                return await message.reply("Не уверен, что понял просьбу. Уточни её или выбери команду.\n\n" + HELP)
            media = {
                "pdf": document,
                "text": image,
                "bg": image,
                "song": audio or video,
                "anime": image,
            }[decision.command]
            if media is None:
                return await message.reply("Для этой задачи нужно другое вложение: " + COMMANDS[decision.command][1] + ".")
            if (media.file_size or 0) > MAX_DOWNLOAD_BYTES:
                return await message.reply("Для этой задачи пришли вложение до 20 МБ.")

            handler_key = COMMANDS[decision.command][0]
            with (
                self.telemetry.context(handler=handler_key, command=decision.command, command_kind="mention"),
                self.telemetry.operation(Boundary.DISPATCH, "intent.execute") as execution,
            ):
                try:
                    result = await self._execute(decision.command, message, bot, cpu_executor)
                except MissingIntegration:
                    execution.set_outcome(Outcome.UNAVAILABLE)
                    return await message.reply("Эта возможность сейчас недоступна. Попробуй позже.")
                if result is True:
                    execution.set_outcome(Outcome.UNAVAILABLE)
                    return await message.reply("Не получилось обработать вложение. Попробуй прислать его ещё раз.")
                return result
        finally:
            self._active.discard(user_id)

    @staticmethod
    async def _execute(command: str, message: Message, bot: Bot, cpu_executor: TPExecutor) -> CommandResult:
        meta = MetaInfo(message=message, command=command)
        match command:
            case "pdf":
                return cast(CommandResult, await invoke_command(process_topdf, message, meta=meta, bot=bot))
            case "text":
                return await process_image_to_text(message, meta, cpu_executor)
            case "bg":
                return await process_bg(message, cpu_executor)
            case "song":
                return await process_song(message, bot, cpu_executor)
            case "anime":
                return await process_which_anime(message)
        raise ValueError("Unsupported intent command")


async def process_intent(
    message: Message, intent_request: str, bot: Bot, cpu_executor: TPExecutor, intents: IntentCommands
) -> CommandResult:
    return await intents.handle(message, intent_request, bot, cpu_executor)
