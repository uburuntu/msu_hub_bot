"""Speech selection and preprocessing checks use synthetic audio without providers."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from msu_hub_bot.commands import speech
from msu_hub_bot.execution.executor import ExecutorBusy
from msu_hub_bot.providers.speech import SpeechError, SpeechErrorReason, Transcript
from msu_hub_bot.settings import MissingIntegration
from msu_hub_bot.telegram import media_jobs


async def prepare_job(prepare, func, **kwargs):
    return func(*(await prepare())), False


def recognizer(monkeypatch, *, text="transcript"):
    monkeypatch.setattr(speech, "prepare_audio", Mock(side_effect=lambda stream: b"mp3:" + stream.read()))
    client = SimpleNamespace(transcribe=AsyncMock(return_value=Transcript(text=text)))
    return speech.Speech(client, executor=SimpleNamespace(run_prepared=AsyncMock(side_effect=prepare_job)))


def audio_message(*, size=3, payload=b"pcm", reply=None, kind="voice", duration=1):
    async def download(file_id, *, destination):
        destination.write(payload)
        return destination

    audio = SimpleNamespace(file_id="synthetic-file", file_size=size, duration=duration, download=AsyncMock(side_effect=download))
    message = SimpleNamespace(
        voice=None,
        video_note=None,
        audio=None,
        video=None,
        reply_to_message=reply,
        bot=SimpleNamespace(download=audio.download),
        reply=AsyncMock(),
    )
    setattr(message, kind, audio)
    return message, audio


@pytest.mark.parametrize("kind", ["voice", "video_note"])
@pytest.mark.parametrize("has_reply", [False, True])
async def test_disabled_automatic_stt_never_downloads_incoming_or_replied_audio(kind, has_reply):
    reply, replied_audio = audio_message()
    incoming, audio = audio_message(kind=kind, reply=reply if has_reply else None)
    client = SimpleNamespace(transcribe=AsyncMock())

    assert await speech.Speech(client).process_stt(incoming, SimpleNamespace(auto_speech_recognition=False)) is True

    audio.download.assert_not_awaited()
    replied_audio.download.assert_not_awaited()
    client.transcribe.assert_not_awaited()


@pytest.mark.parametrize("kind", ["voice", "video_note", "audio", "video"])
async def test_explicit_stt_keeps_reply_selection_when_automatic_stt_is_disabled(monkeypatch, kind):
    reply, audio = audio_message(kind=kind, size=None)
    command = SimpleNamespace(voice=None, video_note=None, reply_to_message=reply, bot=reply.bot)
    handler = recognizer(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(speech, "send_super_reply", send)

    await handler.process_stt_command(command, SimpleNamespace(auto_speech_recognition=False))

    audio.download.assert_awaited_once()
    handler.client.transcribe.assert_awaited_once_with(b"mp3:pcm")
    send.assert_awaited_once_with(reply, "transcript")


async def test_enabled_automatic_stt_uses_incoming_voice_and_escapes_html(monkeypatch):
    reply, replied_audio = audio_message()
    incoming, audio = audio_message(reply=reply)
    handler = recognizer(monkeypatch, text="  <b>привет</b> & пока \n")
    send = AsyncMock()
    monkeypatch.setattr(speech, "send_super_reply", send)

    await handler.process_stt(incoming, SimpleNamespace(auto_speech_recognition=True))

    audio.download.assert_awaited_once()
    replied_audio.download.assert_not_awaited()
    send.assert_awaited_once_with(incoming, "&lt;b&gt;привет&lt;/b&gt; &amp; пока")


@pytest.mark.parametrize("text", ["", "   "])
async def test_empty_transcript_sends_nothing(monkeypatch, text):
    incoming, _ = audio_message()
    handler = recognizer(monkeypatch, text=text)
    send = AsyncMock()
    monkeypatch.setattr(speech, "send_super_reply", send)

    assert await handler.process_stt(incoming, SimpleNamespace(auto_speech_recognition=True)) is True
    send.assert_not_awaited()


async def test_missing_speech_provider_is_checked_before_download_or_conversion():
    incoming, audio = audio_message()
    executor = SimpleNamespace(run_prepared=AsyncMock())

    with pytest.raises(MissingIntegration) as caught:
        await speech.Speech(None, executor=executor).process_stt(incoming, SimpleNamespace(auto_speech_recognition=True))

    assert caught.value.fields == ("openrouter_api_key",)
    audio.download.assert_not_awaited()
    executor.run_prepared.assert_not_awaited()


async def test_long_records_are_skipped_automatically_and_explained_on_request(monkeypatch):
    incoming, audio = audio_message(duration=speech.MAX_SPEECH_SECONDS + 1)
    handler = recognizer(monkeypatch)

    assert await handler.process_stt(incoming, SimpleNamespace(auto_speech_recognition=True)) is True
    incoming.reply.assert_not_awaited()

    await handler.process_stt_command(incoming, SimpleNamespace(auto_speech_recognition=False))
    incoming.reply.assert_awaited_once()
    assert "20 минут" in incoming.reply.await_args.args[0]
    audio.download.assert_not_awaited()
    handler.client.transcribe.assert_not_awaited()


@pytest.mark.parametrize("declared_size", [None, 1, 5])
async def test_download_size_limit_covers_missing_and_inaccurate_metadata(monkeypatch, declared_size):
    monkeypatch.setattr(speech, "MAX_DOWNLOAD_BYTES", 4)
    monkeypatch.setattr(media_jobs, "MAX_DOWNLOAD_BYTES", 4)
    incoming, audio = audio_message(size=declared_size, payload=b"12345")
    handler = recognizer(monkeypatch)

    assert await handler.process_stt(incoming, SimpleNamespace(auto_speech_recognition=True)) is True

    assert audio.download.await_count == (0 if declared_size == 5 else 1)
    handler.client.transcribe.assert_not_awaited()


@pytest.mark.parametrize("result", [(None, True), (None, False), (b"", False)])
async def test_failed_preprocessing_never_submits_an_empty_request(result):
    incoming, _ = audio_message()
    client = SimpleNamespace(transcribe=AsyncMock())
    handler = speech.Speech(client, executor=SimpleNamespace(run_prepared=AsyncMock(return_value=result)))

    assert await handler.process_stt(incoming, SimpleNamespace(auto_speech_recognition=True)) is True
    client.transcribe.assert_not_awaited()


@pytest.mark.parametrize("explicit", [False, True])
async def test_busy_executor_is_quiet_only_for_automatic_recognition(explicit):
    incoming, _ = audio_message()
    handler = speech.Speech(
        SimpleNamespace(transcribe=AsyncMock()), executor=SimpleNamespace(run_prepared=AsyncMock(side_effect=ExecutorBusy("busy")))
    )
    settings = SimpleNamespace(auto_speech_recognition=True)

    if explicit:
        with pytest.raises(ExecutorBusy):
            await handler.process_stt_command(incoming, settings)
    else:
        assert await handler.process_stt(incoming, settings) is True


async def test_provider_failure_propagates_with_a_friendly_message(monkeypatch):
    incoming, _ = audio_message()
    handler = recognizer(monkeypatch)
    handler.client.transcribe.side_effect = SpeechError(SpeechErrorReason.UNAVAILABLE)

    with pytest.raises(SpeechError) as caught:
        await handler.process_stt(incoming, SimpleNamespace(auto_speech_recognition=True))
    assert "Не удалось распознать речь" in caught.value.text


def test_preparation_encodes_one_bounded_mono_mp3(monkeypatch):
    convert = Mock(return_value=io.BytesIO(b"mp3"))
    monkeypatch.setattr(speech, "ffmpeg", convert)

    assert speech.prepare_audio(io.BytesIO(b"audio")) == b"mp3"
    options = convert.call_args.kwargs
    assert options["out_suffix"] == ".mp3"
    parameters = options["parameters"]
    assert parameters[parameters.index("-t") + 1] == str(speech.MAX_SPEECH_SECONDS)
    assert parameters[parameters.index("-ac") + 1] == "1" and "-vn" in parameters


@pytest.mark.parametrize("output", [None, io.BytesIO(b"")])
def test_failed_or_empty_conversion_returns_nothing(monkeypatch, output):
    monkeypatch.setattr(speech, "ffmpeg", Mock(return_value=output))
    assert speech.prepare_audio(io.BytesIO(b"audio")) is None
