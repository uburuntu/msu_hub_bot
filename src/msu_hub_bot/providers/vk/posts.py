"""Render the supported wall subset; unavailable objects retain their source link."""

from __future__ import annotations

import re
from collections import defaultdict
from html import escape

from pydantic import ValidationError

from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.providers.vk.models import (
    Album,
    Audio,
    Cards,
    Document,
    Entity,
    Event,
    Image,
    Link,
    Market,
    MarketAlbum,
    Page,
    Photo,
    Poll,
    Post,
    Video,
    Wall,
)
from msu_hub_bot.providers.vk.utils import bounded_html, href, prepare_vk_text, safe_url
from msu_hub_bot.utils import prettify_bytes, prettify_duration


def best_photo(photo: Photo | None) -> str:
    if photo is None:
        return ""
    images = photo.sizes + photo.images + ([photo.orig_photo] if photo.orig_photo else [])
    return best_image(images)


def best_image(images: list[Image]) -> str:
    candidates = [
        image
        for image in images
        if safe_url(image.url or image.src, media=True)
        and image.width + image.height <= 10_000
        and (not image.width or not image.height or max(image.width, image.height) <= 20 * min(image.width, image.height))
    ]
    if not candidates:
        return ""
    image = max(candidates, key=lambda item: item.width * item.height)
    return image.url or image.src


class VkPost:
    pattern_vk_post = re.compile(
        r"(?:^|\s)(?:https?://)?(?:m\.|www\.)?vk\.(?:com|ru)/(?:[\w]+\?w=)?wall(-?[1-9][0-9]*_[1-9][0-9]*)(?![0-9])", re.U
    )

    def __init__(self, post: Post | dict[str, object], extended: dict[int, Entity] | None = None) -> None:
        self.post = Post.model_validate(post)
        self.extended = extended or {}
        self.repost = VkPost(self.post.copy_history[0], self.extended) if self.post.copy_history else None
        self.is_repost = self.repost is not None
        self.attachments, self.photos_urls, self.gifs_urls, self.previews = self.attachments_handle()

    @property
    def id(self) -> int:
        return self.post.id

    @property
    def owner_id(self) -> int:
        return self.post.owner_id

    @property
    def date(self) -> int:
        return self.post.date

    @property
    def text(self) -> str:
        return self.post.text

    @property
    def body_text(self) -> str:
        return self.repost.text if self.repost else self.text

    @property
    def url(self) -> str:
        return f"https://vk.com/wall{self.owner_id}_{self.id}"

    @staticmethod
    def _get_url(uid: int) -> str:
        return f"https://vk.com/{'id' if uid > 0 else 'club'}{abs(uid)}"

    def _get_name(self, uid: int) -> str:
        entity = self.extended.get(uid)
        if entity:
            name = entity.name if uid < 0 else f"{entity.first_name} {entity.last_name}".strip()
            if name:
                return name
        return ("Пользователь " if uid > 0 else "Группа ") + str(abs(uid))

    @property
    def owner_url(self) -> str:
        return self._get_url(self.owner_id)

    @property
    def owner_name(self) -> str:
        return self._get_name(self.owner_id)

    def header(self, with_header: bool) -> str:
        if self.repost:
            source = "пользователя" if self.repost.owner_id > 0 else "из группы"
            return f"📢 {href(self.url, 'Репост')} {source} {href(self.repost.owner_url, self.repost.owner_name)}:"
        if with_header:
            source = "пользователя" if self.owner_id > 0 else "в группе"
            return f"📋 {href(self.url, 'Пост')} {source} {href(self.owner_url, self.owner_name)}:"
        return ""

    @classmethod
    async def from_api_by_id(cls, api: VkApi, posts: str) -> list[VkPost]:
        items, extended = await api.get_wall_post(posts)
        return [cls(item, extended) for item in items]

    @classmethod
    async def from_api_wall(cls, api: VkApi, owner_id: int, count: int | None = None) -> list[VkPost]:
        items, extended = await api.get_wall(owner_id, count)
        return [cls(item, extended) for item in items]

    @classmethod
    def from_response(cls, response: object) -> list[VkPost]:
        """Offline parsing; live callers must use the API's public-source checks."""
        wall = Wall.model_validate(response)
        return [cls(item, wall.extended) for item in wall.items]

    def render(self, with_header: bool = True) -> str:
        parts = [prepare_vk_text(self.text)] if self.repost and self.text else []
        parts.extend([self.header(with_header), prepare_vk_text(self.body_text), self.attachments])
        text = "\n\n".join(part.strip() for part in parts if part.strip())
        return bounded_html(text or href(self.url, "Открыть запись в VK"), self.url)

    def for_publish(self, with_header: bool = True, with_webpreview: bool = True) -> tuple[str, str, list[str], list[str]]:
        text = self.render(with_header)
        photos = list(dict.fromkeys(self.photos_urls))[:10]
        videos = list(dict.fromkeys(self.gifs_urls))[: 10 - len(photos)]
        if not with_webpreview:
            return text, "", photos, videos
        if len(photos) + len(videos) == 1:
            return text, (photos or videos)[0], [], []
        priority, preview = min(self.previews, default=(99, ""))
        if priority > 5 and photos + videos:
            preview = ""
        return text, preview, photos, videos

    def attachments_handle(self) -> tuple[str, list[str], list[str], list[tuple[int, str]]]:
        groups: dict[str, list[str]] = defaultdict(list)
        photos: list[str] = []
        videos: list[str] = []
        previews: list[tuple[int, str]] = []
        unsupported = len(self.post.copy_history) > 1 or bool(self.repost and self.repost.post.copy_history)
        raw_items = (self.repost.post.attachments if self.repost else []) + self.post.attachments

        def preview(priority: int, url: str) -> None:
            if valid := safe_url(url):
                previews.append((priority, valid))

        for raw in raw_items:
            if not isinstance(raw, dict):
                unsupported = True
                continue
            kind = raw.get("type")
            data = raw.get(kind) if isinstance(kind, str) else None
            try:
                match kind:
                    case "photo":
                        url = best_photo(Photo.model_validate(data))
                        if url:
                            photos.append(url)
                        else:
                            unsupported = True
                    case "posted_photo" | "graffiti" | "app":
                        if not isinstance(data, dict):
                            unsupported = True
                            continue
                        candidates = [
                            (int(k.removeprefix("photo_")), v)
                            for k, v in data.items()
                            if isinstance(k, str) and k.removeprefix("photo_").isdigit() and isinstance(v, str) and safe_url(v, media=True)
                        ]
                        if candidates:
                            photos.append(max(candidates)[1])
                        else:
                            unsupported = True
                    case "video" | "clip":
                        video = Video.model_validate(data)
                        if video.is_private:
                            unsupported = True
                            continue
                        url = f"https://vk.com/{kind}{video.owner_id}_{video.id}"
                        groups["Видео"].append(f"{href(url, video.title)}, {prettify_duration(video.duration)}")
                        preview(3, url)
                    case "audio":
                        audio = Audio.model_validate(data)
                        groups["Аудио"].append(f"{escape(audio.artist)} — {escape(audio.title)}")
                    case "doc":
                        doc = Document.model_validate(data)
                        url = safe_url(doc.url, media=True)
                        if not doc.is_unsafe and url and doc.ext.lower() in {"gif", "mp4"} and 0 < doc.size < 20 * 1024 * 1024:
                            videos.append(url)
                        elif not doc.is_unsafe and url and doc.ext.lower() in {"jpg", "jpeg", "png"} and 0 < doc.size < 5 * 1024 * 1024:
                            photos.append(url)
                        else:
                            url = f"https://vk.com/doc{doc.owner_id}_{doc.id}"
                            groups["Приложения"].append(f"{href(url, doc.title)}, {prettify_bytes(doc.size)}")
                            preview(4, url)
                    case "link":
                        link = Link.model_validate(data)
                        groups["Ссылки"].append(href(link.url, link.title))
                        preview(2, link.url)
                        preview(6, best_photo(link.photo))
                    case "note" | "page":
                        page = Page.model_validate(data)
                        groups["Заметки" if kind == "note" else "Вики-страницы"].append(href(page.view_url, page.title))
                    case "poll":
                        poll = Poll.model_validate(data)
                        url = f"https://vk.com/poll{poll.owner_id}_{poll.id}"
                        lines = [f"{href(url, poll.question)}, голосов: {poll.votes}"]
                        lines.extend(f"  → {escape(answer.text)}, голосов: {answer.votes}" for answer in poll.answers)
                        groups["Опросы"].append("\n".join(lines))
                        preview(10, best_photo(poll.photo))
                    case "album":
                        album = Album.model_validate(data)
                        groups["Альбомы"].append(
                            f"{href(f'https://vk.com/album{album.owner_id}_{album.id}', album.title)}, {album.size} фото"
                        )
                        preview(7, best_photo(album.thumb))
                    case "market":
                        market = Market.model_validate(data)
                        groups["Товары"].append(
                            f"{href(f'https://vk.com/product{market.owner_id}_{market.id}', market.title)}, {escape(market.price.text)}"
                        )
                        preview(8, safe_url(market.thumb_photo, media=True))
                    case "market_album":
                        collection = MarketAlbum.model_validate(data)
                        url = f"https://vk.com/market{collection.owner_id}?section=album_{collection.id}"
                        groups["Подборки товаров"].append(f"{href(url, collection.title)}, {collection.count} шт")
                        preview(9, best_photo(collection.photo))
                    case "pretty_cards":
                        cards = Cards.model_validate(data)
                        groups["Карточки"].extend(f"{href(card.link_url, card.title)}, {escape(card.price)}" for card in cards.cards)
                    case "event":
                        event = Event.model_validate(data)
                        groups["Встречи"].append(href(self._get_url(-event.id), self._get_name(-event.id)))
                    case _:
                        unsupported = True
            except ValidationError, ValueError, TypeError:
                unsupported = True
        result = "\n\n".join(f"— {title}:\n" + "\n".join(lines) for title, lines in groups.items())
        if copyright := self.post.copyright:
            result += "\n\n— Источник: " + href(copyright.link, copyright.name)
        if signer := self.post.signer_id:
            result += "\n\n— Автор: " + href(self._get_url(signer), self._get_name(signer))
        if unsupported or len(photos) + len(videos) > 10:
            result += "\n\n" + href(self.url, "Все вложения — в VK →")
        return result, photos, videos, previews
