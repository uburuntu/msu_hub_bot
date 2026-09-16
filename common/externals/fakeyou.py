import asyncio
import uuid
from enum import Enum

import aiohttp

from common import json
from common.externals.exceptions import BadRequestError

JOB_TIMEOUT_SECONDS = 180


class Voices(str, Enum):
    homer = "TM:r9mxvgcybyy5"
    sinatra = "TM:70nmn1mmqfw8"
    vader = "TM:d4p96mxa4da9"
    queen = "TM:4jhmevqnrqp5"
    glados = "TM:fm4h94vk4eem"


async def fake_you(text: str, voice: str = Voices.homer) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:97.0) Gecko/20100101 Firefox/97.0",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.5",
        "referrer": "https://fakeyou.com/",
    }

    data = {
        "uuid_idempotency_token": str(uuid.uuid4()),
        "tts_model_token": voice,
        "inference_text": text,
    }

    async with asyncio.timeout(JOB_TIMEOUT_SECONDS), aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        url = 'https://api.fakeyou.com/tts/inference'

        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = json.loads(await response.read())

        token = result['inference_job_token']
        url = f'https://api.fakeyou.com/tts/job/{token}'

        while True:
            await asyncio.sleep(2)
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    raise BadRequestError()

                result = json.loads(await response.read())['state']
                if result['status'] == 'complete_success':
                    break
                if result['status'] not in ('pending', 'started'):
                    raise BadRequestError()

        path = result['maybe_public_bucket_wav_audio_path']
        return f'https://storage.googleapis.com/vocodes-public{path}'
