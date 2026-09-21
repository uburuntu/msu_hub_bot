# Commands, prepared inputs and replies

A command declares its inputs beside its async handler. Ordinary arguments come
from the typed signature; `MetaInfo` holds the same resolved inputs, their source
messages and the reply policy. Services come from aiogram workflow/middleware
injection by parameter name.

```python
from aiogram.utils.formatting import Code, Text
from msu_hub_bot.telegram.command_api import Argument, MetaCommand

@MetaCommand("roll", "ролл", digits=Argument(clamp=(1, 100)))
async def process_roll(digits: int = 3) -> Text:
    value, name = get_roll(digits)
    return Text(Code(value), f" — {name}" if name else "")
```

Register with `register_command(router.message, process_roll, *filters,
flags=...)` in `routing.py`, keeping its ordered slot, access rules and
`StateFilter`. The declaration does not invent permissions or FSM behavior.
The existing `telegram.filters.MetaCommand` remains the filter for handlers
using the event-first API; the decorator lives in `telegram.command_api`.
Both share the same slash, caption, hashtag and alias parser.

A direct Python call runs the typed function only. Use
`await invoke_command(process_roll, message, **services)` for internal dispatch
that needs preparation, resource ownership and delivery too. The mention-based
PDF route is an example. Handler names/flags remain stable for middleware and
telemetry.

## Arguments and source selection

- `str`, `int`, `float`, `bool`, enum and string-literal parameters consume positional
  tokens. Optional scalar types and Pydantic `Annotated` constraints are supported.
  An absent or invalid value uses the signature default. Without a default it
  produces usage guidance. `Argument(strict=True)` rejects invalid supplied
  tokens even when a default exists; clamping is explicit.
- Declare `text=TextInput(...)` to receive the remaining text instead of one
  token. Valid leading arguments are removed; an invalid token and everything
  after it remain available as text or resolver input.
- Text uses the invocation first, then the replied-to message when `reply=True`.
  A default such as `text: str = "kek"` applies if neither provides text.
  `document=True` also permits bounded UTF-8 text documents. `max_chars` and
  `max_bytes` are hard limits: exceeding them gives guidance, never default text.
- Media uses eligible invocation content, then eligible reply content. Avatar
  fallback is explicit. `MediaInput(kinds=("image", "video"), avatar=True)`
  ignores unrelated documents/audio before considering a reply or avatar.
- Required-input failures reply to the invocation. Successful output follows
  the selected media source, otherwise the selected text source. Thus `/meme Hi`
  replying to a photo uses `Hi` and replies to the photo. Override with
  `await meta.reply(..., to=message)` when a command needs another target.
- `meta.resolved` contains prepared declared values; `meta.arguments` retains
  raw argument tokens. `meta.raw_text` retains the unparsed tail. Existing
  `meta.extract_text()` and media extractors reuse prepared values/sources.
  `meta.input_sources[name]` identifies the source of each input.
  `meta.reply_target()` is the legacy source selector; `await meta.reply(...)`
  sends output.

Declarations support one text input and one media input per invocation. Multiple
media kinds can share a `MediaInput`; multi-file workflows can use explicit
extraction/native aiogram inside their handler. Missing inputs give brief guidance;
conversations remain explicit FSM handlers with `/cancel`.

## Media representation and ownership

Use `ImageInput`, `VideoInput`, `DocumentInput` or `MediaInput` for acquisition
rules; the parameter annotation selects its representation:

| Annotation | Prepared value and lifetime |
| --- | --- |
| Native Telegram media type/union | Selected reference; no eager download. The consuming branch must use a bounded downloader. |
| `bytes` | Downloaded bytes within the declared budget. |
| `io.BytesIO` | Downloaded stream, kept open through sending and then closed. |
| `Path` | Temporary downloaded file, removed after sending. |
| `PIL.Image.Image` | Decoded, dimension-checked image, closed after sending. |

A signature default of `None` makes an input optional. Native references let
handlers preserve worker admission and partial-success behavior; for example,
translation can report a failed image while retaining a successful text result.
Managed downloads enforce their byte budgets on actual streamed data, including
files whose Telegram metadata has no size. CPU-heavy transformations still use
the injected executor and [media execution](media-execution.md) helpers.

```python
from pathlib import Path
from msu_hub_bot.telegram.command_api import DocumentInput, MetaCommand, MetaInfo

@MetaCommand("pdf", document=DocumentInput(reply=True))
async def pdf(document: Path, meta: MetaInfo):
    with await convert_document(document) as converted:
        await meta.reply(document=converted, fixed=True)
```

Adapter-owned input resources stay alive through returned-result delivery.
Caller-owned output resources must stay open inside an awaited `meta.reply`.
Do not return a temporary path from an exited `with` block. Returning owned
`bytes` avoids a resource lifetime; returning media also requires a declared
`output="photo"`, `"video"`, `"audio"` or `"document"`.

## Output policy

Return literal `str` or aiogram `Text`/`Bold`/`Pre`/other formatting objects for a
complete result. Literal strings never become HTML. Alternatively use the same
policy explicitly:

```python
await meta.reply(Text("Result: ", Code(label)), photo=image)
```

`meta.reply` accepts one media item per call, native formatting entities,
keyboards, video dimensions/duration and streaming metadata. Normal calls return
`list[Message]`. Already-sent messages/lists, `None` and handled booleans are not
sent again by the adapter.

Photo plus text prefers Rich Messages when its formatting can be represented.
Otherwise the platform plans native media and formatted text parts, preserving
all content. `rich=False` disables Rich conversion. It does not change literal
string semantics. Network failures never trigger an automatic alternate send.

The default soft budget is three messages. If text needs more, its complete
UTF-8 content becomes a `.txt` attachment; accompanying media is retained.
`soft_messages=1` keeps figlet output to one message/file. A response containing
media and overflowing text still needs two messages to preserve both the media
and complete text file, even with a soft budget of one. `max_output_bytes`
bounds the total known bytes of text, entity metadata and media (default 20 MiB,
up to 50 MiB);
Telegram's smaller per-kind limits still apply. Input/work limits such as
figlet's 200 characters protect processing before output exists. Splitting
respects entity boundaries and UTF-16 offsets. Text-file fallback contains
literal text rather than interactive formatting/entities.

For an editable game message or another feature that needs its ID:

```python
sent = await meta.reply(
    caption, photo=board, entities=entities, reply_markup=buttons,
    fixed=True, request_timeout=15,
)
```

`fixed=True` returns one native `Message` and forbids Rich conversion, splitting
and file substitution. Oversized captions fail before sending. Keep the feature's
publication marker, durable binding and callback reconciliation around this call;
a send is not a database transaction. Chess and GeoGuess demonstrate this boundary.

Replies preserve chat, topic, business context and reply threading. Each complete
response uses the shared per-chat send lane. Local planning errors give guidance;
`ResponseDeliveryError` records confirmed message IDs, attempted part and whether
delivery is uncertain. Never retry an uncertain publication blindly. Cancellation
propagates, and managed acquisition/delivery resources close after the platform
worker stops using them. Arbitrary executor jobs still require immutable snapshots
or worker-owned resources; see [media execution](media-execution.md).

Media strings normally mean Telegram `file_id`. A trusted provider URL can opt
into `allow_remote_media=True`; Telegram then fetches it, as in GeoGuess. The bot
cannot account for remote or reused `file_id` bytes locally, so only Telegram's
limits apply to that media. General user-supplied URLs belong in the bounded provider download layer.
Other specialized Telegram operations remain available through native aiogram.

## Optional argument resolution

`resolve=resolve_languages, context_messages=5` opts a command into a resolver.
The resolver receives parsed values (or `None`), `MetaInfo` and injected services,
then returns a mapping of declared scalar/text values. The platform revalidates
it before calling the handler. Media preparation happens afterward.

The resolver owns deterministic normalization and calls a model only for unresolved
choices. `/tr en ru Hello` normalizes against the cached `/langs` catalogue and
bypasses Jev. `/tr на русский` replying to text can ask Jev to choose supported
languages. Explicit valid codes win. Ambiguous inline instructions require a
colon or quoted source body; the model never rewrites the text being translated.
Image pixels are not sent to the language resolver.

Recent context is a bounded in-memory window of preceding human messages in the
same chat and topic, excluding the current message and duplicate reply. It starts
empty after restart and expires after 24 hours. Up to five messages plus the
request/reply can inform translation; it is not a proactive chat classifier.
Language inference samples up to 2,000 characters of selected text and 1,000
characters per recent snippet. The full selected text still reaches translation.
Model failures or unresolved choices produce usage guidance. Provider telemetry
records bounded numeric usage and failures, never prompts or conversation text.
