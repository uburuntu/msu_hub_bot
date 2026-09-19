"""Speech preprocessing has one budget and never recognizes a partial batch."""

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from msu_hub_bot.execution.executor import ExecutorBusy
from msu_hub_bot.providers import wit
from test_speech_recognition import audio_message, recognizer


@pytest.mark.parametrize(
    "parameters",
    [
        {"duration": 0},
        {"duration": -1},
        {"duration": float("nan")},
        {"duration": True},
        {"duration": 1, "max_size": 0},
        {"duration": 1, "overlap": -1},
        {"duration": 1, "max_size": 200, "overlap": 100},
    ],
)
def test_invalid_chunk_parameters_do_not_launch_native_work(monkeypatch, parameters):
    native = Mock()
    monkeypatch.setattr(wit, "ffmpeg", native)
    assert wit.Wit.to_raw_chunks(io.BytesIO(b"audio"), **parameters) == []
    native.assert_not_called()


def test_whole_preparation_deadline_shrinks_and_discards_late_batch(monkeypatch):
    now = [0.0]
    opened = []
    budgets = []
    monkeypatch.setattr(wit, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def convert(file, *, timeout, **kwargs):
        budgets.append(timeout)
        now[0] += 40
        chunk = io.BytesIO(b"pcm")
        opened.append(chunk)
        return chunk

    monkeypatch.setattr(wit, "ffmpeg", convert)
    assert wit.Wit.to_raw_chunks(io.BytesIO(b"audio"), duration=40) == []
    assert budgets == [120, 80, 40]
    assert all(chunk.closed for chunk in opened)


def test_expired_preparation_budget_does_not_start_conversion(monkeypatch):
    monkeypatch.setattr(wit, "CHUNK_PREPARATION_TIMEOUT", 0)
    native = Mock()
    monkeypatch.setattr(wit, "ffmpeg", native)
    assert wit.Wit.to_raw_chunks(io.BytesIO(b"audio"), duration=40) == []
    native.assert_not_called()


@pytest.mark.parametrize("failure", ["empty", "missing", "exception", "aggregate-size"])
def test_failed_batch_closes_every_partial_chunk(monkeypatch, failure):
    first = io.BytesIO(b"1234")
    second = io.BytesIO(b"" if failure == "empty" else b"5678")
    values = iter([first, None if failure == "missing" else second])
    calls = 0
    if failure == "aggregate-size":
        monkeypatch.setattr(wit, "MAX_PCM_BYTES", 7)

    def convert(*args, **kwargs):
        nonlocal calls
        calls += 1
        if failure == "exception" and calls == 2:
            raise ValueError("synthetic failure")
        return next(values)

    monkeypatch.setattr(wit, "ffmpeg", convert)
    if failure == "exception":
        with pytest.raises(ValueError, match="synthetic"):
            wit.Wit.to_raw_chunks(io.BytesIO(b"audio"), duration=30)
    else:
        assert wit.Wit.to_raw_chunks(io.BytesIO(b"audio"), duration=30) == []
    assert calls == 2 and first.closed
    if failure in {"empty", "aggregate-size"}:
        assert second.closed
    second.close()


def test_worker_returns_only_immutable_chunks_and_closes_native_buffers(monkeypatch):
    chunks = [io.BytesIO(b"one"), io.BytesIO(b"two")]
    monkeypatch.setattr(wit.Wit, "to_raw_chunks", staticmethod(lambda *args: chunks))
    assert wit.Wit._prepare_audio(io.BytesIO(b"audio"), 30) == (b"one", b"two")
    assert all(chunk.closed for chunk in chunks)


async def test_recognition_keeps_formatting_and_closes_sent_chunks():
    client = wit.Wit(["synthetic"])
    opened = []
    results = iter(["<hello>", "", "world", ""])

    async def speech(chunk, *, content_type):
        assert content_type == "audio/raw;encoding=signed-integer;bits=16;rate=16000;endian=little"
        opened.append(chunk)
        return next(results)

    client.instances[0].speech = speech
    assert await client._recognize_chunks((b"1", b"2", b"3", b"4")) == "&lt;hello&gt; | <i>не распознано</i> | world"
    assert all(chunk.closed for chunk in opened)


async def test_cancelling_recognition_drains_requests_before_closing_buffers():
    client = wit.Wit(["synthetic"])
    started = asyncio.Event()
    opened = []
    settled = []

    async def speech(chunk, **kwargs):
        opened.append(chunk)
        if len(opened) == 2:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not chunk.closed
            settled.append(chunk)

    client.instances[0].speech = speech
    task = asyncio.create_task(client._recognize_chunks((b"1", b"2")))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(settled) == 2 and all(chunk.closed for chunk in opened)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("explicit", [False, True])
async def test_busy_automatic_speech_skips_and_explicit_speech_uses_central_feedback(monkeypatch, explicit):
    incoming, audio = audio_message()
    client = recognizer()
    monkeypatch.setattr(wit, "run_downloaded", AsyncMock(side_effect=ExecutorBusy("busy")))
    if explicit:
        with pytest.raises(ExecutorBusy):
            await client.process_stt(incoming, SimpleNamespace(auto_speech_recognition=True), explicit=True)
    else:
        assert await client.process_stt(incoming, SimpleNamespace(auto_speech_recognition=True)) is True
    audio.download.assert_not_awaited()
    client._recognize_chunks.assert_not_awaited()


async def test_recognition_capacity_is_shared_across_messages_and_tokens(monkeypatch):
    monkeypatch.setattr(wit, "RECOGNITION_CONCURRENCY", 2)
    client = wit.Wit(["first", "second"])
    active, peak = 0, 0
    release = asyncio.Event()
    full = asyncio.Event()

    async def speech(chunk, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            full.set()
        try:
            await release.wait()
            return chunk.getvalue().decode()
        finally:
            active -= 1

    for instance in client.instances:
        instance.speech = speech
    tasks = [asyncio.create_task(client._recognize_chunks((b"a", b"b", b"c"))) for _ in range(3)]
    try:
        await asyncio.wait_for(full.wait(), 1)
        await asyncio.sleep(0)
        assert active == 2
        release.set()
        assert await asyncio.gather(*tasks) == ["a | b | c"] * 3
        assert peak == 2 and active == 0
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_recognition_deadline_includes_capacity_wait_and_drains_children(monkeypatch):
    monkeypatch.setattr(wit, "RECOGNITION_CONCURRENCY", 1)
    monkeypatch.setattr(wit, "RECOGNITION_TIMEOUT", 0.02)
    client = wit.Wit(["synthetic"])
    opened, settled = [], []

    async def speech(chunk, **kwargs):
        opened.append(chunk)
        try:
            await asyncio.Event().wait()
        finally:
            assert not chunk.closed
            settled.append(chunk)

    client.instances[0].speech = speech
    with pytest.raises(wit.WitAPIError) as caught:
        await client._recognize_chunks((b"one", b"two", b"three"))
    assert caught.value.code == 408
    assert len(opened) == len(settled) == 1
    assert all(chunk.closed for chunk in opened)
    assert not client._recognition_slots.locked()


async def test_whole_speech_deadline_includes_preprocessing(monkeypatch):
    monkeypatch.setattr(wit, "SPEECH_REQUEST_TIMEOUT", 0.02)
    settled = asyncio.Event()

    async def prepare(*args):
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    client = wit.Wit(["synthetic"], executor=SimpleNamespace(run=prepare))
    client.instances[0].speech = AsyncMock()
    with pytest.raises(wit.WitAPIError) as caught:
        await client.stt(io.BytesIO(b"audio"), 30)
    assert caught.value.code == 408 and settled.is_set()
    client.instances[0].speech.assert_not_awaited()
