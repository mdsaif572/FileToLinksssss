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
from Thunder.vars import Var

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
    for key in [k for k in _GLOBAL_MSG_CACHE if k[1] == message_id]:
        _GLOBAL_MSG_CACHE.pop(key, None)


class ByteStreamer:
    __slots__ = ('client', 'chat_id')

    def __init__(self, client: Client) -> None:
        self.client = client
        self.chat_id = int(Var.BIN_CHANNEL)

    async def get_message(self, message_id: int, *, force_refresh: bool = False) -> Message:
        """Fetch a BIN_CHANNEL message, with a short per-client cache.

        The message is always read with the client that is going to stream it,
        falling back to the primary bot only if that read fails, because the
        file reference Telegram returns is bound to the session that fetched it.
        ``force_refresh`` additionally skips the cache, which is what the retry
        path needs after a stale reference was rejected.
        """
        now = time.time()
        cache_key = (id(self.client), message_id)

        if not force_refresh:
            async with _MSG_LOCK:
                if cache_key in _GLOBAL_MSG_CACHE:
                    ts, message = _GLOBAL_MSG_CACHE[cache_key]
                    if now - ts < _MSG_CACHE_TTL:
                        _GLOBAL_MSG_CACHE.move_to_end(cache_key)
                        return message
                    _GLOBAL_MSG_CACHE.pop(cache_key, None)

        fetch_clients = [self.client]
        try:
            from Thunder.bot import StreamBot
            if StreamBot and StreamBot is not self.client:
                fetch_clients.append(StreamBot)
        except Exception:
            pass

        last_error: Exception | None = None
        for fetch_client in fetch_clients:
            for _attempt in range(3):
                try:
                    message = await fetch_client.get_messages(self.chat_id, message_id)
                except FloodWait as e:
                    logger.warning(f"FloodWait on get_messages: sleep {e.value}s")
                    await asyncio.sleep(e.value)
                    continue
                except Exception as e:
                    last_error = e
                    logger.debug(
                        f"Error fetching message {message_id} with "
                        f"{type(fetch_client).__name__}: {e}"
                    )
                    break

                if message and message.media:
                    async with _MSG_LOCK:
                        # Cache under the client that fetched it, never under the
                        # client that merely asked for it.
                        _key = (id(fetch_client), message_id)
                        _GLOBAL_MSG_CACHE[_key] = (now, message)
                        _GLOBAL_MSG_CACHE.move_to_end(_key)
                        while len(_GLOBAL_MSG_CACHE) > _MSG_CACHE_MAX_SIZE:
                            _GLOBAL_MSG_CACHE.popitem(last=False)
                    return message

                last_error = FileNotFound(f"Message {message_id} has no media")
                break

        raise FileNotFound(f"Message {message_id} not found") from last_error

    async def stream_file(
        self,
        media_ref: int | Message,
        offset: int = 0,
        limit: int = 0,
        fallback_message_id: int | None = None,
        on_fallback_message: Optional[Callable[[Message], Awaitable[None]]] = None
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
            force_refresh = False
            retry_count = 0
            while retry_count < max_retries:
                try:
                    if target is None:
                        target = (
                            await self.get_message(ref, force_refresh=force_refresh)
                            if isinstance(ref, int) else ref
                        )
                        if (
                            on_fallback_message is not None and
                            fallback_message_id is not None and
                            ref == fallback_message_id and
                            isinstance(target, Message)
                        ):
                            await on_fallback_message(target)

                    async for chunk in self.client.stream_media(
                        target, offset=current_offset, limit=remaining_chunks
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
                        # expire. Drop the cached Message and re-read it with the
                        # streaming client, then retry the same chunk.
                        retry_count += 1
                        last_error = e
                        logger.warning(
                            f"Stale file reference for message {media_id} at chunk "
                            f"{current_offset} (attempt {retry_count}/{max_retries}): "
                            f"refreshing the message and retrying"
                        )
                        if media_id is not None:
                            invalidate_cached_message(media_id)
                        force_refresh = True
                        target = None
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
