# Delivering saved chat stickers

`/sc` adds artwork to the chat's regular sticker pack. Chat administrators can use
images, GIF/video clips, existing regular stickers, or a custom emoji from a
message or caption. Pack replacement, reordering, title editing and thumbnails
remain in Telegram's apps.

## Input and search metadata

`/sc 😀🔥 | кот, мем` assigns associated emoji and comma-separated search keywords.
Emoji and keywords are deduplicated, preserving order; keyword comparison ignores
case. There can be 1–20 emoji and at most 20 keywords with 64 characters in total.
Invalid limits produce a helpful reply before downloading or saving artwork.
An empty keyword section (`/sc |`) clears keywords on an existing sticker.

Without explicit metadata, the existing emoji fallback remains: emoji from the
replied-to text/caption, or `✨`. Reusing an existing sticker without explicit
metadata leaves its saved emoji and keywords unchanged. Metadata changes apply
to the registered sticker in the destination pack, never the source pack.

Attached or replied-to images, videos and stickers keep precedence over custom
emoji in their captions. Send a caption emoji separately to select its artwork.
Repeated instances of the same custom emoji count as one source. When a message
contains several different custom emoji, the user is asked to send the desired
one separately. API results are matched by custom-emoji ID; an unavailable ID
never falls back to unrelated artwork or a profile photo.

Custom emoji are converted to regular sticker artwork. Static/video artwork is
resized; TGS animation keeps its vector layers and timeline inside a scaled
512-pixel canvas. Repaintable emoji depend on Telegram's surrounding text colour;
they are explicitly rejected with a request for an image or clip in the desired
colour. This command does not create native custom-emoji packs.

## Media preparation and resource limits

Valid Telegram-registered regular WEBP/TGS/WEBM stickers reuse their file ID.
They need no encoding or upload, preserving the source artwork. Preview lookup
may lazily download the source if Telegram changes its copied identity.
Other inputs pass through [sticker_media.py](../src/msu_hub_bot/media/sticker_media.py).
Worker admission is acquired before downloading; the byte limit is enforced on
both declared size and streamed content. Temporary files and decoded image cores
are released on success and failure.

Video and animated-image input uses at most the first seven seconds of one source
cycle. Excerpts longer than three seconds are accelerated to approximately 2.95
seconds. Clipping is applied before frame acceleration. A successful save adds
“✂️ Для стикера использованы только первые 7 секунд.” when content was trimmed,
including through the pending title prompt and pack-link fallback.

Pillow decodes APNG/animated WEBP with their frame timing, alpha, blending and
disposal. A separate APNG poster is excluded. Loop counts do not repeat the source
cycle; Telegram loops the resulting sticker. Nonpositive or malformed frame
durations are rejected. Resized RGBA frames feed a generated local timeline into
VP9 encoding. A valid result above 256 KiB gets one stronger compression attempt;
the source, dimensions, alpha, frame rate and timeline stay unchanged. Decode or
encoder failures and timeouts are not retried. If the second result is still too
large, the bot asks for a simpler or shorter excerpt.

| Input or operation | Enforced limits |
| --- | --- |
| Downloaded input | 20 MiB; decoded dimensions at most 16 megapixels and 8192 pixels per side. |
| Animated PNG/WEBP | 300 source frames, 64 Mi decoded pixels traversed and 64 MiB of temporary frame files. |
| Video output | Silent VP9 WEBM, longest side 512 pixels, at most three seconds, 30 FPS and 256 KiB. |
| Static output | Lossless WEBP, longest side 512 pixels, at most 512 KiB. |
| Existing TGS | At most 64 KiB compressed and 2 MiB expanded JSON; positive duration at most three seconds. Arbitrary TGS documents are rejected. |
| FFmpeg/FFprobe | Input probing has a 60-second timeout. Both encoding attempts and their result probes share another 60 seconds; process groups are terminated and reaped. Probe output is capped at 1 MiB. |

Uploaded inputs use a demuxer allowlist for media containers and file/pipe
protocols; playlists cannot cause additional file or network reads. Only the
generated local frame timeline enables the concat demuxer.

Decoder/encoder threads and filter threads are explicitly bounded. Caller timeout
or cancellation cannot stop a running Python worker; its admission slot remains
occupied until actual completion. The worker and process limits complement the
container limits documented in [deployment operations](deployment.md).

## Save and preview contract

`StickerSetClient` owns upload, add/create, metadata and lookup calls. Handlers
own permissions, title prompts and replies. `uploadStickerFile` returns an upload
reference; that ID is not proof that the file can be sent as a registered sticker.

- `UploadedSticker` separates the upload/reference ID from identity metadata.
  Only supported `InputSticker` fields go to Telegram.
- Preview lookup matches `file_unique_id` and format, handling reordered packs,
  duplicate entries and concurrent additions. If Telegram assigns a new identity,
  lookup can compare registered bytes with the prepared payload's SHA-256. Reused
  files without a fingerprint are downloaded lazily only after identity matching
  fails; this also supports older pending title prompts.
- Lookup permits three pack reads, one optional source fingerprint download and
  three bounded candidate downloads within five seconds. Each download is capped
  at 512 KiB or the known smaller candidate size. Pack order only prioritizes
  candidate downloads; it never selects a reply without an identity or content match.
- Telegram can re-encode a cross-pack copy, changing both identity and bytes.
  Such a copy cannot be reliably matched and falls back to a pack link; perceptual
  similarity and pack order are not used to guess a target.
- An unresolved or rejected preview falls back to a pack link. The upload is never
  sent as a fallback, and the last pack entry is never guessed.
- A known destination file ID or unique identity in the same regular format skips
  addition entirely, including after a concurrent pack-creation check.
- Otherwise, duplicate additions can be a Telegram no-op. Explicit emoji/keyword
  updates therefore run after resolving the destination sticker, including when
  another administrator added the same artwork concurrently.
- If metadata updating or its target cannot be confirmed, the reply confirms the
  successful save and explains that the emoji/keyword update is unconfirmed.
  Preview or metadata failure never repeats a confirmed save.
- Successful creation clears its own pending title state before preview delivery.
  A definite creation rejection permits an existence check for a concurrent pack
  creation; uncertain network results from mutations are not retried.

Pending titles retain `mixed_sticker` and `sticker_upload`, including metadata and
trim notices. Older prompts still load and can recover the upload identity with
`getFile`; an unavailable preview still returns the pack link.

Offline tests cover identity, metadata, ownership, concurrency, failure recovery,
limits and native animation timing/transparency. The production-image smoke check
also exercises APNG/WEBP conversion and a rendered normalized TGS canvas.

References: [Bot API sticker methods](https://core.telegram.org/bots/api#stickers)
and [InputSticker formats and metadata](https://core.telegram.org/bots/api#inputsticker).
