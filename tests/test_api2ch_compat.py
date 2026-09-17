import hashlib
import json
import sys
from datetime import timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pydantic
import pydantic.v1
import pytest

from msu_hub_bot.storage.models import UserRecord
from msu_hub_bot.providers import dvach
from msu_hub_bot.providers._api2ch.models.file import Image, Sticker, Video
from msu_hub_bot.providers._api2ch.models.response import ResponseThreadPostsHelper
from msu_hub_bot.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "src/msu_hub_bot/providers/_api2ch"


def post_payload(files=None):
    return {
        "subject": "<b>Synthetic</b>",
        "comment": "<strong>Bold</strong><br><em>Text</em>&nbsp;&quot;&#47;<span>tail</span>",
        "files": files or [],
        "timestamp": 0,
        "date": "Synthetic",
        "lasthit": 0,
        "num": 7,
        "parent": 0,
        "banned": 0,
        "closed": False,
        "op": True,
        "endless": False,
        "sticky": 0,
        "email": "",
        "name": "Synthetic",
        "trip": "",
    }


def thread_payload():
    result = dict.fromkeys(
        "BoardInfo BoardInfoOuter advert_bottom_image advert_bottom_link advert_top_image advert_top_link "
        "board_banner_image board_banner_link default_name thread_first_image title".split(),
        "",
    )
    result.update(
        dict.fromkeys(
            "enable_dices enable_flags enable_icons enable_likes enable_names enable_oekaki enable_posting "
            "enable_sage enable_shield enable_thread_tags enable_trips enable_images enable_video".split(),
            False,
        )
    )
    result.update(
        Board="b",
        BoardName="Synthetic",
        enable_subject=True,
        bump_limit=500,
        max_comment=1000,
        max_files_size=10,
        news_abu=[],
        top=[],
        current_thread="7",
        files_count=0,
        is_board=0,
        is_closed=0,
        is_index=0,
        max_num=7,
        posts_count=1,
        threads=[{"posts": [post_payload()]}],
        unique_posters="1",
    )
    return result


def board_payload():
    def thread(number, views):
        return dict(subject=f"Thread {number}", comment="", num=number, timestamp=0, lasthit=0, views=views, posts_count=2, score=1.0)

    return {"board": "b", "threads": [thread(7, 10), thread(8, 30), thread(9, 30)]}


def test_vendored_source_only_changes_imports_and_keeps_license():
    manifest = json.loads((ROOT / "tests/fixtures/api2ch_source.json").read_text())
    paths = {str(path.relative_to(SDK)): path for path in SDK.rglob("*.py")}
    assert manifest["version"] == "1.2.1"
    assert paths.keys() == manifest["files"].keys()
    for relative, path in paths.items():
        text = path.read_text()
        # Existing __init__ relative imports are upstream source, not a rewrite.
        if relative != "__init__.py":
            prefix = "." * len(Path(relative).parts)
            text = text.replace(f"from {prefix}", "from api2ch.")
        text = text.replace("from pydantic.v1 import", "from pydantic import")
        assert hashlib.sha256(text.encode()).hexdigest() == manifest["files"][relative], relative
    license_text = (SDK / "LICENSE").read_text()
    assert "Copyright (c) 2020 Ramzan Bekbulatov" in license_text
    assert "Permission is hereby granted, free of charge" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
    assert "src/msu_hub_bot/providers/_api2ch/LICENSE" in (ROOT / "THIRD_PARTY_NOTICES.md").read_text()


def test_legacy_models_are_isolated_from_pydantic_two_application_models():
    assert issubclass(dvach.Post, pydantic.v1.BaseModel)
    assert not issubclass(dvach.Post, pydantic.BaseModel)
    assert issubclass(Settings, pydantic.BaseModel)
    assert issubclass(UserRecord, pydantic.BaseModel)
    assert "api2ch" not in sys.modules
    post = dvach.Post.parse_obj(post_payload())
    assert post.api is None
    assert post.number is None
    assert ResponseThreadPostsHelper.parse_obj([post_payload()]).__root__[0].post_id == 7


def test_sdk_keeps_thread_order_html_and_post_links():
    board = dvach.ResponseThreads.parse_obj(board_payload())
    assert [item.thread_id for item in board.sorted_by_views()] == [8, 9, 7]
    thread = dvach.ResponseThread.parse_obj(thread_payload())
    assert (thread.board, thread.enable_subject) == ("b", True)
    post = thread.posts[0]
    assert post.header == "Synthetic"
    assert post.body == "<b>Bold</b>\n<i>Text</i> '/tail"
    assert post.url(thread.board) == "https://2ch.hk/b/res/7.html#7"
    assert post.dt(timezone.utc).year == 1970


@pytest.mark.parametrize("kind, expected", [(1, Image), (4, Image), (6, Video), (10, Video), (100, Sticker), (99, dvach.File)])
def test_sdk_keeps_file_kind_names_and_size_units(kind, expected):
    file = dict(
        name="stored.dat",
        type=kind,
        height=8,
        width=8,
        path="/b/src/stored.dat",
        size=2048,
        thumbnail="/b/thumb/stored.jpg",
        tn_height=8,
        tn_width=8,
        fullname="original.dat",
        displayname="original",
        md5="0" * 32,
        nsfw=False,
        install="",
        pack="",
        sticker="",
    )
    result = dvach.Post.parse_obj(post_payload([file])).files[0]
    assert type(result) is expected
    assert result.original_name == ("original.dat" if expected in (Image, Video) else "stored.dat")
    assert result.size_bytes == 2 * 1024 * 1024
    assert result.size_string == "2.00 Мб"


@pytest.mark.parametrize("host", ["2ch.hk", "2ch.pm", "2ch.re", "2ch.tf", "2ch.wf", "2ch.yt", "2-ch.so"])
def test_sdk_keeps_mirror_url_parsing(host):
    assert dvach.parse_url(f"https://{host}/b/res/7.html#8") == (True, "b", 7)
    assert dvach.parse_url(f"https://{host}/unknown-board/res/7.html") == (False, "", 0)


@pytest.mark.asyncio
async def test_sdk_requests_and_status_errors_use_original_protocol():
    class Response:
        def __init__(self, payload, status=200):
            self.payload = payload
            self.status = status
            self.reason = "Synthetic"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def read(self):
            return json.dumps(self.payload).encode()

    class Session:
        def __init__(self):
            self.calls = []
            self.responses = iter([Response(board_payload()), Response(thread_payload()), Response({}, 404)])
            self.close = AsyncMock()

        def get(self, url):
            self.calls.append(url)
            return next(self.responses)

    client = dvach.Api2chAsync()
    assert not hasattr(client, "_session")
    session = Session()
    client._session = session
    try:
        assert (await client.threads("b")).sorted_by_views()[0].thread_id == 8
        assert (await client.thread("b", 7)).posts[0].post_id == 7
        with pytest.raises(dvach.Api2chError) as error:
            await client.thread("b", 8)
        assert error.value.code == 404
        assert session.calls == ["https://2ch.hk/b/threads.json", "https://2ch.hk/b/res/7.json", "https://2ch.hk/b/res/8.json"]
    finally:
        await client.close()
    session.close.assert_awaited_once()
