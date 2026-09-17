"""Run inside the production image with Docker networking disabled."""

import asyncio
import csv
import importlib
import importlib.util
import shutil
import socket


def blocked(*args, **kwargs):
    raise RuntimeError("Offline image check attempted network access")


socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked


async def main():
    for package in ("common", "hub_bot"):
        assert importlib.util.find_spec(package) is None, f"Obsolete package is installed: {package}"
    for program in ("ffmpeg", "ffprobe", "tesseract"):
        assert shutil.which(program), program
    importlib.import_module("cv2")
    from acrcloud.recognizer import ACRCloudRecognizer

    ACRCloudRecognizer({"host": "example.invalid", "access_key": "test", "access_secret": "test", "timeout": 1})
    from msu_hub_bot.settings import settings
    from msu_hub_bot.app import Application

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
        from msu_hub_bot.execution.sed import sed_calc
        from msu_hub_bot.providers.vk.posts import VkPost
        from msu_hub_bot.resources import debate, lobster_font, times_new_roman_font, ubuntu_mono_font

        for font in (lobster_font, times_new_roman_font, ubuntu_mono_font):
            assert ImageFont.truetype(str(font), 24).getbbox("Привет, Ёж!"), font.name
        with debate.open(encoding="utf-8", newline="") as source:
            rows = csv.reader(source, delimiter=";")
            assert len(next(rows)) == 7
            row = next(rows)
            assert len(row) == 7 and row[-1].strip()
        post = VkPost({"id": 1, "owner_id": -1, "date": 0, "text": "Текст <example> &", "attachments": []}, {})
        assert post.render(with_header=False) == "Текст &lt;example&gt; &amp;"
        assert await asyncio.to_thread(sed_calc, "Привет, кот!", ["s/кот/бот/"]) == "Привет, бот!"
    finally:
        await app.close()
    print("Linux image: native libraries, installed resources, isolated worker, handlers, and shutdown passed")


asyncio.run(main())
