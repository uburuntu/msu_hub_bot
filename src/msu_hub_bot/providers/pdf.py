"""Convert documents through PDF24's public upload/job/download protocol."""

import asyncio
import io
import html
import json
import random
import re
from pathlib import PurePosixPath

import aiohttp

from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES
from msu_hub_bot.providers.exceptions import BadRequestError
from msu_hub_bot.providers.http import USER_AGENT, read_json, read_limited

JOB_TIMEOUT_SECONDS = 180
MAX_PDF_BYTES = 50 * 1024 * 1024
TOOL_URL = "https://tools.pdf24.org/en/convert-to-pdf"


class PdfDocument(io.BytesIO):
    def __init__(self, data: bytes, filename: str) -> None:
        super().__init__(data)
        self.name = filename


def _upload_payload(data: bytes, filename: str, content_type: str) -> tuple[bytes, str, str]:
    if content_type != "text/plain" and PurePosixPath(filename).suffix.lower() not in {".txt", ".text", ".log"}:
        return data, filename, content_type
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("cp1251")
    except UnicodeError:
        raise BadRequestError() from None
    # PDF24's plain-text converter uses WinAnsi fonts. Safe HTML preserves Cyrillic
    # and line wrapping without allowing text to become markup or fetch resources.
    document = (
        '<!doctype html><html><head><meta charset="utf-8"></head><body>'
        '<pre style="font-family: DejaVu Sans, sans-serif; white-space: pre-wrap">' + html.escape(text) + "</pre></body></html>"
    ).encode()
    if len(document) > MAX_DOWNLOAD_BYTES:
        raise BadRequestError()
    return document, PurePosixPath(filename).stem + ".html", "text/html"


async def _select_worker(session: aiohttp.ClientSession) -> str:
    async with session.get(TOOL_URL, allow_redirects=False) as response:
        page = (await read_limited(response, 512 * 1024)).decode("utf-8")
    match = re.search(r"pdf24\.workerServers\s*=\s*(\[.*?\]);", page)
    if match is None:
        raise BadRequestError()
    try:
        servers = json.loads(match.group(1))
    except ValueError:
        raise BadRequestError() from None
    if not isinstance(servers, list):
        raise BadRequestError()
    hosts = list(
        dict.fromkeys(
            entry["host"]
            for entry in servers
            if isinstance(entry, dict)
            and isinstance(entry.get("host"), str)
            and re.fullmatch(r"filetools[0-9]{1,3}\.pdf24\.org", entry["host"])
        )
    )
    random.shuffle(hosts)
    for host in hosts[:3]:
        endpoint = f"https://{host}/client.php"
        try:
            async with session.post(endpoint, params={"action": "start"}, allow_redirects=False) as response:
                ready = await read_json(response)
            if isinstance(ready, dict) and ready.get("available") is True:
                return endpoint
        except aiohttp.ClientError, BadRequestError, TimeoutError:
            continue
    raise BadRequestError()


async def convert_to_pdf(file: io.BytesIO, filename: str, content_type: str) -> PdfDocument:
    if file.getbuffer().nbytes > MAX_DOWNLOAD_BYTES:
        raise BadRequestError()
    name = PurePosixPath(filename.replace("\\", "/")).name[:180] or "document"
    payload, upload_name, upload_type = _upload_payload(file.getvalue(), name, content_type)
    try:
        async with (
            asyncio.timeout(JOB_TIMEOUT_SECONDS),
            aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=30, connect=10)) as session,
        ):
            endpoint = await _select_worker(session)
            # FormData owns its stream; never close or consume the caller's buffer.
            data = aiohttp.FormData()
            data.add_field("file", payload, filename=upload_name, content_type=upload_type)
            async with session.post(endpoint, params={"action": "upload"}, data=data, allow_redirects=False) as response:
                uploaded = await read_json(response)
            if not isinstance(uploaded, list) or len(uploaded) != 1 or not isinstance(uploaded[0], dict):
                raise BadRequestError()
            if not isinstance(uploaded[0].get("file"), str):
                raise BadRequestError()
            async with session.post(
                endpoint,
                params={"action": "convertToPdf", "srcPageId": "convertToPdf"},
                json={"files": uploaded, "pageLangCode": "ru", "language": "ru"},
                allow_redirects=False,
            ) as response:
                created = await read_json(response)
            if not isinstance(created, dict) or not isinstance(created.get("jobId"), str) or not created["jobId"]:
                raise BadRequestError()
            job_id = created["jobId"]
            while True:
                async with session.post(
                    endpoint,
                    params={"action": "getStatus"},
                    data={"jobId": job_id},
                    allow_redirects=False,
                ) as response:
                    status = await read_json(response)
                if not isinstance(status, dict):
                    raise BadRequestError()
                if status.get("status") == "done":
                    job = status.get("job")
                    if not isinstance(job, dict) or job.get("0.state") != "3":
                        raise BadRequestError()
                    break
                if status.get("status") != "pending":
                    raise BadRequestError()
                await asyncio.sleep(1)
            async with session.get(
                endpoint,
                params={"action": "downloadJobResult", "jobId": job_id},
                allow_redirects=False,
            ) as response:
                result = await read_limited(response, MAX_PDF_BYTES)
            if not result.startswith(b"%PDF-"):
                raise BadRequestError()
            return PdfDocument(result, filename=PurePosixPath(name).stem[:160] + ".pdf")
    except aiohttp.ClientError, UnicodeError:
        raise BadRequestError() from None
