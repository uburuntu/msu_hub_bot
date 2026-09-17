import re
from typing import Optional, Tuple, Union

import aiohttp
from aiogram.utils.markdown import hbold, hlink
from yarl import URL

from msu_hub_bot import json
from msu_hub_bot.utils import prettify_number


def lookup(d, *keys, default=None):
    try:
        for key in keys:
            d = d[key]
        return d
    except LookupError:
        return default


class InstagramViewer:
    @classmethod
    async def request(cls, url: str) -> Optional[dict]:
        url = URL(url).update_query(__a="1")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0",
            "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                result = await response.json(loads=json.loads)

        return result

    @classmethod
    def parse_image_video(cls, d: dict) -> Tuple[str, str]:
        if d["is_video"]:
            url = d["video_url"]
        else:
            url = d["display_resources"][-1]["src"]

        text = ""
        # if c := d['accessibility_caption']:
        #     text += c.partition('. ')[2] + '\n'

        return url, text

    @classmethod
    def parse_user(cls, d: dict):
        def usernames(s: str) -> str:
            def repl(match):
                return hlink(f"@{match.group(1)}", f"https://www.instagram.com/{match.group(1)}/")

            return re.sub(r"@(\S+\w)", repl, s)

        url = d.get("profile_pic_url_hd", d["profile_pic_url"])
        prefix = d["username"]

        link = hlink(f"@{d['username']}", f"https://www.instagram.com/{d['username']}/")
        caption = hbold(f"{d['full_name']}") + f", {link}\n\n"
        if biography := d.get("biography"):
            caption += f"{usernames(biography)}\n\n"
        if external_url := d.get("external_url"):
            caption += f"{usernames(external_url)}\n\n"
        caption += f"{hbold('Подписчиков')}: {prettify_number(d['edge_followed_by']['count'])}, "
        caption += f"{hbold('подписок')}: {prettify_number(d['edge_follow']['count'])}, "
        caption += f"{hbold('постов')}: {prettify_number(d['edge_owner_to_timeline_media']['count'])}"

        return [(url, caption)], prefix

    @classmethod
    async def links(cls, url: Union[str, URL]):
        d = await cls.request(url)
        if not d:
            return None

        if user := lookup(d, "graphql", "user"):
            return cls.parse_user(user)

        d = lookup(d, "graphql", "shortcode_media")
        if not d:
            return None

        pairs = []

        if d["__typename"] == "GraphSidecar":
            for edge in d["edge_sidecar_to_children"]["edges"]:
                pairs.append(cls.parse_image_video(edge["node"]))
        else:
            pairs.append(cls.parse_image_video(d))

        if not pairs:
            return None

        owner = d["owner"]
        link = hlink(f"@{owner['username']}", f"https://www.instagram.com/{owner['username']}/")
        caption = hbold(f"{owner['full_name']}") + ", " + link + "\n\n"
        # if c := lookup(d, 'edge_media_to_caption', 'edges', 0, 'node', 'text'):
        #     caption += hitalic(shorten(one_liner(c), width=260, placeholder=' [...] ')) + '\n\n'
        pairs[-1] = (pairs[-1][0], pairs[-1][1] + "\n" + caption)
        # pairs[0] = (pairs[0][0], caption + pairs[0][1])

        return pairs, owner["username"]
