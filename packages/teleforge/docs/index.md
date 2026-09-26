# TeleForge authoring guide

TeleForge compiles ordinary feature methods onto aiogram routers. One feature owns related entrypoints, cards and conversation steps. Its instance lives as long as the application: constructor services belong on `self`; the current update belongs in the explicitly typed `ctx` parameter.

Start with [the executable counter](../examples/quickstart.py). `testing.RecordingBot` records actual aiogram methods and uploaded bytes without opening sockets. Test through `App.feed_update` to exercise routing, preparation and delivery together. [AGENTS.md](../AGENTS.md) defines development and review rules.

## Commands and inputs

```python
class Utilities(Feature, key="utilities"):
    @command("roll", digits=Argument(clamp=(1, 100)))
    async def roll(self, digits: int = 3) -> Text:
        return Text(Code(make_roll(digits)))

    @command("meme", text=TextInput(), image=ImageInput(reply=True))
    async def meme(self, ctx: MessageContext, text: str, image: Image.Image) -> None:
        with render_meme(image, text) as encoded:
            await ctx.reply(photo=encoded)
```

Here `render_meme` returns an encoded `BytesIO`; the input is Pillow's decoded image. Install the `media` extra for decoding. Native aiogram media objects, `bytes`, `BytesIO` and `Path` are also supported input representations. Choose a native media object when an application executor must admit work before downloading, as in MSU's caption feature.

Undeclared positional scalar parameters of commands consume tokens in signature order. Keyword-only parameters are middleware dependencies unless explicitly declared as acquired input; payload fields and step drafts have their declared sources. Ordinary event handlers never parse command tokens. Missing or invalid values use the annotated default; `Argument(strict=True)` reports invalid supplied values instead. Required parameters give brief guidance. `TextInput` explicitly consumes remaining text, then optionally a replied message or UTF-8 text document. Invalid tokens rescued by defaults remain available to text input. `document="prefer"` reads an attached document before its caption within each source; explicit command text still precedes a replied document. Constrained text that fails validation produces localizable guidance; invalid author defaults are configuration errors.

Declarations are named after parameters. Undeclared command services are keyword-only and come from aiogram middleware by name. Middleware cannot shadow command arguments, declared payload fields or native events. Dependencies are type-checked without copying or reconstructing service objects. `ctx`, `event`, `bot`, message `message`, and callback `query` are invocation values. The observer's exact native event type also binds by annotation: `chosen_result: ChosenInlineResult` or `callback: CallbackQuery` needs no special spelling or context wrapper. Other model types remain dependencies; a callback can receive an explicitly injected `source: Message`. Explicit input and payload declarations take precedence. Constructor injection is ordinary Python.

Attached media takes precedence over replied media. `/meme Hello` replying to a photo selects the command's text and the replied photo: successful output replies to the photo, guidance to the command. `ctx.input_sources` records each source separately. Callback UI is never implicitly interpreted as input; select a source deliberately or load an application record.

Media selection chooses the source message first, from the invocation then its reply. Within that source, `MediaInput.kinds` defines the preferred media order.

Image acquisition accepts JPEG, PNG, TIFF, BMP, GIF and WebP documents. Arbitrary documents remain available through `DocumentInput` or a `MediaInput` with the `document` kind. Avatar fallback tries the known original author of a forwarded message before its forwarding user, preferring the replied message over the invocation.

Downloads are bounded even when Telegram omits their size. Streams, files and decoded images stay alive through returned-output delivery, then close. Never retain them on the feature, enqueue temporary paths, or pass them to detached tasks. Decode work joins on cancellation. Application executors still own their processing limits.

The default matcher is native, case-insensitive aiogram `Command`. Applications can supply `filter=...` returning `_teleforge_tail` for argument parsing. An optional `_teleforge_text` supplies a separate body to `TextInput`; omitting it uses the unconsumed tail. Both values must be strings. MSU's `HubCommand` uses these channels to retain slash aliases and in-text hashtags without confusing hashtag arguments with surrounding text. Explicit filters, flags, native routers and middleware remain available.

## Output and delivery

Plain strings are literal text. Use aiogram `Text`, `Bold`, `Code`, `TextLink` and related objects for formatting. TeleForge disables bot-wide parse-mode defaults for its own sends. Explicit entities use Telegram's UTF-16 offsets.

Return `str | Text` from an ordinary command. Use `await ctx.reply(photo=...)` for media, combined content or a sent result; a string file ID belongs in a named media argument. `ctx.reply(..., fixed=True)` and `show(...)` return one `Message`. Native aiogram methods and handled results are supported. Native sends bypass TeleForge's delivery policies.

Text plus media prefers one Rich Message when supported by the content's entity/media constraints; otherwise delivery plans captions and complete text chunks before sending. A rejected or uncertain Rich API write is never retried as native messages. The default soft budget is three messages; larger text becomes a complete UTF-8 file. `rich=False`, `soft_messages=...` and `max_output_bytes=...` customize that policy. Hard budgets bound locally owned text, uploads and serialized markup before sending; referenced Telegram files and explicitly allowed remote URLs cannot be preflighted for media size. Output is never silently truncated.

Use `fixed=True` for one message that must stay editable. Oversized fixed output fails before delivery; it is not split, reposted or converted to a file. `ctx.edit` addresses the actual UI regardless of input selection; `to=` selects a native message or `DeliveryTarget`. Inline/inaccessible edits require an explicit content kind when it cannot be inferred. Replying to inaccessible UI requires an explicit target and scope. Full Rich Message edits replace the tree, without inferring partial merges. Omitted or `None` edit markup clears buttons; provide fresh markup on each redraw.

Native handlers can finish a waiting text message with `complete_response(bot, status, result, overflow_to=source, overflow_notice="Full result attached.")`. Valid text that fits edits the status; known overflow sends one complete UTF-8 `result.txt` to the explicitly selected source and then updates the status notice. File output retains destinations from text links. The complete file, notice and controls share the output budget. An API rejection or uncertain write never activates file fallback or replays the upload. `CompletedResponse.result` is the confirmed edit/file, `spilled` identifies a file, and `status_error` records a failed notice after a confirmed file. Report that separate error without repeating completed work. A supplied `DeliveryProgress` tracks the result write through cancellation, including a confirmed file whose notice is interrupted. Provider execution and cancellation copy stay in the application. `edit_response` and `ctx.edit` accept native `link_preview_options` when an application deliberately shows a preview.

Only finite local media and Telegram file IDs are accepted by default. Remote URLs require `allow_remote_media=True`; validate provider URLs before enabling it. Arbitrary streaming `InputFile` is not a bounded upload. Caller streams stay caller-owned; TeleForge snapshots stay alive until requests finish.

`DeliveryError.progress` distinguishes confirmed sends from an uncertain request. Timeout does not prove rejection. Do not blindly retry a send or a domain action: reconcile or wait for a new invocation. Known rejections and `message is not modified` have separate handling. A primary exception must not be hidden by fallback acknowledgements or guidance.

## Callbacks and managed cards

Raw `@callback(Payload)` projects validated native `CallbackData` fields into typed parameters. Use explicit `ctx.answer`, `ctx.edit` or `ctx.reply`; a returned string is not guessed to mean an alert or an edit. `ack="manual"` transfers acknowledgement ownership to an existing native handler. Otherwise normal completion supplies an empty acknowledgement only if none was attempted.

An existing native button format can use the same redraw lifecycle: `@action(key="navigate", card="board", payload=PageCallback)`. Pass its native buttons directly to `Card`; payload fields supply renderer arguments and keyword-only renderer services still come from middleware. This avoids inventing another encoding or invalidating existing buttons. Inaccessible or foreign card messages receive localizable guidance without running the action or reading application state.

Use a managed card when an action should reload and redraw the same UI:

```python
@card
async def board(self, ctx: Context, game: str) -> Card:
    record = await self.games.get(game)
    return Card(record.text, buttons=[[Button("Vote", self.vote, option=1)]])


@action(key="vote", card="board")
async def vote(self, ctx: CallbackContext, game: str, option: int) -> None:
    await self.games.vote(game, actor=ctx.user.id, option=option)
    await ctx.answer("Saved")
```

`await show(ctx, self.board, game=id)` opens it. Renderer payload arguments carry into buttons; button arguments add or replace values. Keyword-only services come from middleware and never enter the callback payload. Explicit stable action keys survive Python action/renderer method renames; changing payload schema intentionally invalidates incompatible old buttons. Pass short application IDs, not serialized state: callback payloads have a 64-byte bound. Valid typed payloads are **not authorization**. The service must check current actor, origin, ownership, expiry and revision.

Callback validators must be pure and preserve the encoded argument value. Normalize values before building a button; a transforming validator is rejected so repeated validation cannot change which record an action addresses. Renderer defaults are included in button arguments, and runtime card locks belong to the application and use the actual bot/chat/message/business address across features and renderers.

Renderers reload current state and have no domain side effects. Actions serialize per UI in the process; application transactions/CAS provide durable concurrency. Successful actions refresh by default. `CardRefreshError` means the handler returned but presentation failed; application state determines whether rendering can be retried. `ctx.outcome` and an exception’s `teleforge_outcome` preserve independent handler-return, acknowledgement and presentation facts. A Python return never proves a database commit, and an exception never proves its absence. `refresh=False` supports explicit barriers such as delivering feedback's exact preview before enabling submission.

Read-only cards may choose `ack="early", coalesce=True`: acknowledge before waiting, and drop overlapping refreshes for that UI. Early acknowledgement gives up a later alert result. Do not coalesce votes, payments or other mutations that must each run. Early/coalescing actions release their conversation isolation after route selection; they must not access FSM afterward or skip to another handler. Embedded hosts must provide the explicit release bridge described below.

A job can prepare the same view without manufacturing a Telegram event:

```python
content = await prepare_card(render_board, data={"games": games}, game=game_id)
await edit_response(bot, saved_target, **content)
```

`prepare_card` accepts an ordinary renderer or an already-rendered `Card`. Use `context=` only if the renderer explicitly needs one. Its result is native keyword arguments for existing delivery functions. Media remains caller-owned and must stay open through send/edit. The application owns leases, current-state checks and persisted targets.

## Conversations

```python
@step("title", draft=StickerDraft)
async def title(self, ctx: MessageContext, draft: StickerDraft) -> None:
    await self.stickers.create(draft, title=ctx.message.text)
    await leave(ctx)


# After confirming the prompt's delivery:
await enter(ctx, self.title, draft)
```

Steps persist named destinations and JSON-compatible Pydantic drafts through the host's aiogram FSM storage. There is no replay engine or second conversation database. `enter` starts from no state or advances within the same feature; cancel or clear the previous workflow explicitly before changing features. `leave` removes TeleForge's draft while preserving unrelated FSM data and refuses to clear a non-TeleForge state. Incompatible drafts fail explicitly; the application owns migrations/reset policy.

A separate cancel callback can read `draft = await read_draft(ctx, StickerDraft)` before cleanup and `leave(ctx)`. Select the intended step with a route/state check first. The reader verifies the active state, saved envelope, actor/chat/topic scope and model without changing data. Read before leaving or releasing isolation.

Native FSM data and state updates are separate writes, not a transaction. A failed transition may leave a mismatched envelope; the step rejects it rather than running with another step's draft. The host owns recovery and must configure storage namespaces/key builders to isolate bots and business connections. Direct-message topics require an application conversation adapter; they cannot use the default forum-topic key safely.

Standalone apps default to `USER_IN_TOPIC` and local event isolation. State loading and route selection happen while locked. Terminal stateless handlers can use `flags={"fsm_release": True}`, or call `await ctx.release_isolation()` after completing a transition; later FSM access, child-task release and `SkipHandler` are rejected. Embedded hosts must supply state scoped to the actual bot, chat, actor and forum topic. Inline/inaccessible callbacks cannot invent that scope. Commands default to `StateFilter(None)`. Put `/cancel` before catch-all steps, with an explicit state filter; other commands remain input during the conversation. Conversations are opt-in.

## Guidance and observation

Automatic acquisition errors are structured `InputError` values with a safe code and declared parameter/limit metadata. Supply `App(input_formatter=...)` to render them in the bot’s language. Internal configuration errors propagate to the host error boundary. An application handler can deliberately send `await ctx.guide("Пришли картинку в ответ на это сообщение.")`; no exception or framework translation catalog is needed.

`data["teleforge_invocation"].outcome` lets host middleware distinguish handled input issues from successful work. Install `InvocationMiddleware` before host outer middleware that needs this holder; inner middleware receives it through the compiled router. The snapshot includes no user text, callback payloads or credentials. It records managed delivery and native returned methods; direct `ctx.bot(...)` calls remain native operations observed by the host’s session middleware. TeleForge does not infer transaction commits, charge status or retry safety. Commit application operation/results in explicit short transactions before presentation; do not hold a transaction around a provider call plus automatic delivery.

## Jobs, HTTP and persistence

The optional registration helpers are convenience APIs, outside the stable Telegram invocation contract. Use native host registration when it is clearer. `@job("name", payload=Model)` and `bind_jobs(app, adapter)` expose validated methods to the application's worker under `feature_key.name`; renaming the Python method does not change that durable identity. Enqueue with the application's transaction API. TeleForge does not provide atomic state-plus-enqueue, exactly-once execution, leases, retry policy or retention. Those belong to the durable worker and repository. MSU keeps its worker in the existing host supervisor; its quiz service calls `send_response` and `edit_response` directly, including from background jobs.

`@web("POST", "/path")` and `bind_web(app, adapter)` attach ordinary bound request methods to a host HTTP router. Auth, body limits, request services, transactions and native responses remain with that host. Shared services can serve Telegram, jobs and HTTP; a live request transaction must never live on shared `self`.

## Composition, inheritance and diagnostics

`App().include(feature)` preserves feature order. `build_router()` returns a fresh native router for an existing dispatcher; the host wraps its runtime in `async with app.lifespan()`. `create_dispatcher()`/`run_polling()` own standalone startup, feature lifespans, shutdown and FSM cleanup. Pass `close_bot_session=True` only when transferring session ownership to polling. Resource factories unwind in reverse order, including partial startup failures.

For a mixed bot whose native routing order must stay intact, `app.register(router, feature.command, feature.callback)` installs selected declared methods at that position. All stacked declarations are included in order; aliases, filters and flags remain on the methods. Services still come from native middleware. Register only methods of included features; duplicate registration on the same router is rejected. Each native router has one App owner so dependency defaults and ownership registries cannot mix. The host retains startup, shutdown and FSM ownership. A clean native handler needs no forwarding Feature merely to use platform delivery helpers.

Use `App.run_polling()` as the polling owner. Cancelling it requests native stop and joins the poller and stop waiter before closing feature resources or an owned session; cancellation during startup unwinds startup before polling tasks are created. Repeated cancellation cannot abandon that cleanup. Resources enter and exit in the same task and context. The caller's cancellation or the error reported by aiogram remains primary if application cleanup also fails, with the cleanup failure attached as its cause. Native aiogram shutdown hooks retain aiogram's exception policy. A failed update drain leaves resources and the session open for the owner to finish shutdown. Stop or cancel and await the polling owner before separately calling `aclose()`.

The standalone dispatcher executes returned native `TelegramMethod` values before leaving update admission, including native routes and slow webhook processing; it sends through the Bot API instead of returning methods for an HTTP response shortcut. Embedded native hosts keep their own transport semantics.

Standalone shutdown closes update admission, drains for `drain_timeout` (30 seconds), cancels remaining updates and joins for `cancel_timeout` (5 seconds) before closing resources. `DrainTimeout` leaves resources open rather than closing clients beneath a handler that refuses cancellation; the owner must retry shutdown or terminate the process. An active handler cannot call `app.aclose()` itself. Embedded hosts must stop admission and drain their own updates before leaving the feature lifespan.

Override `Feature.lifespan` for resources/background workers; stop and join them before returning. Imports and constructors must not perform network work. App data is injected by name; invocation-specific middleware data wins. Native aiogram middleware remains the injection and instrumentation mechanism. A host with its own FSM installs an async zero-argument `_teleforge_release_isolation` callback in middleware data. It must release the current task’s already-selected state scope; TeleForge guards subsequent injected FSM access. A host without that bridge cannot promise early/coalescing cards while holding FSM isolation. Hub’s `HubIsolationBridge` adapts its existing scope.

Declarations compile after class creation. Effective parameter sources, native state constraints and static card/step/job relationships share the same checks used by inspection and dispatch. External middleware availability and dynamic filters remain host responsibilities. Automatic inherited route declarations are an advanced extension convention; ordinary helper composition is sufficient for most features. An undecorated override retains its inherited route and position. A new decorator replaces it; `@disable` removes it. Give reusable subclasses explicit stable feature keys. Signatures and direct calls remain ordinary Python.

```sh
PYTHONPATH=packages/teleforge/examples uv run --no-sync teleforge inspect quickstart:make_app --json
PYTHONPATH=packages/teleforge/examples uv run --no-sync teleforge check quickstart:make_app
uv run --no-sync pytest -q packages/teleforge/tests
```

Factories must be side-effect-free. The manifest shows identities, signatures, source locations, policies and dynamic filter names; dependency values are omitted. Inspection cannot prove authorization, provider availability or storage correctness. Exercise those through real service fixtures and native dispatch tests.
