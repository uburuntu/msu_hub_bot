from msu_hub_bot.settings import settings, MissingIntegration

import asyncio
import io
from contextlib import suppress
from typing import Any, cast

from aiogram import Bot
from aiogram.types import Audio, LinkPreviewOptions, Message, Video, VideoNote, Voice
from aiogram.utils.formatting import Bold, Code, Italic, Text, TextLink
from teleforge.delivery import CompletedResponse, DeliveryError, DeliveryProgress, complete_response, edit_response, send_response
from teleforge.formatting import ResponseError

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot import json
from msu_hub_bot.providers.ydl import YDL
from msu_hub_bot.telegram.media_jobs import DownloadUnavailable, run_downloaded
from msu_hub_bot.telemetry import record_handled_failure
from msu_hub_bot.utils import megabytes, prettify_duration


def recognize_song(audio: io.BytesIO) -> str:
    settings.require("acrcloud_host", "acrcloud_access_key", "acrcloud_access_secret")
    try:
        from acrcloud.recognizer import ACRCloudRecognizer
    except ImportError:
        raise MissingIntegration("acrcloud_linux_sdk") from None

    config = dict(
        host=settings.require("acrcloud_host"),
        access_key=settings.require("acrcloud_access_key"),
        access_secret=settings.require("acrcloud_access_secret"),
        timeout=10,
    )

    re = ACRCloudRecognizer(config)
    return cast(str, re.recognize_by_filebuffer(audio.read(), 0))


def _song_text(info: dict[str, Any]) -> Text:
    parts: list[Text | str] = ["🎙 ", Code(", ".join(a["name"] for a in info["artists"]) + " — " + info["title"]), "\n\n"]
    with suppress(LookupError):
        if album := info.get("album"):
            parts.extend([Bold("Альбом"), ": ", Italic(album.get("name", 1)), "\n"])
        if genres := info.get("genres"):
            parts.extend(
                [Bold("Жанр"), ": ", Text(*(Text(", " if index else "", Italic(item["name"])) for index, item in enumerate(genres))), "\n"]
            )
        if duration_ms := info.get("duration_ms"):
            parts.extend([Bold("Длительность"), ": ", Italic(prettify_duration(duration_ms // 1000)), "\n"])
        if release_date := info.get("release_date"):
            parts.extend([Bold("Дата выпуска"), ": ", Italic(release_date), "\n"])
        parts.append("\n")
        if external := info.get("external_metadata"):
            links = []
            for provider, prefix, label in (
                ("youtube", "https://www.youtube.com/watch?v=", "YouTube"),
                ("spotify", "https://open.spotify.com/track/", "Spotify"),
                ("deezer", "https://www.deezer.com/track/", "Deezer"),
            ):
                if ext := external.get(provider):
                    identifier = ext["vid"] if provider == "youtube" else ext["track"]["id"]
                    links.append(TextLink(label, url=prefix + identifier))
            if links:
                parts.extend([Bold("Слушать"), ": ", Text(*(Text(", " if index else "", link) for index, link in enumerate(links))), "\n"])
    return Text(*parts)


async def process_song(message: Message, bot: Bot, cpu_executor: TPExecutor) -> Message | bool:
    target = message
    dest: Voice | VideoNote | Audio | Video | None = target.voice or target.video_note
    if dest is None and message.reply_to_message:
        target = message.reply_to_message
        dest = target.voice or target.video_note or target.audio or target.video
    if dest is None or (dest.file_size or 0) > megabytes(20):
        return True

    status = await send_response(bot, target, Italic("🔄 Ожидание..."), fixed=True)
    assert isinstance(status, Message)
    progress = DeliveryProgress()
    completed: CompletedResponse | None = None
    try:
        info = None
        try:
            payload, timed_out = await run_downloaded(cpu_executor, dest, recognize_song, bot=bot)
        except DownloadUnavailable:
            payload, timed_out = None, False
        content: Text = Italic("Не удалось распознать песню 😔")
        if timed_out:
            content = Code("🤷🏻‍♂️ Timeout")
        elif payload is not None:
            result = json.loads(payload)
            if result["status"]["code"] == 0:
                info = result["metadata"]["music"][0]
                content = _song_text(info)
        completed = await complete_response(
            bot, status, content, overflow_to=target, overflow_notice="Готово — полная информация о песне в файле.", progress=progress
        )
        if completed.status_error is not None:
            record_handled_failure(completed.status_error)
        if not completed.spilled and info is not None:
            external = info.get("external_metadata", {})
            if youtube := external.get("youtube"):
                preview, _ = await cpu_executor.run(YDL.preview, "https://www.youtube.com/watch?v=" + youtube["vid"])
                if preview:
                    await edit_response(bot, status, content, link_preview_options=LinkPreviewOptions(is_disabled=False, url=preview))
        return completed.result
    except asyncio.CancelledError:
        if progress.phase != "complete" and not progress.uncertain:
            with suppress(DeliveryError, ResponseError):
                await edit_response(bot, status, "Распознавание отменено.")
        raise
    except (DeliveryError, ResponseError) as error:
        record_handled_failure(error)
        if completed is not None:
            return completed.result
        notice = "Не удалось подтвердить отправку. Результат мог уже прийти." if progress.uncertain else "Не удалось отправить результат."
        with suppress(DeliveryError, ResponseError):
            if progress.uncertain:
                guidance = await send_response(bot, target, notice, fixed=True)
                assert isinstance(guidance, Message)
                return guidance
            return await edit_response(bot, status, notice)
        raise
