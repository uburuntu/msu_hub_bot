# Delivering saved chat stickers

`uploadStickerFile` returns a file for use in sticker-set operations. Its ID is
not proof that the uploaded document is ready to send as a sticker. `/sc` saves
that upload, then obtains the reply ID from the registered pack.

`StickerSetClient` contains the Telegram upload, add/create, and lookup calls.
Handlers retain media preparation, permissions, title prompts, and replies.

Video and GIF input uses at most the first seven seconds of the original
timeline. Excerpts longer than three seconds are accelerated to approximately
2.95 seconds. FFmpeg's input duration limit is applied before speeding up the
frames, so later content cannot enter the sticker. The 20 MiB input limit and
single encoding attempt still apply.

Prepared media carries a `trimmed` flag through the pending title prompt. A
successful save includes “✂️ Для стикера использованы только первые 7 секунд.”
when needed, including when preview lookup falls back to a pack link. Valid TGS
stickers retain their vector animation; malformed TGS timelines over three
seconds are still rejected.

- `UploadedSticker` carries the upload reference separately from its identity
  metadata. Only the required `InputSticker` fields go to Telegram.
- After a successful save, lookup matches `file_unique_id` and sticker format.
  This works with reordered packs, duplicate entries, and concurrent additions.
- If Telegram assigns a new identity, lookup can compare registered file bytes
  with the prepared upload's SHA-256. Pack order only prioritizes candidate
  downloads; it never selects the reply without a match.
- Lookup makes at most three pack reads and three content downloads, within a
  five-second overall deadline. Content candidates must match the prepared file
  size, which must be at most 512 KiB.
- If identity and content cannot be matched, or Telegram rejects the preview,
  the reply confirms the save with a pack link. It never sends the upload as a
  fallback or guesses the last sticker in the pack.
- Successful creation clears the pending title state before preview delivery.
  Upload, creation, and addition are not repeated to recover a preview. An
  uncertain network result from a mutation is propagated without retrying it.
- A definite creation rejection is followed by an existence check, allowing a
  title prompt to reuse a pack another admin just created.

Pending title prompts retain the existing `mixed_sticker` payload and add
`sticker_upload` metadata. Older prompts recover the upload's unique ID through
`getFile`; they can still save and return a pack link if preview matching fails.

Offline tests verify the registered ID passed to `reply_sticker`, all three
formats, concurrency, duplicate media, bounded lookup, cancellation, creation
races, and failure handling after a confirmed save.
Native MP4/GIF tests use a colour-coded timeline to verify that frames after
seven source seconds are excluded while the earlier segment remains present.

## Suggested coverage next

Tracked in [issue #7](https://github.com/uburuntu/msu_hub_bot/issues/7).

1. **Reuse existing regular stickers directly.** Avoid unnecessary encoding when
   the input already has a valid sticker format, dimensions, and duration. Cover
   static WEBP, vector TGS, VP9 alpha, and duplicate additions.
2. **Validate more animated image inputs.** Add explicit APNG and animated WEBP
   coverage, including transparency, loop timing, malformed metadata, and decoder
   availability in the production image. These depend on the media decoders.
3. **Accept custom emoji as source material.** Resolve custom emoji entities in
   text/captions through `getCustomEmojiStickers` and normalize their artwork to
   regular sticker dimensions. Native custom emoji output should use a separate
   pack type and rendering policy, including repainting where supported.
4. **Expose search metadata.** Support the API's 1–20 associated emoji and up to
   20 search keywords with 64 characters total; the command currently uses at
   most five emoji. Add editing through `setStickerEmojiList` and
   `setStickerKeywords`.
Pack replacement, reordering, title editing, and thumbnail management stay in
Telegram's apps, which provide a better interface for these operations.

These API operations can use the shared client on aiogram 2. A later aiogram 3
migration can replace the adapter with current typed models without mixing that
work into media conversion or command behavior.

References: [Bot API sticker methods](https://core.telegram.org/bots/api#stickers)
and [InputSticker formats and metadata](https://core.telegram.org/bots/api#inputsticker).
