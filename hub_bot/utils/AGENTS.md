# Bot service helpers

- This folder mixes FFmpeg conversion with JDoodle, Wit and Wolfram clients; [wit.py](wit.py) and [wolfram.py](wolfram.py) also contain registered Telegram handlers.
- [jdoodle.py](jdoodle.py) owns the compiler catalog and request models; language aliases and stdin conversations are assembled in [commands/prog.py](../commands/prog.py).
- [ffmpeg.py](ffmpeg.py) launches local processes and uses temporary files; changes must account for input flushing, cleanup and actual process termination.
- The [shared executor](../../common/executor.py) runs threads, so an await timeout alone does not stop conversion or other blocking work.
- Separate service results from Telegram presentation while preserving the command's input and output contract.
- Treat provider access, preprocessing and final Telegram delivery as separate checks; success at one stage does not validate the whole command.
- Trace registrations and callers before removing a helper; colocated functions may serve unrelated features.
