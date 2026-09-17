from functools import partial

import ujson as json

__all__ = (
    "dump",
    "load",
    "dumps",
    "loads",
    "json",
)

dump = partial(json.dump, escape_forward_slashes=False, ensure_ascii=False)
load = partial(json.load)

dumps = partial(json.dumps, escape_forward_slashes=False, ensure_ascii=False)
loads = partial(json.loads)
