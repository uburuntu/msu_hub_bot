"""Run inside the production image with Docker networking disabled."""

import asyncio
import importlib
import shutil
import socket


def blocked(*args, **kwargs):
    raise RuntimeError("Offline image check attempted network access")


socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked


async def main():
    for program in ("ffmpeg", "ffprobe", "tesseract"):
        assert shutil.which(program), program
    importlib.import_module("cv2")
    from acrcloud.recognizer import ACRCloudRecognizer

    ACRCloudRecognizer({"host": "example.invalid", "access_key": "test", "access_secret": "test", "timeout": 1})
    from msu_hub_bot.settings import settings
    from hub_bot.app import Application

    settings.bot_token = "123456789:" + "a" * 35
    settings.redis_host = "localhost"
    settings.edgedb_dsn = "edgedb://localhost/msu_hub"
    app = await Application.create(settings)
    try:
        def count(event):
            return sum(len(router.observers[event].handlers) for router in app.dispatcher.chain_tail)
        assert count("message") == 262
        assert count("callback_query") == 19
        assert count("edited_message") == 149
        from PIL import ImageFont
        from hub_bot.resources import times_new_roman_font

        assert ImageFont.truetype(str(times_new_roman_font), 24).getbbox("Привет, Ёж!")
    finally:
        await app.close()
    print("Linux image: native libraries, resources, handlers, and shutdown passed")


asyncio.run(main())
