import heapq
import io
import itertools
import random
import re
import string
import time
from datetime import datetime, timedelta
from functools import wraps
from itertools import chain, islice, tee
from operator import itemgetter
from typing import Generic, Iterator, Callable, Hashable, Iterable, List, Optional, TypeVar, AnyStr

import pendulum
from PIL import Image

T = TypeVar('T')


def attributes(it: Iterable, name: str):
    return [getattr(obj, name) for obj in it if hasattr(obj, name)]


def call(it: Iterable[Callable], *args, **kwargs) -> list:
    return [f(*args, **kwargs) for f in it]


class FakeBytesIO(io.BytesIO):
    def close(self) -> None:
        self.seek(0)
        return


def bytes_io(data: AnyStr, filename: str = None) -> io.BytesIO:
    file = FakeBytesIO(data)
    if filename:
        file.name = filename
    file.seek(0)
    return file


def image_bytes_io(image: Image.Image, filename: str = 'image', ext: str = 'jpeg') -> io.BytesIO:
    file = FakeBytesIO()
    image.save(file, format=ext)
    file.name = f'{filename}.{ext}'
    file.seek(0)
    return file


def prettify_number(n: int, sep: str = '’') -> str:
    s = str(n)[::-1]
    return sep.join(s[i:i + 3] for i in range(0, len(s), 3))[::-1]


def prettify_duration(seconds: int) -> str:
    td = timedelta(seconds=seconds)
    return str(td)


def prettify_bytes(size: float) -> str:
    for unit in ('Б', 'Кб', 'Мб', 'Гб', 'Тб'):
        if size < 1024.0:
            break
        size /= 1024.0
    return f'{size:.0f} {unit}' if unit in ('Б', 'Кб') else f'{size:.1f} {unit}'


def megabytes(size: float) -> float:
    """Megabytes in bytes"""
    return size * 1024 * 1024


def one_liner(s: str, cut_len: int = None) -> str:
    s = s.replace('\n', ' ')
    while '  ' in s:
        s = s.replace('  ', ' ')
    return s[:cut_len] if cut_len else s


def strip_blank_rows(s: str) -> str:
    text = '\n'.join(map(str.strip, s.split('\n')))
    while '\n\n\n' in text:
        text = text.replace('\n\n\n', '\n\n')
    return text


def random_cycle(*args: T) -> Iterator[T]:
    it = list(args)
    random.shuffle(it)
    return itertools.cycle(it)


def chunks(iterable, size=10):
    iterator = iter(iterable)
    for first in iterator:
        yield list(chain([first], islice(iterator, size - 1)))


def parse_int(s: str, default: int = None, bound_l: int = float('-inf'), bound_r: int = float('inf')) -> Optional[int]:
    if not s.isdigit():
        return default

    return min(max(int(s), bound_l), bound_r)


def shorten(text: str, width: int = 32, placeholder: str = '...') -> str:
    if len(text) <= width:
        return text

    if width <= len(placeholder):
        return placeholder

    first_cut, last_cut = width // 2, -max(width // 2 - len(placeholder), 1)
    return text[:first_cut] + placeholder + text[last_cut:]


def outdated(dt: datetime, curr_dt: datetime = None):
    if curr_dt is None:
        curr_dt = datetime.now(dt.tzinfo)
    return curr_dt > dt


def unique_by(a: Iterable[T], key: Callable[[T], Hashable] = itemgetter('id')) -> List[T]:
    return list({key(i): i for i in a}.values())


def percent_chance(percent: float) -> bool:
    if percent < 0. or percent > 100.:
        raise ValueError(f'`percent` should be between 0. an 100., not {percent}')
    chance = percent / 100.
    return random.random() < chance


def is_en(text: str) -> bool:
    en = string.printable
    return sum(c in en for c in text) / len(text) > 0.8


class PriorityQueue(Generic[T]):
    def __init__(self) -> None:
        self._data: list[tuple[int, T]] = []

    def head(self, default: T | None = None) -> T | None:
        if self._data:
            return self._data[0][1]
        return default

    def head_with_priority(self, default: T | None = None, default_priority: int = 0) -> tuple[T | None, int]:
        if self._data:
            return self._data[0][1], self._data[0][0]
        return default, default_priority

    def put(self, priority: int, value: T) -> None:
        heapq.heappush(self._data, (priority, value))

    def pop(self) -> T:
        return heapq.heappop(self._data)[1]


def retry(exception=Exception, retries_count=5, sleep_for=0.):
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            for retry in range(1, retries_count + 1):
                try:
                    return func(self, *args, **kwargs)
                except exception:
                    if retry == retries_count:
                        raise
                    time.sleep(sleep_for)

        return wrapper

    return decorator


def two(iterable, with_bound=False, prev=None, next=None):
    prevs, currs = tee(iterable, 2)
    if with_bound:
        prevs = chain([prev], prevs)
        currs = chain(islice(currs, 0, None), [next])
    else:
        prevs = prevs
        currs = islice(currs, 1, None)
    return zip(prevs, currs)


def three(iterable, with_bound=False, prev=None, next=None):
    prevs, currs, nexts = tee(iterable, 3)
    if with_bound:
        prevs = chain([prev], prevs)
        currs = currs
        nexts = chain(islice(nexts, 1, None), [next])
    else:
        prevs = prevs
        currs = islice(currs, 1, None)
        nexts = islice(nexts, 2, None)
    return zip(prevs, currs, nexts)


def cut_long_text_yield(text: str, soft_max_len: int = 4000, hard_max_len: int = 4096):
    """
    Cut long text by new-line, space or dot symbols.
    """
    last_cut = 0
    nl_anchor, dot_anchor, space_anchor = 0, 0, 0
    soft_max_len = min(soft_max_len, hard_max_len)

    if len(text) < hard_max_len:
        yield text
        return

    for i in range(len(text) - 1):
        if text[i] == '\n':
            nl_anchor = i + 1
        if text[i] == '.' and text[i + 1] == ' ':
            dot_anchor = i + 2
        if text[i] == ' ':
            space_anchor = i

        if i - last_cut > soft_max_len:
            if nl_anchor > last_cut:
                yield text[last_cut:nl_anchor]
                last_cut = nl_anchor
            elif dot_anchor > last_cut:
                yield text[last_cut:dot_anchor]
                last_cut = dot_anchor
            elif space_anchor > last_cut:
                yield text[last_cut:space_anchor]
                last_cut = space_anchor
            else:
                yield text[last_cut:i]
                last_cut = i

            if len(text) - last_cut < soft_max_len:
                yield text[last_cut:]
                return

    yield text[last_cut:]


def cut_long_text(text: str, soft_max_len: int = 4000, hard_max_len: int = 4096) -> List[str]:
    return list(cut_long_text_yield(text, soft_max_len, hard_max_len))


def clear_html(text: str) -> str:
    """Clear text from HTML tags"""
    text = re.sub(r'<br>', '\n', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&#47;', '/', text)

    text = re.sub(r'<.*?>', '', text)
    return text


re_filename = re.compile(r'(?u)[^-\w.]')


def valid_filename(s: str, length: int = None) -> str:
    """
    Return the given string converted to a string that can be used for a clean
    filename. Remove leading and trailing spaces; convert other spaces to
    underscores; and remove anything that is not an alphanumeric, dash,
    underscore, or dot.
    >>> valid_filename("john's portrait in 2004.jpg")
    'johns_portrait_in_2004.jpg'
    """
    s = str(s).strip().replace(' ', '_')
    s = re_filename.sub('', s)
    return s if s is None else s[:length]


class RandomizerForDay:
    tz = pendulum.timezone('Europe/Moscow')
    until_ts = 0

    @classmethod
    def random(cls, user_id: int):
        curr_ts = time.time()
        if curr_ts > cls.until_ts:
            cls.until_ts = pendulum.tomorrow(tz=cls.tz).int_timestamp
        return random.Random(cls.until_ts + user_id)
