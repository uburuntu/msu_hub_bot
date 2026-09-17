from contextlib import suppress
from itertools import chain
from operator import itemgetter
from typing import List, Tuple, Optional

import requests
from youtube_dl import YoutubeDL
from youtube_dl.utils import YoutubeDLError

from common.utils import megabytes
from common.vk.utils import href


class YDL:
    @classmethod
    def create_ydl(cls) -> YoutubeDL:
        ydl = YoutubeDL(
            {
                "quiet": True,
                "ignorerrors": True,
                "geo_bypass": True,
                "youtube_include_dash_manifest": False,
            }
        )
        ydl.report_error = lambda *a, **k: None
        ydl.report_warning = lambda *a, **k: None
        return ydl

    @classmethod
    def extract_data(cls, url: str, ydl: YoutubeDL = None) -> Optional[dict]:
        ydl = ydl or cls.create_ydl()
        with suppress(YoutubeDLError):
            return ydl.extract_info(url, download=False)

    @classmethod
    def extract(cls, real_url: str, ydl: YoutubeDL = None) -> Optional[Tuple[str, List[Tuple[str, str]], Optional[str]]]:
        info = cls.extract_data(real_url, ydl)
        if not info:
            return

        links = []
        extractor = info["extractor"].lower()

        if extractor in ("generic", "yandexmusic"):
            return

        elif extractor == "youtube":
            formats = list(filter(lambda x: "m3u8" not in x["protocol"], info["formats"]))
            formats = list(filter(lambda x: x["acodec"] != "none", formats))
            formats_a = list(filter(lambda x: x["vcodec"] == "none", formats))
            format_a = [max(formats_a, key=itemgetter("asr"))] if formats_a else []
            formats_v = list(filter(lambda x: x["vcodec"] != "none", formats))
            info["formats"] = list(chain(formats_v, format_a))
            for f in info["formats"]:
                url = f.get("fragment_base_url", f["url"])
                title = f["format"].partition(" - ")[2]
                width, height = f.get("width"), f.get("height")
                links.append((url, title, width, height))

        elif extractor == "vk":
            formats = list(filter(lambda x: x["format_id"].startswith("cache"), info["formats"]))
            info["formats"] = formats
            for f in sorted(info["formats"], key=lambda x: x.get("height", 0) or 0, reverse=True):
                url = f["url"]
                title = f["format"].partition(" - ")[2]
                width, height = f.get("width"), f.get("height")
                links.append((url, title, width, height))

        else:
            if "formats" in info:
                formats = list(filter(lambda x: "m3u8" not in x["protocol"], info["formats"]))
                info["formats"] = formats
                for f in info["formats"]:
                    url = f["url"]
                    title = f["format"].partition(" - ")[2]
                    width, height = f.get("width"), f.get("height")
                    links.append((url, title, width, height))
            else:
                if "url" not in info:
                    return
                url, title = info["url"], info["title"]
                width, height = info.get("width"), info.get("height")
                links.append((url, title, width, height))

        if len(links) == 0:
            return

        title = info.get("title", "Video")
        links, preview = cls.post_process_links(links)
        return title, links, preview

    @classmethod
    def preview(cls, url: str) -> Optional[str]:
        result = cls.extract(url)
        return result and result[2]

    @classmethod
    def post_process_links(cls, links):
        headers = {url: requests.head(url).headers for url, *_ in links}

        links_sizes = [int(h.get("Content-Length", 0)) for h in headers.values()]
        temp = sorted(zip(links_sizes, links), key=itemgetter(0), reverse=True)

        previews = [
            (url, width, height)
            for size, (url, title, width, height) in temp
            if size < megabytes(20) and headers[url].get("Content-Type") == "video/mp4"
        ]

        return [x for _, x in temp], previews[0] if previews else None

    @classmethod
    def text_with_preview(cls, url: str) -> Optional[Tuple[str, str]]:
        result = cls.extract(url)

        if result is None:
            return None

        title, links, preview = result
        text = "🎞 "
        if preview:
            text = href(preview[0], "📺") + " "
        text += href(url, title) + "\n\n— " + ", ".join(href(url, title) for url, title, _, _ in links)

        return text, preview
