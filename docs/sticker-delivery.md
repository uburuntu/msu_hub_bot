# Delivering saved chat stickers

`uploadStickerFile` returns a file for use in sticker-set operations. Its ID is
not proof that the uploaded document is ready to send as a sticker. `/sc` saves
that upload, then obtains the reply ID from the registered pack.

`StickerSetClient` contains the Telegram upload, add/create, and lookup calls.
Handlers retain media preparation, permissions, title prompts, and replies.

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
