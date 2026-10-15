# Thunder/utils/custom_dl.py

import asyncio
import time
from collections import OrderedDict
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Optional, Tuple

from pyrogram import Client
from pyrogram.errors import (FileIdInvalid, FileReferenceEmpty,
                             FileReferenceExpired, FileReferenceInvalid,
                             FloodWait)
from pyrogram.types import Message

from Thunder.server.exceptions import FileNotFound
from Thunder.utils.file_properties import get_media
from Thunder.utils.logger import logger
from Thunder.utils.metrics import metrics
from Thunder.vars import Var

# A chunk that never arrives used to hang the response until the gateway killed
# it (Heroku router: H15 "Idle connection" after 55s, with the client left on a
# half-delivered stream). Fail the read instead so the retry path can resume.
CHUNK_READ_TIMEOUT = 20.0

_MSG_CACHE_TTL = 300  # 5 minutes
_MSG_CACHE_MAX_SIZE = 500
# Keyed by (id(client), message_id): a Telegram file reference is only valid for
# the session that fetched the message, so a Message obtained by one client must
# never be handed to another one. Sharing one cache across multi-clients is what
# produced FILE_REFERENCE_EXPIRED on the streaming client.
_GLOBAL_MSG_CACHE: OrderedDict[Tuple[int, int], Tuple[float, Message]] = OrderedDict()
_MSG_LOCK = asyncio.Lock()

_FILE_REFERENCE_ERRORS = (
    FileReferenceExpired,
    FileReferenceInvalid,
    FileReferenceEmpty,
    FileIdInvalid,
)


def _is_file_reference_error(exc: BaseException) -> bool:
    """True for Telegram errors that mean "this file reference is not usable"."""
    if isinstance(exc, _FILE_REFERENCE_ERRORS):
        return True
    text = str(exc).upper()
    return "FILE_REFERENCE" in text or "FILE_ID_INVALID" in text


def invalidate_cached_message(message_id: int) -> None:
    """Drop every cached Message for this id so the next read re-fetches it."""
    for key in [k for k in _GLOBAL_MSG_CACHE if k[2] == message_id]:
        _GLOBAL_MSG_CACHE.pop(key, None)


def primary_client() -> Optional[Client]:
    """The primary bot client, or None if it is not importable."""
    try:
        from Thunder.bot import StreamBot
        return StreamBot
    except Exception:
        return None


def client_label(client: Optional[Client]) -> str:
    """Readable client name for logs - the point is telling sessions apart."""
    if client is None:
        return "none"
    name = getattr(client, "name", None)
    return f"{type(client).__name__}:{name}" if name else type(client).__name__


class ByteStreamer:
    __slots__ = ('client', 'chat_id')

    def __init__(self, client: Client) -> None:
        self.client = client
        self.chat_id = int(Var.BIN_CHANNEL)

    async def get_message_with_owner(
        self,
        message_id: int,
        *,
        chat_id: Optional[int] = None,
        force_refresh: bool = False,
        preferred_client: Optional[Client] = None
    ) -> Tuple[Message, Client]:
        """Fetch a stored message and report which client fetched it.

        A Telegram file reference is only valid for the session that fetched the
        message, so the caller must stream the returned message *with the
        returned client*. The read is attempted with the client that asked for it
        first and falls back to the primary bot, because a secondary client may
        not be a member of that channel. The cache is keyed by
        ``(client, channel, message_id)`` and written under the client that
        performed the fetch, never under the client that merely asked.

        ``chat_id`` names the storage channel holding the file. Without it every
        configured channel is tried in order (primary first), which is how links
        created before multi-channel support still resolve.

        ``force_refresh`` skips the cache, which is what the retry path needs
        after a stale reference was rejected. ``preferred_client`` asks a
        specific session to do the read - the retry path rotates to the other
        client so a rejected reference is not simply reproduced.
        """
        now = time.time()
        channels = [int(chat_id)] if chat_id else [
            int(channel)
            for channel in (getattr(Var, "BIN_CHANNELS", None) or [self.chat_id])
        ]
        use_cache = not force_refresh and (
            preferred_client is None or preferred_client is self.client
        )

        if use_cache:
            async with _MSG_LOCK:
                # Look the message up in each candidate channel: a link that does
                # not name its channel can still be served from the cache.
                for channel in channels:
                    cache_key = (id(self.client), channel, message_id)
                    if cache_key not in _GLOBAL_MSG_CACHE:
                        continue
                    ts, message = _GLOBAL_MSG_CACHE[cache_key]
                    if now - ts < _MSG_CACHE_TTL:
                        _GLOBAL_MSG_CACHE.move_to_end(cache_key)
                        metrics.record_message_cache(hit=True)
                        # A hit on this key means this very client fetched it.
                        return message, self.client
                    _GLOBAL_MSG_CACHE.pop(cache_key, None)

        metrics.record_message_cache(hit=False)

        fetch_clients: list[Client] = [preferred_client or self.client]
        for candidate in (self.client, primary_client()):
            if candidate is not None and candidate not in fetch_clients:
                fetch_clients.append(candidate)

        last_error: Exception | None = None
        for channel in channels:
            for fetch_client in fetch_clients:
                for _attempt in range(3):
                    try:
                        message = await fetch_client.get_messages(channel, message_id)
                    except FloodWait as e:
                        logger.warning(f"FloodWait on get_messages: sleep {e.value}s")
                        await asyncio.sleep(e.value)
                        continue
                    except Exception as e:
                        last_error = e
                        logger.debug(
                            f"Error fetching message {message_id} from channel {channel} "
                            f"with {type(fetch_client).__name__}: {e}"
                        )
                        break

                    if message and message.media:
                        async with _MSG_LOCK:
                            _key = (id(fetch_client), channel, message_id)
                            _GLOBAL_MSG_CACHE[_key] = (now, message)
                            _GLOBAL_MSG_CACHE.move_to_end(_key)
                            while len(_GLOBAL_MSG_CACHE) > _MSG_CACHE_MAX_SIZE:
                                _GLOBAL_MSG_CACHE.popitem(last=False)
                        return message, fetch_client

                    last_error = FileNotFound(
                        f"Message {message_id} has no media in channel {channel}")
                    break

        raise FileNotFound(f"Message {message_id} not found") from last_error

    async def get_message(self, message_id: int, *, chat_id: Optional[int] = None,
                          force_refresh: bool = False) -> Message:
        message, _owner = await self.get_message_with_owner(
            message_id, chat_id=chat_id, force_refresh=force_refresh
        )
        return message

    async def _read_chunks(self, client, target, offset: int, limit: int):
        """Yield media chunks, but never wait forever for one.

        The timeout is what turns a stalled MTProto read into a retryable error
        (``asyncio.TimeoutError`` is handled by the retry branch) instead of a
        response that sits silent until the router drops it.
        """
        iterator = client.stream_media(target, offset=offset, limit=limit).__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    iterator.__anext__(), timeout=CHUNK_READ_TIMEOUT
                )
            except StopAsyncIteration:
                return
            yield chunk

    async def stream_file(
        self,
        media_ref: int | Message,
        offset: int = 0,
        limit: int = 0,
        fallback_message_id: int | None = None,
        on_fallback_message: Optional[Callable[[Message], Awaitable[None]]] = None,
        chat_id: Optional[int] = None
    ) -> AsyncGenerator[bytes, None]:
        chunk_size = 1024 * 1024
        chunk_offset = offset // chunk_size
        chunk_limit = 0
        if limit > 0:
            first_chunk = offset // chunk_size
            last_chunk = (offset + limit - 1) // chunk_size
            chunk_limit = (last_chunk - first_chunk) + 1

        refs: list[int | Message] = [media_ref]
        media_id = media_ref if isinstance(media_ref, int) else None
        if isinstance(media_ref, Message):
            media_id = getattr(media_ref, "id", getattr(media_ref, "message_id", None))
        if fallback_message_id is not None and (media_id is None or fallback_message_id != media_id):
            refs.append(fallback_message_id)

        current_offset = chunk_offset
        remaining_chunks = chunk_limit
        max_retries = 5
        last_error: Exception | None = None

        for ref in refs:
            target = None
            # Whoever fetched the reference, streams it. They differ only when
            # this client could not read BIN_CHANNEL itself.
            stream_client = self.client
            force_refresh = False
            preferred_client: Optional[Client] = None
            retry_count = 0
            while retry_count < max_retries:
                try:
                    if target is None:
                        if isinstance(ref, int):
                            target, stream_client = await self.get_message_with_owner(
                                ref,
                                chat_id=chat_id,
                                force_refresh=force_refresh,
                                preferred_client=preferred_client
                            )
                        else:
                            target, stream_client = ref, self.client
                        if (
                            on_fallback_message is not None and
                            fallback_message_id is not None and
                            ref == fallback_message_id and
                            isinstance(target, Message)
                        ):
                            await on_fallback_message(target)

                    async for chunk in self._read_chunks(
                        stream_client, target, current_offset, remaining_chunks
                    ):
                        yield chunk
                        current_offset += 1
                        if remaining_chunks > 0:
                            remaining_chunks -= 1
                            if remaining_chunks <= 0:
                                return
                    return
                except FloodWait as e:
                    logger.warning(f"FloodWait in stream_file: sleeping {e.value}s")
                    await asyncio.sleep(e.value)
                    retry_count += 1
                except (ConnectionError, ConnectionResetError, OSError, asyncio.TimeoutError) as e:
                    retry_count += 1
                    last_error = e
                    logger.warning(
                        f"Telegram connection reset/timeout in stream_file at chunk {current_offset} "
                        f"(attempt {retry_count}/{max_retries}): {e}"
                    )
                    await asyncio.sleep(0.5 * retry_count)
                except Exception as e:
                    if _is_file_reference_error(e):
                        # Telegram hands out a file reference that only the
                        # session which fetched the message can use, and it can
                        # expire. Drop the cached Message, rotate to the other
                        # session so a rejected reference is not reproduced, and
                        # retry the same chunk.
                        retry_count += 1
                        last_error = e
                        metrics.record_error(
                            "reference", str(e), client=client_label(stream_client)
                        )
                        if media_id is not None:
                            invalidate_cached_message(media_id)

                        others = [
                            candidate for candidate in (self.client, primary_client())
                            if candidate is not None and candidate is not stream_client
                        ]
                        preferred_client = others[0] if others else None
                        force_refresh = True
                        target = None

                        logger.warning(
                            f"Stale file reference for message {media_id} at chunk "
                            f"{current_offset} (attempt {retry_count}/{max_retries}): "
                            f"reference came from {client_label(stream_client)}, "
                            f"retrying with {client_label(preferred_client or stream_client)}"
                        )
                        await asyncio.sleep(0.25 * retry_count)
                        continue

                    last_error = e
                    logger.debug(f"Error streaming media ref {ref}: {e}", exc_info=True)
                    break

        raise FileNotFound(f"Unable to stream file: {last_error}")

    def get_file_info_sync(self, message: Message) -> Dict[str, Any]:
        media = get_media(message)
        if not media:
            return {"message_id": message.id, "error": "No media"}

        media_type = type(media).__name__.lower()
        file_name = getattr(media, 'file_name', None)
        mime_type = getattr(media, 'mime_type', None)

        if not file_name:
            ext_map = {
                "photo": "jpg",
                "audio": "mp3",
                "voice": "ogg",
                "video": "mp4",
                "animation": "mp4",
                "videonote": "mp4",
                "sticker": "webp",
            }
            ext = ext_map.get(media_type, "bin")
            file_name = f"Thunder_{message.id}.{ext}"

        if not mime_type:
            mime_map = {
                "photo": "image/jpeg",
                "voice": "audio/ogg",
                "videonote": "video/mp4",
            }
            mime_type = mime_map.get(media_type)

        return {
            "message_id": message.id,
            "file_size": getattr(media, 'file_size', 0) or 0,
            "file_name": file_name,
            "mime_type": mime_type,
            "unique_id": getattr(media, 'file_unique_id', None),
            "media_type": media_type
        }

    async def get_file_info(self, message_id: int) -> Dict[str, Any]:
        try:
            message = await self.get_message(message_id)
            return self.get_file_info_sync(message)
        except Exception as e:
            logger.debug(f"Error getting file info for {message_id}: {e}", exc_info=True)
            return {"message_id": message_id, "error": str(e)}
