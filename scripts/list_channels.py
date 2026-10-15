#!/usr/bin/env python3
# scripts/list_channels.py
"""List the channels the primary bot can see, with their ids.

Use it to find the id for BIN_CHANNEL_2: create the channel, add the primary bot
(and the pool bots) as administrators, then run this where the environment lives:

    python scripts/list_channels.py          # via the bot's /shell command
    heroku run python scripts/list_channels.py -a <app>

Copy the ``-100...`` id of the new channel into BIN_CHANNEL_2.
"""

import asyncio
import os
import sys
from typing import Iterable, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv("config.env")


def configured_channel_ids(environ=None) -> List[int]:
    """BIN_CHANNEL and BIN_CHANNEL_2..N, in configuration order."""
    environ = os.environ if environ is None else environ
    ids: List[int] = []
    raw = (environ.get("BIN_CHANNEL") or "").strip().strip('"').strip("'")
    if raw:
        ids.append(int(raw))
    for index in range(2, 50):
        raw = (environ.get(f"BIN_CHANNEL_{index}") or "").strip().strip('"').strip("'")
        if raw:
            try:
                ids.append(int(raw))
            except ValueError:
                continue
    return ids


def format_channel_row(chat_id: int, title: str, chat_type: str, configured: bool) -> str:
    """One aligned row per channel, flagged when it is already configured."""
    mark = "configured" if configured else "NOT configured"
    return f"{chat_id:>16}  {title[:44]:<44}  {chat_type:<10}  {mark}"


async def collect_channels(client) -> List[Tuple[int, str, str]]:
    """Every channel the client can see, as (id, title, type)."""
    found: List[Tuple[int, str, str]] = []
    seen = set()

    async for dialog in client.get_dialogs():
        chat = getattr(dialog, "chat", None)
        if chat is None:
            continue
        chat_id = getattr(chat, "id", None)
        if chat_id is None or chat_id in seen:
            continue
        chat_type = str(getattr(chat, "type", "")).split(".")[-1].lower()
        if "channel" not in chat_type and "supergroup" not in chat_type:
            continue
        seen.add(chat_id)
        found.append((int(chat_id), getattr(chat, "title", "") or "", chat_type))

    return sorted(found)


def summarise(channels: Iterable[Tuple[int, str, str]], configured: Iterable[int]) -> str:
    configured = list(configured)
    rows = [format_channel_row(chat_id, title, kind, chat_id in configured)
            for chat_id, title, kind in channels]
    if not rows:
        return ("No channels visible to the primary bot. Add it to the channel "
                "as an administrator first, then run this again.")
    header = f"{'id':>16}  {'title':<44}  {'type':<10}  status"
    return header + "\n" + "-" * len(header) + "\n" + "\n".join(rows)


async def main() -> int:
    from pyrogram import Client

    api_id = int(os.getenv("API_ID", "0") or 0)
    api_hash = (os.getenv("API_HASH") or "").strip().strip('"').strip("'")
    bot_token = (os.getenv("BOT_TOKEN") or "").strip().strip('"').strip("'")

    if not api_id or not api_hash or not bot_token:
        print("API_ID, API_HASH and BOT_TOKEN must be set (config.env or environment).")
        return 2

    client = Client(
        name="channellister",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        in_memory=True,
        no_updates=True,
    )
    try:
        await client.start()
        channels = await collect_channels(client)
    finally:
        try:
            await client.stop()
        except Exception:
            pass

    print(f"BIN_CHANNEL: {os.getenv('BIN_CHANNEL', '(unset)')}")
    print()
    print(summarise(channels, configured_channel_ids()))
    print()
    print("Put the id of the new channel into BIN_CHANNEL_2 (then restart).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
