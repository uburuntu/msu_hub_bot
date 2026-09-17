from __future__ import annotations

import re
from collections import defaultdict
from enum import IntEnum, auto
from operator import itemgetter
from pathlib import Path
from typing import Final, List, Tuple, Iterable

from jinja2 import Environment, FileSystemLoader

from msu_hub_bot.utils import PriorityQueue, megabytes, prettify_bytes, prettify_duration
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.providers.vk.utils import escape_symbols as e, href, prepare_vk_text

env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=True,
    auto_reload=False,
)
env.filters["prepare_vk_text"] = prepare_vk_text

post_template = env.get_template("post.html")


class VkPost:
    """
    VK Doc: https://vk.com/dev/objects/post
    """

    pattern_vk_post = re.compile(r"(?:^|[\s])(?:http[s]?://)?(?:m.)?vk\.com/(?:[\w\d_]+\?w=)?wall(-?[0-9]+_[0-9]+)", re.U)

    def __init__(self, post: dict, extended: dict):
        self.post = post
        self.extended = extended

        self.is_repost = "copy_history" in self.post
        self.repost = self.is_repost and VkPost(self.post["copy_history"][0], extended)

        self.attachments, self.photos_urls, self.gifs_urls, self.web_preview_pq = self.attachments_handle()

    @property
    def id(self) -> int:
        return self.post["id"]

    @property
    def owner_id(self) -> int:
        return self.post["owner_id"]

    @property
    def date(self) -> int:
        return self.post["date"]

    @property
    def text(self) -> str:
        return self.post["text"]

    @property
    def body_text(self) -> str:
        return self.repost.text if self.is_repost else self.text

    @property
    def url(self) -> str:
        return f"https://vk.com/wall{self.owner_id}_{self.id}"

    def _get_url(self, uid: int) -> str:
        screen_name = self.extended[uid].get("screen_name", "")
        return f"https://vk.com/{screen_name}"

    def _get_name(self, uid: int) -> str:
        entity = self.extended[uid]
        if uid > 0:
            return entity["first_name"] + " " + entity["last_name"]
        return entity["name"]

    @property
    def owner_url(self) -> str:
        return self._get_url(self.owner_id)

    @property
    def owner_name(self) -> str:
        return self._get_name(self.owner_id)

    def header(self, with_header: bool) -> str:
        if self.is_repost:
            source = "пользователя" if self.repost.owner_id > 0 else "из группы"
            return f"📢 {href(self.url, 'Репост')} {source} {href(self.repost.owner_url, e(self.repost.owner_name))}:"
        if with_header:
            source = "пользователя" if self.owner_id > 0 else "в группе"
            return f"📋 {href(self.url, 'Пост')} {source} {href(self.owner_url, e(self.owner_name))}:"
        return ""

    @classmethod
    async def from_api_by_id(cls, api: VkApi, posts: str) -> List[VkPost]:
        # Returns list of VkPost objects by list of {owner_id}_{post_id} ids joined by comma
        items, extended = await api.get_wall_post(posts)
        return [cls(item, extended) for item in items]

    @classmethod
    async def from_api_wall(cls, api: VkApi, owner_id: int, count: int = None) -> List[VkPost]:
        items, extended = await api.get_wall(owner_id, count)
        return [cls(item, extended) for item in items]

    @classmethod
    async def from_api_wall_last_post(cls, api: VkApi, owner_id: int) -> VkPost:
        # Skips pinned post and returns last post
        items, extended = await api.get_wall(owner_id, count=2)
        return sorted([cls(item, extended) for item in items], key=lambda x: x.date, reverse=True)[0]

    @classmethod
    async def from_api_newsfeed(cls, api: VkApi, owner_ids: Iterable[int], from_ts: int = 0) -> List[VkPost]:
        items, extended = await api.get_newsfeed(owner_ids, from_ts)
        for d in items:
            d["id"] = d["post_id"]
            d["owner_id"] = d["source_id"]
        return [cls(item, extended) for item in items]

    @classmethod
    def from_response(cls, response: dict) -> List[VkPost]:
        extended = VkApi.extract_extended(response)
        posts = [cls(item, extended) for item in response["items"]]
        return posts

    @staticmethod
    def postprocess_text(text: str) -> str:
        return text.strip().replace("\n ", "\n")

    def render(self, with_header: bool = True) -> str:
        result = post_template.render(
            post=self,
            with_header=with_header,
        )
        return self.postprocess_text(result)

    def for_publish(self, with_header: bool = True, with_webpreview: bool = True) -> Tuple[str, str, list, list]:
        max_media_count: Final[int] = 10

        text = self.render(with_header)
        photos_urls, gifs_urls = self.photos_urls[:max_media_count], self.gifs_urls[:max_media_count]

        if not with_webpreview:
            return text, "", photos_urls, gifs_urls

        if text:
            if len(photos_urls) + len(gifs_urls) == 1:
                url = photos_urls[0] if photos_urls else gifs_urls[0]
                self.web_preview_pq.put(self.PreviewPriority.media, url)
                photos_urls, gifs_urls = [], []

        web_preview, priority = self.web_preview_pq.head_with_priority(default="")
        if priority > self.PreviewPriority.low_priority:
            # Exclude low-priority previews if have media
            if len(photos_urls) + len(gifs_urls) > 1:
                web_preview = ""

        return text, web_preview, photos_urls, gifs_urls

    class PreviewPriority(IntEnum):
        media = auto()
        link = auto()
        video = auto()
        docs = auto()
        low_priority = auto()
        link_photo = auto()
        album_photo = auto()
        market_photo = auto()
        market_album_photo = auto()
        poll_photo = auto()

    def attachments_handle(self):
        """
        VK Doc: https://vk.com/dev/objects/attachments_w
        """
        attachments_raw = self.post.get("copy_history", [{}])[0].get("attachments", []) + self.post.get("attachments", [])

        attachments_by_type = defaultdict(list)
        for attachment in attachments_raw:
            attachments_by_type[attachment["type"]].append(attachment[attachment["type"]])

        result_text, web_preview_pq = "", PriorityQueue()
        photos_urls, gifs_urls = [], []

        if attachments := attachments_by_type.get("photo"):
            photo_sizes_priority = dict(z=2, y=1, x=3, m=4, s=5, r=6, q=7, p=8, o=9)
            for attachment in attachments:
                url = min(attachment["sizes"], key=lambda x: photo_sizes_priority.get(x["type"], 10))["url"]
                photos_urls.append(url)

        for attachment_type in ("posted_photo", "graffiti", "app"):
            if attachments := attachments_by_type.get(attachment_type):
                for attachment in attachments:
                    url = None
                    for k, v in attachment.items():
                        if k.startswith("photo_"):
                            url = v
                    if url:
                        photos_urls.append(url)

        if attachments := attachments_by_type.get("video"):
            result_text += "\n— Видео:\n"
            for attachment in attachments:
                if "player" in attachment:
                    url = attachment["player"]
                else:
                    owner_id, video_id = attachment["owner_id"], attachment["id"]
                    url = f"https://vk.com/video{owner_id}_{video_id}"
                title, duration = attachment["title"], prettify_duration(attachment["duration"])
                result_text += f"{href(url, title)}, {duration}\n"
                web_preview_pq.put(self.PreviewPriority.video, url)

        if attachments := attachments_by_type.get("audio"):
            result_text += "\n— Аудио:\n"
            for attachment in attachments:
                artist, title = attachment["artist"], attachment["title"]
                result_text += f"{e(artist)} — {e(title)}\n"

        if attachments := attachments_by_type.get("doc"):
            doc_text = "\n— Приложени" + ("я" if len(attachments) > 1 else "е") + ":\n"
            for attachment in attachments:
                url, title, size = attachment["url"], attachment["title"], prettify_bytes(attachment["size"])
                if attachment["ext"] in ("gif", "mp4") and attachment["size"] < megabytes(20):
                    gifs_urls.append(url)
                elif attachment["ext"] in ("jpg", "jpeg", "png") and attachment["size"] < megabytes(5):
                    photos_urls.append(url)
                else:
                    doc_text += f"{href(url, title)}, {size}\n"
                    web_preview_pq.put(self.PreviewPriority.docs, url)
            result_text += doc_text if doc_text.count("\n") > 2 else ""

        if attachments := attachments_by_type.get("link"):
            result_text += "\n— Ссылк" + ("и" if len(attachments) > 1 else "а") + ":\n"
            for attachment in attachments:
                url, title = attachment["url"].replace("https://m.vk.com", "https://vk.com"), attachment["title"]
                result_text += f"{href(url, title)}\n"
                if photo := attachment.get("photo"):
                    photo_url = max(photo["sizes"], key=itemgetter("width")).get("url")
                    web_preview_pq.put(self.PreviewPriority.link_photo, photo_url)
                web_preview_pq.put(self.PreviewPriority.link, url)

        if attachments := attachments_by_type.get("note"):
            result_text += "\n— Заметк" + ("и" if len(attachments) > 1 else "а") + ":\n"
            for attachment in attachments:
                url, title = attachment["view_url"], attachment["title"]
                result_text += f"{href(url, title)}\n"

        if attachments := attachments_by_type.get("poll"):
            result_text += "\n— Опрос" + ("ы" if len(attachments) > 1 else "") + ":\n"
            for attachment in attachments:
                owner_id, poll_id = attachment["owner_id"], attachment["id"]
                question, votes = attachment["question"], attachment["votes"]
                url = f"https://vk.com/poll{owner_id}_{poll_id}"
                result_text += f"{href(url, question)}, голосов: {votes}\n"
                for answer in attachment["answers"]:
                    text, votes = answer["text"], answer["votes"]
                    result_text += f"  → {e(text)}, голосов: {votes}\n"

                if photo := attachment.get("photo"):
                    photo_url = max(photo["images"], key=itemgetter("width")).get("url")
                    web_preview_pq.put(self.PreviewPriority.poll_photo, photo_url)

        if attachments := attachments_by_type.get("page"):
            result_text += "\n— Вики-страниц" + ("ы" if len(attachments) > 1 else "а") + ":\n"
            for attachment in attachments:
                url, title = attachment["view_url"], attachment["title"]
                result_text += f"{href(url, title)}\n"

        if attachments := attachments_by_type.get("album"):
            result_text += "\n— Альбом" + ("ы" if len(attachments) > 1 else "") + ":\n"
            for attachment in attachments:
                owner_id, album_id = attachment["owner_id"], attachment["id"]
                url = f"https://vk.com/album{owner_id}_{album_id}"
                title, size = attachment["title"], attachment["size"]
                result_text += f"{href(url, title)}, {size} фото\n"
                if photo := attachment.get("thumb"):
                    photo_url = max(photo["sizes"], key=itemgetter("width")).get("url")
                    web_preview_pq.put(self.PreviewPriority.album_photo, photo_url)

        if _attachments := attachments_by_type.get("photos_list"):
            pass

        if attachments := attachments_by_type.get("market"):
            market_text = "\n— Товар" + ("ы" if len(attachments) > 1 else "") + ":\n"
            for attachment in attachments:
                if attachment is False:
                    continue
                title, price = attachment["title"], attachment["price"]["text"]
                owner_id, product_id = attachment["owner_id"], attachment["id"]
                url = f"https://vk.com/product{owner_id}_{product_id}"
                market_text += f"{href(url, title)}, {price}\n"
                if photo_url := attachment.get("thumb_photo"):
                    web_preview_pq.put(self.PreviewPriority.market_photo, photo_url)
            result_text += market_text if market_text.count("\n") > 2 else ""

        if attachments := attachments_by_type.get("market_album"):
            result_text += "\n— Подборк" + ("и" if len(attachments) > 1 else "а") + " товаров:\n"
            for attachment in attachments:
                title, count = attachment["title"], attachment["count"]
                owner_id, album_id = attachment["owner_id"], attachment["id"]
                url = f"https://vk.com/market{owner_id}?section=album_{album_id}"
                result_text += f"{href(url, title)}, {count} шт\n"
                if photo := attachment.get("photo"):
                    photo_url = max(photo["sizes"], key=itemgetter("width")).get("url")
                    web_preview_pq.put(self.PreviewPriority.market_album_photo, photo_url)

        if attachments := attachments_by_type.get("pretty_cards"):
            result_text += "\n— Карточки:\n"
            for attachment in attachments:
                for card in attachment:
                    url, title, price = card["link_url"], card["title"], card["price"]
                    result_text += f"{href(url, title)}, {price}\n"

        if attachments := attachments_by_type.get("event"):
            result_text += "\n— Встреч" + ("и" if len(attachments) > 1 else "а") + ":\n"
            for attachment in attachments:
                event_id = -attachment["id"]
                url, title = self._get_url(event_id), self._get_name(event_id)
                result_text += f"{href(url, title)}\n"

        if copy_right := self.post.get("copyright"):
            url, title = copy_right["link"], copy_right["name"]
            result_text += f"\n— Источник: {href(url, title)}\n"

        if signer_id := self.post.get("signer_id"):
            url, title = self._get_url(signer_id), self._get_name(signer_id)
            result_text += f"\n— Автор: {href(url, title)}\n"

        return result_text, photos_urls, gifs_urls, web_preview_pq
