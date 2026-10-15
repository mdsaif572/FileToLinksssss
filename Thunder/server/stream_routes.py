# Thunder/server/stream_routes.py

import asyncio
import re
import secrets
import time
from typing import Optional
from urllib.parse import quote, unquote

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

from Thunder import __version__, StartTime
from Thunder.bot import StreamBot, client_channels, multi_clients, work_loads
from Thunder.server.exceptions import FileNotFound, InvalidHash
from Thunder.utils.bot_utils import quote_media_name
from Thunder.utils.canonical_files import (
    PUBLIC_HASH_LENGTH,
    get_file_by_hash,
    record_bin_channel,
    record_unavailable_reason,
    update_cached_file_id,
)
from Thunder.utils.custom_dl import ByteStreamer, client_label
from Thunder.utils.file_properties import get_media
from Thunder.utils.logger import logger
from Thunder.utils.metrics import metrics
from Thunder.utils.render_template import render_media_page, render_page
from Thunder.utils.time_format import get_readable_time
from Thunder.vars import Var

routes = web.RouteTableDef()

SECURE_HASH_LENGTH = 6
CHUNK_SIZE = 1024 * 1024
MAX_CONCURRENT_PER_CLIENT = getattr(Var, "MAX_CONCURRENT_PER_CLIENT", 16)
MAX_CONCURRENT_PER_USER = getattr(Var, "MAX_CONCURRENT_PER_USER", 8)
OVERLOAD_RETRY_AFTER_SECONDS = 2
RANGE_REGEX = re.compile(r"^bytes=(?P<start>\d*)-(?P<end>\d*)$")
PATTERN_HASH_FIRST = re.compile(
    rf"^([a-zA-Z0-9_-]{{{SECURE_HASH_LENGTH}}})(\d+)(?:/.*)?$")
PATTERN_ID_FIRST = re.compile(r"^(\d+)(?:/.*)?$")
VALID_HASH_REGEX = re.compile(r'^[a-zA-Z0-9_-]+$')
VALID_PUBLIC_HASH_REGEX = re.compile(rf'^[0-9a-f]{{{PUBLIC_HASH_LENGTH}}}$')
VALID_DISPOSITIONS = {"inline", "attachment"}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range, Content-Type, *",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Content-Disposition",
}

streamers = {}


class UserStreamLimiter:
    """Caps how many streams one uploader (or one caller IP) may hold open.

    Without a cap a single browser or download manager can occupy every slot of
    every Telegram client and everyone else gets 503.
    """

    def __init__(self, limit: int) -> None:
        self.limit = int(limit)
        self._active: dict = {}

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def acquire(self, key) -> bool:
        if not self.enabled or key is None:
            return True
        current = self._active.get(key, 0)
        if current >= self.limit:
            return False
        self._active[key] = current + 1
        return True

    def release(self, key) -> None:
        if not self.enabled or key is None:
            return
        current = self._active.get(key, 0) - 1
        if current <= 0:
            self._active.pop(key, None)
        else:
            self._active[key] = current

    def active(self, key) -> int:
        return self._active.get(key, 0)

    def total(self) -> int:
        return sum(self._active.values())


user_streams = UserStreamLimiter(MAX_CONCURRENT_PER_USER)


def get_streamer(client_id: int) -> ByteStreamer:
    if client_id not in streamers:
        streamers[client_id] = ByteStreamer(multi_clients[client_id])
    return streamers[client_id]


def parse_media_request(path: str, query: dict) -> tuple[int, str]:
    clean_path = unquote(path).strip('/')

    match = PATTERN_HASH_FIRST.match(clean_path)
    if match:
        try:
            message_id = int(match.group(2))
            secure_hash = match.group(1)
            if (len(secure_hash) == SECURE_HASH_LENGTH and
                    VALID_HASH_REGEX.match(secure_hash)):
                return message_id, secure_hash
        except ValueError as e:
            raise InvalidHash(f"Invalid message ID format in path: {e}") from e

    match = PATTERN_ID_FIRST.match(clean_path)
    if match:
        try:
            message_id = int(match.group(1))
            secure_hash = query.get("hash", "").strip()
            if (len(secure_hash) == SECURE_HASH_LENGTH and
                    VALID_HASH_REGEX.match(secure_hash)):
                return message_id, secure_hash
            else:
                raise InvalidHash("Invalid or missing hash in query parameter")
        except ValueError as e:
            raise InvalidHash(f"Invalid message ID format in path: {e}") from e

    raise InvalidHash("Invalid URL structure or missing hash")


def validate_public_hash(public_hash: str) -> str:
    secure_hash = public_hash.strip().lower()
    if len(secure_hash) != PUBLIC_HASH_LENGTH or not VALID_PUBLIC_HASH_REGEX.match(secure_hash):
        raise InvalidHash("Invalid canonical file hash")
    return secure_hash


def caller_key(request: web.Request) -> str:
    """Identity used for the per-caller stream cap.

    Behind a PaaS every request arrives from the router, so request.remote alone
    collapses all viewers into one bucket. Heroku (and most proxies) append the
    real connecting address to X-Forwarded-For; the last entry is the one the
    proxy added and therefore the only one a client cannot forge. Earlier
    entries are client-supplied - never key on those.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        candidate = forwarded.split(",")[-1].strip()
        if candidate:
            return candidate
    return request.remote or "unknown"


def _log_http_exception(scope: str, error: web.HTTPException) -> None:
    """Client-state errors are normal traffic; only 5xx deserve a warning."""
    if error.status >= 500:
        logger.warning(f"HTTP exception in {scope}: {error}")
    else:
        logger.debug(f"Client HTTP error in {scope}: {error}")


def _client_dc(client) -> Optional[int]:
    """The Telegram data centre this client is connected to, if known."""
    storage = getattr(client, "storage", None)
    return getattr(storage, "dc_id", None)


def dc_from_file_id(file_id) -> Optional[int]:
    """The data centre that holds a file, from its stored file_id."""
    if not file_id or not isinstance(file_id, str):
        return None
    try:
        from pyrogram.file_id import FileId
        return FileId.decode(file_id).dc_id or None
    except Exception:
        return None


def select_optimal_client(
    preferred_dc: Optional[int] = None,
    required_channel: Optional[int] = None
) -> tuple[int, ByteStreamer]:
    if not work_loads:
        raise web.HTTPInternalServerError(
            text=("No available clients to handle the request. "
                  "Please try again later."),
            headers=CORS_HEADERS,
        )

    available_clients = [
        (cid, load) for cid, load in work_loads.items()
        if load < MAX_CONCURRENT_PER_CLIENT]

    if not available_clients:
        loads = list(work_loads.values())
        load_range = f"~{min(loads)}–{max(loads)}" if min(loads) != max(loads) else f"~{min(loads)}"
        raise web.HTTPServiceUnavailable(
            text=(
                "All Telegram clients are currently at capacity "
                f"({load_range} active streams). Please retry shortly."
            ),
            headers={
                **CORS_HEADERS,
                "Retry-After": str(OVERLOAD_RETRY_AFTER_SECONDS),
            },
        )

    if required_channel is not None and client_channels:
        # Telegram only lets a bot read a channel it administers, so a file may
        # only be served by a client capable of reading that channel.
        capable = [
            (cid, load) for cid, load in available_clients
            if required_channel in client_channels.get(cid, ())
        ]
        if capable:
            available_clients = capable
        else:
            metrics.record_error("no_capable_client", f"channel {required_channel}")
            logger.warning(
                f"No pool client can read channel {required_channel}; "
                "serving with the least loaded client instead"
            )

    candidates = available_clients
    if preferred_dc:
        # A client sitting in the file's own data centre skips the cross-DC hop,
        # which is most of the time-to-first-byte on a fresh link.
        same_dc = [
            (cid, load) for cid, load in available_clients
            if _client_dc(multi_clients.get(cid)) == preferred_dc
        ]
        if same_dc:
            candidates = same_dc

    client_id = min(candidates, key=lambda x: x[1])[0]
    return client_id, get_streamer(client_id)


def get_content_disposition(request: web.Request) -> str:
    disposition = request.query.get("disposition", "attachment").strip().lower()
    return disposition if disposition in VALID_DISPOSITIONS else "attachment"


def parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    if not range_header:
        return 0, file_size - 1

    match = RANGE_REGEX.fullmatch(range_header)
    if not match:
        raise web.HTTPBadRequest(text=f"Invalid range header: {range_header}")

    start_str = match.group("start")
    end_str = match.group("end")
    if start_str:
        start = int(start_str)
        end = int(end_str) if end_str else file_size - 1
    else:
        if not end_str:
            raise web.HTTPBadRequest(text=f"Invalid range header: {range_header}")
        suffix_len = int(end_str)
        if suffix_len <= 0:
            raise web.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{file_size}"})
        start = max(file_size - suffix_len, 0)
        end = file_size - 1

    if start < 0 or end >= file_size or start > end:
        raise web.HTTPRequestRangeNotSatisfiable(
            headers={"Content-Range": f"bytes */{file_size}"}
        )

    return start, end


def _unavailable_error(reason: str, public_hash: str) -> web.HTTPException:
    """Map a record state to the HTTP error the caller should see."""
    metrics.record_error("gone", f"{reason}:{public_hash}")
    if reason == "revoked":
        return web.HTTPGone(text="This link has been revoked by its owner.")
    if reason == "expired":
        return web.HTTPGone(
            text="This link has expired. Send the file to the bot again for a new one."
        )
    return web.HTTPNotFound(text="Resource not found")


def _resolve_unique_id(file_info: dict) -> str:
    unique_id = file_info.get("unique_id") or file_info.get("file_unique_id")
    if not unique_id:
        raise FileNotFound("File unique ID not found in info.")
    return unique_id


def _resolve_filename(file_info: dict, mime_type: str) -> str:
    filename = file_info.get("file_name")
    if filename:
        return filename

    ext = mime_type.split('/')[-1] if '/' in mime_type else 'bin'
    ext_map = {'jpeg': 'jpg', 'mpeg': 'mp3', 'octet-stream': 'bin'}
    ext = ext_map.get(ext, ext)
    return f"file_{secrets.token_hex(4)}.{ext}"


async def _serve_media_response(
    request: web.Request,
    *,
    file_info: dict,
    streamer: ByteStreamer,
    client_id: int,
    media_ref: int | str,
    fallback_message_id: int | None = None,
    on_fallback_message=None,
    chat_id: Optional[int] = None
):
    file_size = int(file_info.get('file_size', 0) or 0)
    if file_size == 0:
        raise FileNotFound("File size is reported as zero or unavailable.")

    range_header = request.headers.get("Range", "")
    start, end = parse_range_header(range_header, file_size)
    content_length = end - start + 1

    if start == 0 and end == file_size - 1:
        range_header = ""

    mime_type = file_info.get('mime_type') or 'application/octet-stream'
    filename = _resolve_filename(file_info, mime_type)
    disposition = get_content_disposition(request)

    headers = {
        "Content-Type": mime_type,
        "Content-Length": str(content_length),
        "Content-Disposition": (
            f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}"),
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=31536000",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Range, Content-Type, *",
        "Access-Control-Expose-Headers": (
            "Content-Length, Content-Range, Content-Disposition"),
        "X-Content-Type-Options": "nosniff"
    }

    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    if request.method == 'HEAD':
        work_loads[client_id] -= 1
        return web.Response(
            status=206 if range_header else 200,
            headers=headers
        )

    # Initialize the generator and pre-fetch the first chunk BEFORE committing headers.
    # This guarantees that MTProto connection is established and data is ready,
    # preventing Heroku H15 Idle Connection (55s) timeouts before data arrives.
    metrics.record_stream_started()
    stream_gen = streamer.stream_file(
        media_ref,
        offset=start,
        limit=content_length,
        fallback_message_id=fallback_message_id,
        on_fallback_message=on_fallback_message,
        chat_id=chat_id
    )

    first_chunk = None
    try:
        first_chunk = await stream_gen.__anext__()
    except StopAsyncIteration:
        pass
    except Exception as e:
        # The route handler owns the single work_loads decrement for every error
        # path (see media_delivery / canonical_media_delivery). Decrementing here
        # as well double-counts the release and drives this client's load
        # negative, which then defeats the capacity gate in
        # select_optimal_client() and shows up as negative numbers in /status.
        logger.error(f"Failed to fetch initial chunk for {filename}: {e}", exc_info=True)
        metrics.record_error("stream", str(e), client=client_label(streamer.client))
        raise web.HTTPServiceUnavailable(
            text="Media stream temporarily unavailable. Please retry shortly.",
            headers={"Retry-After": "2", **CORS_HEADERS}
        )

    response = web.StreamResponse(
        status=206 if range_header else 200,
        headers=headers
    )
    await response.prepare(request)

    try:
        bytes_sent = 0
        bytes_to_skip = start % CHUNK_SIZE

        if first_chunk is not None:
            if bytes_to_skip > 0:
                if len(first_chunk) > bytes_to_skip:
                    first_chunk = first_chunk[bytes_to_skip:]
                    bytes_to_skip = 0
                else:
                    bytes_to_skip -= len(first_chunk)
                    first_chunk = b""

            if first_chunk:
                remaining = content_length - bytes_sent
                if len(first_chunk) > remaining:
                    first_chunk = first_chunk[:remaining]
                try:
                    await response.write(first_chunk)
                    bytes_sent += len(first_chunk)
                    metrics.record_bytes(len(first_chunk))
                except (
                    ConnectionResetError,
                    ConnectionError,
                    BrokenPipeError,
                    ClientConnectionResetError,
                    asyncio.CancelledError,
                ):
                    logger.debug(f"Client disconnected early while streaming {filename}")
                    metrics.record_client_disconnect()
                    return response

        if bytes_sent < content_length:
            async for chunk in stream_gen:
                if bytes_to_skip > 0:
                    if len(chunk) <= bytes_to_skip:
                        bytes_to_skip -= len(chunk)
                        continue
                    chunk = chunk[bytes_to_skip:]
                    bytes_to_skip = 0

                remaining = content_length - bytes_sent
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]

                if chunk:
                    try:
                        await response.write(chunk)
                        bytes_sent += len(chunk)
                        metrics.record_bytes(len(chunk))
                    except (
                        ConnectionResetError,
                        ConnectionError,
                        BrokenPipeError,
                        ClientConnectionResetError,
                        asyncio.CancelledError,
                    ):
                        logger.debug(f"Client disconnected early while streaming {filename}")
                        return response

                if bytes_sent >= content_length:
                    break
    except (
        ConnectionResetError,
        ConnectionError,
        BrokenPipeError,
        ClientConnectionResetError,
        asyncio.CancelledError,
    ):
        logger.debug(f"Client stream cancelled for {filename}")
        metrics.record_client_disconnect()
    except Exception as e:
        err_str = str(e).lower()
        if "closing transport" in err_str or "connection lost" in err_str or "connection reset" in err_str:
            logger.debug(f"Client transport closed while streaming {filename}")
        else:
            logger.error(f"Error streaming {filename}: {e}", exc_info=True)
            raise
    finally:
        work_loads[client_id] -= 1

    metrics.record_stream_completed()
    return response


@routes.get("/", allow_head=True)
async def root_redirect(request):
    raise web.HTTPFound("https://github.com/fyaz05/FileToLink")


@routes.get("/status", allow_head=True)
async def status_endpoint(request):
    uptime = time.time() - StartTime
    total_load = sum(work_loads.values())

    workload_distribution = {str(k): v for k, v in sorted(work_loads.items())}

    return web.json_response(
        {
            "server": {
                "status": "operational",
                "version": __version__,
                "uptime": get_readable_time(uptime)
            },
            "telegram_bot": {
                "username": f"@{getattr(StreamBot, 'username', None) or 'unknown'}",
                "active_clients": len(multi_clients)
            },
            "resources": {
                "total_workload": total_load,
                "workload_distribution": workload_distribution,
                "per_user_streams": {
                    "limit": user_streams.limit,
                    "active": user_streams.total(),
                },
            },
            "metrics": metrics.snapshot()
        },
        headers={"Access-Control-Allow-Origin": "*"}
    )


@routes.options("/status")
async def status_options(request: web.Request):
    return web.Response(headers={
        **CORS_HEADERS,
        "Access-Control-Max-Age": "86400"
    })


@routes.options(r"/{path:.+}")
async def media_options(request: web.Request):
    return web.Response(headers={
        **CORS_HEADERS,
        "Access-Control-Max-Age": "86400"
    })


@routes.get(r"/thumb/{secure_hash}", allow_head=True)
async def canonical_thumbnail(request: web.Request):
    """Serve the stored thumbnail, so links unfurl with a preview image."""
    try:
        secure_hash = validate_public_hash(request.match_info["secure_hash"])
        file_record = await get_file_by_hash(secure_hash, raise_on_error=False)
        if not file_record:
            raise FileNotFound("Canonical file not found")

        reason = record_unavailable_reason(file_record)
        if reason:
            raise _unavailable_error(reason, secure_hash)

        thumb_file_id = file_record.get("thumb_file_id")
        if not thumb_file_id:
            raise FileNotFound("No thumbnail stored for this file")

        # The primary bot is the one that read the canonical record, so its
        # reference is the one that works here.
        payload = await StreamBot.download_media(thumb_file_id, in_memory=True)
        if not payload:
            raise FileNotFound("Thumbnail could not be downloaded")

        body = payload.getvalue() if hasattr(payload, "getvalue") else bytes(payload)
        return web.Response(
            body=body,
            content_type="image/jpeg",
            headers={
                **CORS_HEADERS,
                "Cache-Control": "public, max-age=604800",
                "X-Content-Type-Options": "nosniff",
            }
        )
    except (InvalidHash, FileNotFound) as e:
        logger.debug(f"Thumbnail not available: {e}")
        raise web.HTTPNotFound(text="Resource not found") from e
    except web.HTTPException:
        raise
    except Exception as e:
        error_id = secrets.token_hex(6)
        logger.error(f"Thumbnail error {error_id}: {e}", exc_info=True)
        raise web.HTTPInternalServerError(
            text=f"Server error occurred: {error_id}") from e


@routes.get(r"/watch/f/{secure_hash}/{name:.+}", allow_head=True)
async def canonical_media_preview(request: web.Request):
    try:
        secure_hash = validate_public_hash(request.match_info["secure_hash"])
        file_record = await get_file_by_hash(secure_hash, raise_on_error=False)
        if not file_record:
            raise FileNotFound("Canonical file not found")

        reason = record_unavailable_reason(file_record)
        if reason:
            raise _unavailable_error(reason, secure_hash)

        file_name = file_record.get("file_name") or f"file_{secure_hash}"
        src = f"{Var.URL.rstrip('/')}/f/{secure_hash}/{quote_media_name(file_name)}"
        thumb_url = (
            f"{Var.URL.rstrip('/')}/thumb/{secure_hash}"
            if file_record.get("thumb_file_id") else None
        )
        rendered_page = await render_media_page(
            file_name, src, requested_action='stream', thumb_url=thumb_url)

        response = web.Response(
            text=rendered_page,
            content_type='text/html',
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Range, Content-Type, *",
                "X-Content-Type-Options": "nosniff",
            }
        )
        response.enable_compression()
        return response
    except (InvalidHash, FileNotFound) as e:
        logger.debug(f"Canonical preview error: {type(e).__name__} - {e}", exc_info=True)
        raise web.HTTPNotFound(text="Resource not found") from e
    except web.HTTPException:
        raise
    except Exception as e:
        error_id = secrets.token_hex(6)
        logger.error(f"Canonical preview error {error_id}: {e}", exc_info=True)
        raise web.HTTPInternalServerError(
            text=f"Server error occurred: {error_id}") from e


@routes.get(r"/watch/{path:.+}", allow_head=True)
async def media_preview(request: web.Request):
    try:
        path = request.match_info["path"]
        message_id, secure_hash = parse_media_request(path, request.query)

        rendered_page = await render_page(
            message_id, secure_hash, requested_action='stream')

        response = web.Response(
            text=rendered_page,
            content_type='text/html',
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Range, Content-Type, *",
                "X-Content-Type-Options": "nosniff",
            }
        )
        response.enable_compression()
        return response

    except (InvalidHash, FileNotFound) as e:
        logger.debug(
            f"Client error in preview: {type(e).__name__} - {e}",
            exc_info=True)
        raise web.HTTPNotFound(text="Resource not found") from e
    except web.HTTPException:
        raise
    except Exception as e:
        error_id = secrets.token_hex(6)
        logger.error(f"Preview error {error_id}: {e}", exc_info=True)
        raise web.HTTPInternalServerError(
            text=f"Server error occurred: {error_id}") from e


@routes.get(r"/f/{secure_hash}/{name:.+}", allow_head=True)
async def canonical_media_delivery(request: web.Request):
    try:
        secure_hash = validate_public_hash(request.match_info["secure_hash"])
        file_record = await get_file_by_hash(secure_hash, raise_on_error=False)
        if not file_record:
            raise FileNotFound("Canonical file not found")

        reason = record_unavailable_reason(file_record)
        if reason:
            raise _unavailable_error(reason, secure_hash)

        file_channel = record_bin_channel(file_record)
        preferred_dc = dc_from_file_id(file_record.get("file_id"))
        client_id, streamer = select_optimal_client(
            preferred_dc=preferred_dc, required_channel=file_channel
        )
        work_loads[client_id] += 1

        user_key = caller_key(request)
        if not user_streams.acquire(user_key):
            work_loads[client_id] -= 1
            metrics.record_error("rate_limit",
                                 f"per-caller stream cap reached for {user_key}")
            raise web.HTTPTooManyRequests(
                text=("You already have the maximum number of simultaneous streams. "
                      "Close one and retry."),
                headers={"Retry-After": str(OVERLOAD_RETRY_AFTER_SECONDS), **CORS_HEADERS},
            )

        try:
            try:
                _resolve_unique_id(file_record)
                media_ref = int(file_record["canonical_message_id"])
                fallback_message_id = int(file_record["canonical_message_id"])

                async def persist_refreshed_file_id(message):
                    if client_id != 0:
                        return
                    media = get_media(message)
                    new_file_id = getattr(media, "file_id", None) if media else None
                    if new_file_id and new_file_id != file_record.get("file_id"):
                        try:
                            await update_cached_file_id(file_record, new_file_id)
                        except Exception as e:
                            logger.warning(
                                f"Failed to refresh cached file_id for canonical file {secure_hash}: {e}",
                                exc_info=True
                            )

                return await _serve_media_response(
                    request,
                    file_info=file_record,
                    streamer=streamer,
                    client_id=client_id,
                    media_ref=media_ref,
                    fallback_message_id=fallback_message_id,
                    on_fallback_message=persist_refreshed_file_id,
                    chat_id=file_channel
                )
            except (FileNotFound, InvalidHash):
                work_loads[client_id] -= 1
                raise
            except web.HTTPException as e:
                work_loads[client_id] -= 1
                logger.debug(f"Client HTTP error in canonical stream: {e}")
                raise
            except Exception as e:
                work_loads[client_id] -= 1
                error_id = secrets.token_hex(6)
                logger.error(f"Canonical stream error {error_id}: {e}", exc_info=True)
                raise web.HTTPInternalServerError(
                    text=f"Server error during streaming: {error_id}") from e
        finally:
            user_streams.release(user_key)
    except (InvalidHash, FileNotFound) as e:
        logger.debug(f"Canonical client error: {type(e).__name__} - {e}", exc_info=True)
        raise web.HTTPNotFound(text="Resource not found") from e
    except web.HTTPException as e:
        _log_http_exception("canonical stream", e)
        raise
    except Exception as e:
        error_id = secrets.token_hex(6)
        logger.error(f"Canonical server error {error_id}: {e}", exc_info=True)
        raise web.HTTPInternalServerError(
            text=f"An unexpected server error occurred: {error_id}") from e


@routes.get(r"/{path:.+}", allow_head=True)
async def media_delivery(request: web.Request):
    try:
        path = request.match_info["path"]
        message_id, secure_hash = parse_media_request(path, request.query)

        client_id, streamer = select_optimal_client()

        work_loads[client_id] += 1

        user_key = caller_key(request)
        if not user_streams.acquire(user_key):
            work_loads[client_id] -= 1
            metrics.record_error("rate_limit", f"per-caller stream cap reached for {user_key}")
            raise web.HTTPTooManyRequests(
                text=("You already have the maximum number of simultaneous streams. "
                      "Close one and retry."),
                headers={"Retry-After": str(OVERLOAD_RETRY_AFTER_SECONDS), **CORS_HEADERS},
            )

        try:
            try:
                file_info = await streamer.get_file_info(message_id)
                unique_id = _resolve_unique_id(file_info)

                if unique_id[:SECURE_HASH_LENGTH] != secure_hash:
                    raise InvalidHash(
                        "Provided hash does not match file's unique ID.")
                return await _serve_media_response(
                    request,
                    file_info=file_info,
                    streamer=streamer,
                    client_id=client_id,
                    media_ref=message_id
                )

            except (FileNotFound, InvalidHash):
                work_loads[client_id] -= 1
                raise
            except web.HTTPException as e:
                work_loads[client_id] -= 1
                logger.debug(f"Client HTTP error in media stream: {e}")
                raise
            except Exception as e:
                work_loads[client_id] -= 1
                error_id = secrets.token_hex(6)
                logger.error(
                    f"Stream error {error_id}: {e}",
                    exc_info=True)
                raise web.HTTPInternalServerError(
                    text=f"Server error during streaming: {error_id}") from e
        finally:
            user_streams.release(user_key)

    except (InvalidHash, FileNotFound) as e:
        logger.debug(f"Client error: {type(e).__name__} - {e}", exc_info=True)
        raise web.HTTPNotFound(text="Resource not found") from e
    except web.HTTPException as e:
        _log_http_exception("media stream", e)
        raise
    except Exception as e:
        error_id = secrets.token_hex(6)
        logger.error(f"Server error {error_id}: {e}", exc_info=True)
        raise web.HTTPInternalServerError(
            text=f"An unexpected server error occurred: {error_id}") from e
