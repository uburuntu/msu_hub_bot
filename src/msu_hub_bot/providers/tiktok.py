from typing import Optional, Tuple

import httpx
from msu_hub_bot.providers.vk.utils import href


async def tiktok_text_with_preview(url: str) -> Optional[Tuple[str, str]]:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://7wzlye.deta.dev/tt?url={url}")

    result = response.json()

    if result["status"] != "success":
        return

    if result["type"] != "video":
        return

    title = result["desc"]
    preview = result["video_data"]["nwm_video_url_HQ"]

    text = href(preview, "📺") + " " + href(url, title)
    return text, preview
