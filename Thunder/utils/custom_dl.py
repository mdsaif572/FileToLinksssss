# Thunder/utils/custom_dl.py

import asyncio
import time
from collections import OrderedDict
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Optional, Tuple

from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from Thunder.server.exceptions import FileNotFound
from Thunder.utils.file_properties import get_media
from Thunder.utils.logger import logger
from Thunder.vars import Var

_MSG_CACHE_TTL = 300  # 5 minutes
_MSG_CACHE_MAX_SIZE = 500
_GLOBAL_MSG_CACHE: OrderedDict[int, Tuple[float, Message]] = OrderedDict()
_MSG_LOCK = asyncio.Lock()


class ByteStreamer:
    __slots__ = ('client', 'chat_id')

    def __init__(self, client: Client) -> None:
        self.client = client
        self.chat_id = int(Var.BIN_CHANNEL)

    async def get_message(self, message_id: int) -> Message:
        now = time.time()
        async with _MSG_LOCK:
            if message_id in _GLOBAL_MSG_CACHE:
                ts, message = _GLOBAL_MSG_CACHE[message_id]
                if now - ts < _MSG_CACHE_TTL:
                    _GLOBAL_MSG_CACHE.move_to_end(message_id)
                    return message
                _GLOBAL_MSG_CACHE.pop(message_id, None)

        fetch_client = self.client
        try:
            from Thunder.bot import StreamBot
            if StreamBot and fetch_client is not StreamBot:
                fetch_client = StreamBot
        except Exception:
            pass

        message = None
        for attempt in range(5):
            try:
                message = await fetch_client.get_messages(self.chat_id, message_id)
                break
            except FloodWait as e:
                logger.warning(f"FloodWait on get_messages: sleep {e.value}s")
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.debug(f"Error fetching message {message_id} with primary: {e}")
                if fetch_client is not self.client:
                    try:
                        message = await self.client.get_messages(self.chat_id, message_id)
                        break
                    except Exception:
                        pass
                if attempt == 4:
                    raise FileNotFound(f"Message {message_id} not found") from e
                await asyncio.sleep(0.5)

        if not message or not message.media:
            raise FileNotFound(f"Message {message_id} not found")

        async with _MSG_LOCK:
            _GLOBAL_MSG_CACHE[message_id] = (now, message)
            _GLOBAL_MSG_CACHE.move_to_end(message_id)
            while len(_GLOBAL_MSG_CACHE) > _MSG_CACHE_MAX_SIZE:
                _GLOBAL_MSG_CACHE.popitem(last=False)

        return message

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
            retry_count = 0
            while retry_count < max_retries:
                try:
                    if target is None:
                        target = await self.get_message(ref) if isinstance(ref, int) else ref
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
