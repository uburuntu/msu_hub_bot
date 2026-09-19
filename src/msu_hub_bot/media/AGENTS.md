# Media processing

- Own caption layout, FFmpeg conversion and sticker preparation. Return prepared media; commands own copy/FSM and [Telegram sticker delivery](../telegram/sticker_sets.py) owns API mutations.
- [ffmpeg.py](ffmpeg.py) and [sticker_media.py](sticker_media.py) must bound inputs, process runtime and output, flush input files and clean up temporary files on every exit.
- The [executor](../execution/executor.py) runs threads; caller cancellation or timeout does not stop native work. Subprocesses need their own termination limits.
- Preserve established crop, font and timing behavior; test long captions and native decoding/encoding when affected. Lobster/demotivator changes do not authorize changing Wolfram crops.
- Follow [sticker delivery](../../../docs/sticker-delivery.md) for clip limits and format/preview contracts; successful conversion alone does not prove Telegram delivery.
- [background.py](background.py) runs only the checksum-verified local model in a bounded subprocess; [provider tools](../../../docs/provider-tools.md) owns model installation and processing limits. Never fetch weights during a user request.
