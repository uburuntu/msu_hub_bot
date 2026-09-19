# Bounded media execution

The application shares three worker slots and permits three waiting jobs.
Further requests receive a short busy reply; automatic video previews quietly
skip work while saturated. Queueing, input preparation, execution and one
broken-pool recovery share the caller's deadline.

Use `TPExecutor.run_prepared` when a job needs expensive inputs: its async
factory runs only after acquiring a worker slot. For Telegram files,
`telegram.media_jobs.run_downloaded` limits the actual stream to 20 MiB,
closes the download buffer, and transfers immutable bytes to the worker.
Waiting jobs retain media references, not downloaded payloads. Metadata size
checks are an early rejection; the streaming limit remains authoritative.

Caller cancellation releases a queued or preparing job. A running thread keeps
its slot until it actually finishes, even after its caller times out. Native
work therefore needs its own deadline. `/cancel` clears a conversation draft;
it does not cancel an already-started conversion.

`execution.process.run_process` starts a separate process group, discards
diagnostic output, optionally bounds captured stdout, kills remaining group
members on exit, and waits for the direct child. Docker's init process reaps
orphaned descendants. Temporary workspaces and image buffers belong to the
worker, so a caller timeout cannot remove inputs while native work still uses
them. Regex evaluation uses a disposable isolated interpreter with its own
deadline and input/output limits.

| Input or operation | Bound |
| --- | --- |
| Downloaded worker input | 20 MiB |
| Decoded image/video dimensions | 16 million pixels; 8192 pixels per side |
| Reverse-filter buffering | Conservative estimate of at most 512 MiB per job, including video frames and decoded audio |
| Shared FFprobe / FFmpeg | 10 / 120 seconds; captured probe output at most 1 MiB |
| Shared FFmpeg output | 50 MiB; oversized results are rejected |
| OCR | 60 seconds; recognized text at most 1 MiB |
| Speech preprocessing batch | 120 seconds total and 64 MiB of PCM; partial batches are discarded |

Native input uses a local-protocol and media-container allowlist. Uploaded
playlists cannot open other local files or fetch network resources. Only the
internally generated sticker frame timeline enables concatenation.

Oversized reverse jobs are rejected with advice to shorten the input or lower
its resolution; their content is never silently trimmed. Sticker clips have
their own documented seven-second policy and stricter animation/output bounds
in [sticker delivery](sticker-delivery.md). Native tools and Pillow can allocate
beyond simple input estimates, so [container resource limits](deployment.md#resource-budget)
provide the final boundary.

## Captions

`/l` overlays Lobster lettering, `/de` puts a centered caption below a black
demotivator frame, and `/meme` puts black sans-serif text in a white panel above
the media. Supply text with the command and attach an image/video, or reply to
one. Explicitly attached media takes precedence over media in the replied-to
message; the existing profile-photo fallback remains available.

The three styles share measured text layout for images and videos. Captions
wrap and shrink to fit instead of imposing a separate character limit or
truncating text. Paragraph breaks are retained; repeated blank lines are
compacted. Panel captions grow within the canvas budget before shrinking, and
visible glyph bounds determine centering. Very long text necessarily becomes
small. Source images are oriented and scaled to at most 1600 pixels per side;
extreme aspect ratios receive padding.

Video captions use a rendered PNG overlay rather than interpolating text into
FFmpeg syntax. Output uses H.264/AAC MP4 with even dimensions and fast-start
metadata, retaining the source timeline, variable frame timestamps and optional
audio. Source rotation and sample aspect ratio determine display geometry.
The same download, native execution and output limits above apply; clips are
never deliberately shortened to fit.

Worker telemetry separates queue delay, preparation, awaited execution and
actual completion. Late completion exports aggregate measurements without
request content. Admission and input-limit rejections are expected outcomes,
not unexpected incidents.

Regression checks cover saturation without downloading, cancellation and
timeout recovery, native process-tree cleanup, resource ownership, malformed
inputs and normal conversions. Pathological regex checks run under an
independent process watchdog so a GIL regression cannot freeze the test runner.

Speech recognition shares three outgoing chunk slots across all messages and configured Wit tokens. The 180-second recognition deadline includes waiting for capacity and provider throttling; the complete download/preparation/recognition pipeline has a 240-second deadline. Chunk order and the existing unrecognized-fragment markers are preserved. Failed or timed-out batches never emit a misleading partial transcript, and all child requests settle before audio buffers are released.

[Provider tools](provider-tools.md) describes document conversion, sourced lookups and the offline background-removal model, including build and resource limits.
