from aiogram import html
from msu_hub_bot.settings import settings, MissingIntegration

import io
from contextlib import suppress
from typing import Any, cast

from aiogram import Bot
from aiogram.types import Audio, LinkPreviewOptions, Message, Video, VideoNote, Voice
from aiogram.utils.markdown import hcode, hitalic, hbold, hide_link, hlink

from common.executor import TPExecutor
from common import json
from common.externals.ydl import YDL
from common.tg.files import download
from common.utils import megabytes, prettify_duration


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


def quote_html_dict(d: dict[str, Any]) -> dict[str, Any]:
    for k, v in d.items():
        if isinstance(v, dict):
            d[k] = quote_html_dict(v)
        elif isinstance(v, list):
            for i in range(len(v)):
                if isinstance(v[i], dict):
                    v[i] = quote_html_dict(v[i])
                else:
                    v[i] = html.quote(v[i])
        elif isinstance(v, str):
            d[k] = html.quote(v)
    return d


async def process_song(message: Message, bot: Bot, cpu_executor: TPExecutor) -> Message | bool:
    target = message
    dest: Voice | VideoNote | Audio | Video | None = target.voice or target.video_note

    if dest is None:
        if message.reply_to_message:
            target = message.reply_to_message
            dest = target.voice or target.video_note or target.audio or target.video

    if dest is None or (dest.file_size or 0) > megabytes(20):
        return True

    m = await target.reply(hitalic("🔄 Ожидание..."))
    file = await download(dest, bot)
    if file is None:
        return await m.edit_text(hitalic("Не удалось распознать песню 😔"))

    text, timeouted = await cpu_executor.run(recognize_song, file)
    if timeouted:
        return await m.edit_text(hcode("🤷🏻‍♂️ Timeout"))

    result = json.loads(text)

    if result["status"]["code"] != 0:
        return await m.edit_text(hitalic("Не удалось распознать песню 😔"))

    info = result["metadata"]["music"][0]
    text = "🎙 " + hcode(", ".join(a["name"] for a in info["artists"]) + " — " + info["title"]) + "\n\n"

    with suppress(LookupError):
        if album := info.get("album"):
            text += hbold("Альбом") + ": " + hitalic(album.get("name", 1)) + "\n"
        if genres := info.get("genres"):
            text += hbold("Жанр") + ": " + ", ".join(hitalic(a["name"]) for a in genres) + "\n"
        if duration_ms := info.get("duration_ms"):
            text += hbold("Длительность") + ": " + hitalic(prettify_duration(duration_ms // 1000)) + "\n"
        if release_date := info.get("release_date"):
            text += hbold("Дата выпуска") + ": " + hitalic(release_date) + "\n"

        text += "\n"

        if external := info.get("external_metadata"):
            links = []
            if ext := external.get("youtube"):
                link = ("https://www.youtube.com/watch?v=" + ext["vid"], "YouTube")
                links.append(link)
            if ext := external.get("spotify"):
                link = ("https://open.spotify.com/track/" + ext["track"]["id"], "Spotify")
                links.append(link)
            if ext := external.get("deezer"):
                link = ("https://www.deezer.com/track/" + ext["track"]["id"], "Deezer")
                links.append(link)

            if links:
                text += hbold("Слушать") + ": " + ", ".join(hlink(title, url) for url, title in links) + "\n"

    await m.edit_text(text, link_preview_options=LinkPreviewOptions(is_disabled=True))

    if external := info.get("external_metadata"):
        if ext := external.get("youtube"):
            url = "https://www.youtube.com/watch?v=" + ext["vid"]
            preview, _ = await cpu_executor.run(YDL.preview, url)
            if preview:
                await m.edit_text(hide_link(preview) + text, link_preview_options=LinkPreviewOptions(is_disabled=False))

    return m
