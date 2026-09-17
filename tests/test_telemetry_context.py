"""Request identity survives shared boundaries without exporting message contents."""

import asyncio
import logging
from contextvars import ContextVar

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, Update, User
from opentelemetry import baggage, context, trace
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

from msu_hub_bot.telegram.filters import MetaCommand
from msu_hub_bot.telegram.middlewares.telemetry import DispatchTelemetryMiddleware, HandlerTelemetryMiddleware
from msu_hub_bot.telegram.runtime import Supervisor
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.telemetry import Boundary, Provider, Telemetry, safe_failure
from telegram_helpers import RecordingSession, make_bot, make_message
from telemetry_helpers import Capture, config

CANARY = "SYNTHETIC_CONTEXT_PRIVATE_CONTENT"
IDS = {"telegram.user_id", "telegram.chat_id", "telegram.message_id", "telegram.thread_id", "telegram.update_id"}
METRIC_LABELS = {"boundary", "operation", "outcome", "provider", "backend", "update.kind"}


@pytest.fixture(autouse=True)
def offline_sdk(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)


def attributes(item):
    return {attribute.key: getattr(attribute.value, attribute.value.WhichOneof("value")) for attribute in item.attributes}


def metric_points(capture):
    for message in capture.messages():
        if isinstance(message, ExportMetricsServiceRequest):
            for resource in message.resource_metrics:
                for scope in resource.scope_metrics:
                    for metric in scope.metrics:
                        yield from getattr(metric, metric.WhichOneof("data")).data_points


def dispatcher_for(telemetry, handler):
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(DispatchTelemetryMiddleware(telemetry))
    dispatcher.message.middleware(HandlerTelemetryMiddleware(telemetry))
    dispatcher.message.register(handler, MetaCommand("probe", "echo"), flags={"handler_key": "test.handler"})
    return dispatcher


def update(bot, index, *, prefix="/", command="probe"):
    message = make_message(
        bot,
        message_id=100 + index,
        message_thread_id=200 + index,
        is_topic_message=True,
        chat={"id": -7000 - index, "type": "supergroup", "title": CANARY},
        from_user={"id": 500 + index, "is_bot": False, "first_name": CANARY, "username": CANARY},
        text=f"{prefix}{command} {CANARY}",
    )
    return Update(update_id=9000 + index, message=message)


async def test_concurrent_updates_keep_identity_in_provider_jobs_and_correlated_logs():
    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    telemetry.register_commands({"probe", "echo"})
    await telemetry.start()
    supervisor = Supervisor(telemetry)
    private = ContextVar("private_request_content", default=None)
    both_started, release_jobs = asyncio.Event(), asyncio.Event()
    started = 0
    jobs = []

    async def job():
        assert private.get() is None
        await release_jobs.wait()
        with telemetry.operation(Boundary.PROVIDER, "http.request", provider=Provider.OTHER):
            await asyncio.sleep(0)

    async def provider_request():
        with telemetry.operation(Boundary.PROVIDER, "jdoodle.execute", provider=Provider.JDOODLE):
            await asyncio.sleep(0)

    async def handler(message):
        nonlocal started
        token = private.set(CANARY)
        try:
            started += 1
            if started == 2:
                both_started.set()
            await both_started.wait()
            await asyncio.gather(provider_request(), provider_request())
            jobs.append(supervisor.create_job(job))
        finally:
            private.reset(token)

    dispatcher = dispatcher_for(telemetry, handler)
    bot = make_bot()
    try:
        await asyncio.gather(
            dispatcher.feed_update(bot, update(bot, 1)),
            dispatcher.feed_update(bot, update(bot, 2, prefix="#", command="echo")),
        )
        release_jobs.set()
        await asyncio.gather(*jobs)
        await supervisor.drain()
        with telemetry.operation(Boundary.PROVIDER, "wolfram.query", provider=Provider.WOLFRAM):
            pass
    finally:
        await bot.session.close()
        await telemetry.close()

    spans = sink.spans()
    expected = {
        -7001: (501, 101, 201, 9001, "probe", "slash"),
        -7002: (502, 102, 202, 9002, "echo", "hashtag"),
    }
    correlated = [span for span in spans if attributes(span)["operation"] != "wolfram.query"]
    assert len(correlated) == 10
    for span in correlated:
        values = attributes(span)
        user, message, thread, update_id, command, kind = expected[values["telegram.chat_id"]]
        assert (values["telegram.user_id"], values["telegram.message_id"], values["telegram.thread_id"], values["telegram.update_id"]) == (
            user,
            message,
            thread,
            update_id,
        )
        assert values["handler"] == "test.handler"
        assert (values["command"], values["command.kind"]) == (command, kind)
    orphan = next(span for span in spans if attributes(span)["operation"] == "wolfram.query")
    assert not (IDS | {"handler", "command", "command.kind"}) & attributes(orphan).keys()
    handlers = {attributes(span)["telegram.chat_id"]: span for span in spans if span.name == "bot.handler"}
    for span in spans:
        if span.name == "job.run":
            parent = handlers[attributes(span)["telegram.chat_id"]]
            assert not span.parent_span_id and span.trace_id != parent.trace_id
            assert len(span.links) == 1 and span.links[0].trace_id == parent.trace_id
    records = sink.logs()
    assert len(records) == 4
    by_id = {span.span_id: span for span in spans}
    for record in records:
        owner = by_id[record.span_id]
        assert record.trace_id == owner.trace_id
        assert attributes(record)["telegram.chat_id"] == attributes(owner)["telegram.chat_id"]
        assert attributes(record)["handler"] == "test.handler"
        assert record.body.string_value == "bot.operation.completed"
    points = list(metric_points(sink))
    assert points
    assert all(attributes(point).keys() <= METRIC_LABELS for point in points)
    assert CANARY not in sink.serialized()


async def test_cancellation_and_next_update_cannot_retain_the_previous_identity():
    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    telemetry.register_commands({"probe", "echo"})
    await telemetry.start()
    started = asyncio.Event()

    async def handler(message):
        if message.from_user.id == 501:
            started.set()
            await asyncio.Event().wait()

    dispatcher = dispatcher_for(telemetry, handler)
    bot = make_bot()
    try:
        first = asyncio.create_task(dispatcher.feed_update(bot, update(bot, 1)))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await dispatcher.feed_update(bot, update(bot, 2))
        with telemetry.operation(Boundary.PROVIDER, "http.request"):
            pass
    finally:
        await bot.session.close()
        await telemetry.close()
    handlers = [span for span in sink.spans() if span.name == "bot.handler"]
    assert {(attributes(span)["telegram.user_id"], attributes(span)["outcome"]) for span in handlers} == {
        (501, "cancelled"),
        (502, "success"),
    }
    assert len({span.trace_id for span in handlers}) == 2
    orphan = next(span for span in sink.spans() if span.name == "provider.request")
    assert not IDS & attributes(orphan).keys()
    assert not any(span.events for span in handlers)


async def test_context_rejects_unregistered_commands_and_invalid_identifiers():
    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    telemetry.register_commands({"probe"})
    await telemetry.start()
    try:
        with telemetry.context(
            user_id=CANARY,
            chat_id=True,
            message_id=2**100,
            handler=CANARY,
            command=CANARY,
            command_kind=CANARY,
        ):
            with telemetry.operation(Boundary.PROVIDER, "http.request"):
                logging.getLogger("synthetic.legacy").warning("Legacy content: %s", CANARY, extra={"secret": CANARY})
    finally:
        await telemetry.close()
    values = attributes(sink.spans()[0])
    assert not IDS & values.keys()
    assert values.get("handler", "unknown") == "unknown"
    assert values.get("command", "unknown") == "unknown"
    assert CANARY not in sink.serialized()
    assert sink.logs() == []


@pytest.mark.parametrize("inline", [False, True])
async def test_callback_context_uses_actor_and_never_exports_callback_contents(inline):
    sink = Capture()
    telemetry = Telemetry(config(), {"test.callback"}, transport=sink)
    await telemetry.start()
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(DispatchTelemetryMiddleware(telemetry))
    dispatcher.callback_query.middleware(HandlerTelemetryMiddleware(telemetry))

    async def callback(query):
        return True

    dispatcher.callback_query.register(callback, flags={"handler_key": "test.callback"})
    bot = make_bot()
    message = make_message(
        bot,
        message_id=123,
        from_user={"id": 999, "is_bot": True, "first_name": CANARY},
        text=CANARY,
        reply_to_message=make_message(bot, message_id=456, text=CANARY),
    )
    query = CallbackQuery(
        id=CANARY,
        from_user=User(id=502, is_bot=False, first_name=CANARY),
        chat_instance=CANARY,
        data=CANARY,
        **({"inline_message_id": CANARY} if inline else {"message": message}),
    )
    try:
        await dispatcher.feed_update(bot, Update(update_id=987, callback_query=query))
    finally:
        await bot.session.close()
        await telemetry.close()
    values = attributes(sink.spans()[0])
    assert values["telegram.user_id"] == 502
    assert values["telegram.update_id"] == 987
    assert values["handler"] == "test.callback"
    assert "command" not in values
    if inline:
        assert "telegram.chat_id" not in values and "telegram.message_id" not in values
    else:
        assert values["telegram.chat_id"] == message.chat.id
        assert values["telegram.message_id"] == 123
        assert values["telegram.reply_to_message_id"] == 456
    assert CANARY not in sink.serialized()


@pytest.mark.parametrize(
    "kind,message,reason,status",
    [
        (TelegramBadRequest, "Bad Request: chat not found", "chat_not_found", 400),
        (TelegramBadRequest, "Bad Request: can't parse entities: " + CANARY, "invalid_entities", 400),
        (TelegramRetryAfter, CANARY, "rate_limited", 429),
    ],
)
async def test_telegram_failure_keeps_source_and_destination_without_payloads(kind, message, reason, status):
    class FailedSession(RecordingSession):
        async def make_request(self, bot, method, timeout=None):
            fields = {"retry_after": 100_000} if kind is TelegramRetryAfter else {}
            error = kind(method=method, message=message, **fields)
            error.add_note(CANARY)
            raise error from RuntimeError(CANARY)

    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    telemetry.register_commands({"probe"})
    await telemetry.start()
    bot = BotWrapper("123456789:" + "a" * 35, session=FailedSession(), telemetry=telemetry)
    try:
        with telemetry.context(user_id=501, chat_id=-7001, message_id=101, command="probe", command_kind="slash"):
            with telemetry.operation(Boundary.HANDLER, "test.handler"):
                with pytest.raises(kind):
                    await bot.edit_message_text(CANARY, chat_id=-8001, message_id=202)
    finally:
        await bot.session.close()
        await telemetry.close()
    request = next(span for span in sink.spans() if span.name == "telegram.request")
    values = attributes(request)
    assert values["telegram.user_id"] == 501 and values["telegram.chat_id"] == -7001
    assert values["telegram.message_id"] == 101
    assert values["telegram.target_chat_id"] == -8001 and values["telegram.target_message_id"] == 202
    assert values["telegram.method"] == "editMessageText"
    assert values["handler"] == "test.handler" and values["command"] == "probe"
    assert values["error.type"] == kind.__name__
    assert values["error.reason"] == reason and values["http.response.status_code"] == status
    assert values["code.file.path"] == "msu_hub_bot/telegram/wrapper.py"
    assert values["code.function.name"] == "_request"
    assert values["code.line.number"] > 0
    if kind is TelegramRetryAfter:
        assert values["telegram.retry_after"] == 86400
    records = [record for record in sink.logs() if record.body.string_value == "telegram.request.failed"]
    assert len(records) == 1
    assert records[0].span_id == request.span_id and records[0].trace_id == request.trace_id
    assert attributes(records[0])["telegram.target_chat_id"] == -8001
    assert attributes(records[0])["code.file.path"] == "msu_hub_bot/telegram/wrapper.py"
    assert all(attributes(point).keys() <= METRIC_LABELS for point in metric_points(sink))
    assert CANARY not in sink.serialized()


async def test_unsampled_failures_keep_only_vetted_diagnostics_in_bounded_log_queue():
    sink = Capture()
    telemetry = Telemetry(config(sample_rate=0, queue_capacity=2), {"test.handler"}, transport=sink)
    await telemetry.start()
    private_error = type(CANARY, (Exception,), {})
    try:
        with telemetry.context(user_id=501, chat_id=-7001):
            for _ in range(5):
                with pytest.raises(private_error):
                    with telemetry.operation(Boundary.HANDLER, "test.handler"):
                        error = private_error(CANARY)
                        error.add_note(CANARY)
                        raise error from RuntimeError(CANARY)
    finally:
        await telemetry.close()
    assert sink.spans() == []
    assert telemetry.dropped_logs == 3
    assert len(sink.logs()) == 2
    for record in sink.logs():
        values = attributes(record)
        assert record.body.string_value == "bot.operation.failed"
        assert values["telegram.user_id"] == 501 and values["telegram.chat_id"] == -7001
        assert values["error.type"] == "Exception" and values["error.reason"] == "unexpected"
        assert record.trace_id and record.span_id
    output = sink.serialized()
    assert "bot.telemetry.dropped_logs" in output
    assert "exception.message" not in output and "exception.stacktrace" not in output
    assert CANARY not in output


def test_exception_location_ignores_fake_private_filenames_and_module_claims():
    code = compile("def fail():\n    raise RuntimeError(private)\nfail()", f"/private/{CANARY}.py", "exec")
    with pytest.raises(RuntimeError) as caught:
        exec(code, {"__name__": "msu_hub_bot.telegram.wrapper", "private": CANARY})
    values = safe_failure(caught.value)
    assert not any(key.startswith("code.") for key in values)
    assert CANARY not in str(values)


async def test_failure_log_without_owned_span_does_not_borrow_ambient_trace_or_baggage():
    sink = Capture()
    telemetry = Telemetry(config(), transport=sink)
    await telemetry.start()
    external = trace.NonRecordingSpan(trace.SpanContext(trace_id=123, span_id=456, is_remote=False))
    ambient = baggage.set_baggage("private", CANARY, context=trace.set_span_in_context(external))
    token = context.attach(ambient)
    try:
        with pytest.raises(RuntimeError):
            with telemetry.operation(Boundary.TELEGRAM, "telegram.request", telegram_method="getChat", trace=False):
                raise RuntimeError(CANARY)
    finally:
        context.detach(token)
        await telemetry.close()
    assert sink.spans() == []
    assert len(sink.logs()) == 1
    record = sink.logs()[0]
    assert not record.trace_id and not record.span_id
    assert record.time_unix_nano > 0
    assert CANARY not in sink.serialized()


@pytest.mark.parametrize("business_failure", [False, True])
async def test_sdk_log_failure_preserves_business_result_and_clears_context(monkeypatch, caplog, business_failure):
    from msu_hub_bot import telemetry as module

    sink = Capture()
    telemetry = Telemetry(config(), {"test.handler"}, transport=sink)
    await telemetry.start()
    business_error = ValueError("synthetic business failure")
    result = object()
    handles = []

    def failed_emit(**kwargs):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(telemetry._structured_logger, "emit", failed_emit)

    def business_operation():
        with telemetry.context(user_id=501, chat_id=-7001):
            with telemetry.operation(Boundary.HANDLER, "test.handler") as handle:
                handles.append(handle)
                if business_failure:
                    raise business_error
                return result

    try:
        if business_failure:
            with pytest.raises(ValueError) as caught:
                business_operation()
            assert caught.value is business_error
        else:
            assert business_operation() is result
        assert not handles[0].active
        assert module._current.get() is None and module._request.get() is None
        with telemetry.operation(Boundary.PROVIDER, "http.request"):
            pass
    finally:
        await telemetry.close()

    handler = next(span for span in sink.spans() if span.name == "bot.handler")
    orphan = next(span for span in sink.spans() if span.name == "provider.request")
    assert handler.end_time_unix_nano >= handler.start_time_unix_nano
    assert attributes(handler)["outcome"] == ("unexpected" if business_failure else "success")
    assert not orphan.parent_span_id and orphan.trace_id != handler.trace_id
    assert not (IDS | {"handler", "command"}) & attributes(orphan).keys()
    assert telemetry.dropped_logs == 1
    assert sink.logs() == []
    assert "Telemetry log emission failed" in caplog.text
    assert CANARY not in caplog.text and CANARY not in sink.serialized()
