"""Speech selection and preprocessing checks use synthetic audio without providers."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from msu_hub_bot.cli import prepare_imports
from msu_hub_bot.settings import MissingIntegration

prepare_imports()
from utils import wit  # noqa: E402


def audio_message(*, size=3, payload=b"pcm", reply=None, kind="voice"):
    async def download(*, destination_file):
        destination_file.write(payload)
        return destination_file

    audio = SimpleNamespace(file_size=size, duration=1, download=AsyncMock(side_effect=download))
    message = SimpleNamespace(voice=None, video_note=None, audio=None, video=None, reply_to_message=reply)
    setattr(message, kind, audio)
    return message, audio


@pytest.mark.parametrize("kind", ["voice", "video_note"])
@pytest.mark.parametrize("has_reply", [False, True])
async def test_disabled_automatic_stt_never_downloads_incoming_or_replied_audio(kind, has_reply):
    reply, replied_audio = audio_message()
    incoming, audio = audio_message(kind=kind, reply=reply if has_reply else None)
    client = wit.Wit([])
    client.stt = AsyncMock()

    assert await client.process_stt(incoming, SimpleNamespace(auto_speech_recognition=False)) is True

    audio.download.assert_not_awaited()
    replied_audio.download.assert_not_awaited()
    client.stt.assert_not_awaited()


@pytest.mark.parametrize("kind", ["voice", "video_note", "audio", "video"])
async def test_explicit_stt_keeps_reply_selection_when_automatic_stt_is_disabled(monkeypatch, kind):
    reply, audio = audio_message(kind=kind, size=None)
    command = SimpleNamespace(voice=None, video_note=None, reply_to_message=reply)
    client = wit.Wit(["synthetic"])
    client.stt = AsyncMock(return_value="transcript")
    send = AsyncMock()
    monkeypatch.setattr(wit, "send_super_reply", send)

    await client.process_stt_command(command, SimpleNamespace(auto_speech_recognition=False))

    audio.download.assert_awaited_once()
    downloaded = client.stt.await_args.args[0]
    assert downloaded.read() == b"pcm"
    assert client.stt.await_args.kwargs == {"duration": 1}
    send.assert_awaited_once_with(reply, "transcript")


async def test_enabled_automatic_stt_uses_incoming_voice(monkeypatch):
    reply, replied_audio = audio_message()
    incoming, audio = audio_message(reply=reply)
    client = wit.Wit(["synthetic"])
    client.stt = AsyncMock(return_value="transcript")
    send = AsyncMock()
    monkeypatch.setattr(wit, "send_super_reply", send)

    await client.process_stt(incoming, SimpleNamespace(auto_speech_recognition=True))

    audio.download.assert_awaited_once()
    replied_audio.download.assert_not_awaited()
    send.assert_awaited_once_with(incoming, "transcript")


async def test_missing_speech_provider_is_checked_before_download_or_conversion():
    incoming, audio = audio_message()
    executor = SimpleNamespace(run=AsyncMock())
    client = wit.Wit([], executor=executor)

    with pytest.raises(MissingIntegration):
        await client.process_stt(incoming, SimpleNamespace(auto_speech_recognition=True))

    audio.download.assert_not_awaited()
    executor.run.assert_not_awaited()


@pytest.mark.parametrize("declared_size", [None, 1, 5])
async def test_download_size_limit_covers_missing_and_inaccurate_metadata(monkeypatch, declared_size):
    monkeypatch.setattr(wit, "megabytes", lambda _value: 4)
    incoming, audio = audio_message(size=declared_size, payload=b"12345")
    client = wit.Wit(["synthetic"])
    client.stt = AsyncMock()

    assert await client.process_stt(incoming, SimpleNamespace(auto_speech_recognition=True)) is True

    assert audio.download.await_count == (0 if declared_size == 5 else 1)
    client.stt.assert_not_awaited()


def test_audio_buffer_rejects_the_chunk_that_exceeds_the_limit():
    buffer = wit._AudioBuffer(4)
    assert buffer.write(b"123") == 3
    with pytest.raises(wit._AudioTooLarge):
        buffer.write(b"45")
    assert buffer.getvalue() == b"123"


def test_failed_native_conversion_stops_chunking(monkeypatch):
    convert = Mock(return_value=None)
    monkeypatch.setattr(wit, "ffmpeg", convert)

    assert wit.Wit.to_raw_chunks(io.BytesIO(b"audio"), duration=60) == []
    convert.assert_called_once()


@pytest.mark.parametrize("result", [(None, True), ([], False), ([None], False)])
async def test_failed_preprocessing_never_submits_an_empty_request(result):
    client = wit.Wit(["synthetic"], executor=SimpleNamespace(run=AsyncMock(return_value=result)))
    client.instances[0].speech = AsyncMock()

    assert await client.stt(io.BytesIO(b"audio"), duration=1) is None
    client.instances[0].speech.assert_not_awaited()
