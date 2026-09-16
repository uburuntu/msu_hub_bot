import re
from typing import List

import aiohttp

from common.externals.exceptions import BadRequestError, NotFoundError


async def urban_dictionary(query: str = None) -> List[dict]:
    """Use the dictionary's JSON endpoint and preserve the command's result fields."""
    endpoint = 'define' if query else 'random'
    params = {'term': query} if query else None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.get(f'https://api.urbandictionary.com/v0/{endpoint}', params=params) as response:
            if response.status == 404:
                raise NotFoundError()
            if response.status != 200:
                raise BadRequestError()
            try:
                result = await response.json()
                definitions = result['list']
                if not isinstance(definitions, list):
                    raise TypeError
                records = []
                for definition in definitions:
                    text = [definition[key] for key in ('word', 'definition', 'example')]
                    if not all(isinstance(value, str) for value in text):
                        raise TypeError
                    # Square brackets mark dictionary links; the command displays their visible text.
                    text = [re.sub(r'\[([^\[\]]+)\]', r'\1', value) for value in text]
                    records.append({
                        'header': text[0].strip(),
                        'meaning': text[1].replace('\r\n', '\n').replace('\r', '\n').strip(),
                        'example': text[2].replace('\r\n', '\n').replace('\r', '\n').strip(),
                        'up': int(definition['thumbs_up']),
                        'down': int(definition['thumbs_down']),
                    })
                return records
            except (aiohttp.ContentTypeError, KeyError, TypeError, ValueError):
                raise BadRequestError() from None
