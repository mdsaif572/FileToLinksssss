#!/usr/bin/env python3
# scripts/check_pool.py
"""Report which MULTI_TOKEN bots are configured, who they are, and who can serve.

Answers the operational question "which token is the one being skipped?" without
reading secrets out of the host: token values are masked, and each one is
identified by the @username Telegram returns for it.

Run it where the environment lives, e.g. through the bot's /shell command or as
a one-off dyno:

    python scripts/check_pool.py                 # every MULTI_TOKEN* slot
    python scripts/check_pool.py --only 28,33,34  # just these slots
    python scripts/check_pool.py --limit 5        # first five configured slots

Exit code is 1 when a configured token cannot read BIN_CHANNEL, 0 otherwise.
"""

import argparse
import asyncio
import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv("config.env")

MAX_TOKEN_SLOTS = 50


def mask_token(token: str) -> str:
    """Never print a usable credential: keep just enough to identify the slot."""
    token = (token or "").strip()
    if not token:
        return "(empty)"
    head = token.split(":", 1)[0] if ":" in token else token[:8]
    return f"{head}:{'*' * 6}"


def configured_slots(environ: Optional[Dict[str, str]] = None) -> Dict[int, str]:
    """MULTI_TOKEN<number> -> token, for every slot that is actually set."""
    environ = os.environ if environ is None else environ
    slots: Dict[int, str] = {}
    for key, value in environ.items():
        if not key.startswith("MULTI_TOKEN"):
            continue
        digits = "".join(filter(str.isdigit, key))
        if not digits:
            continue
        token = (value or "").strip().strip('"').strip("'")
        if token:
            slots[int(digits)] = token
    return dict(sorted(slots.items()))


def missing_slots(slots: Dict[int, str], upto: int = MAX_TOKEN_SLOTS) -> List[int]:
    """Slots inside 1..upto that are configured nowhere (not an error, just news)."""
    if not slots:
        return []
    return [index for index in range(1, max(slots) + 1) if index not in slots]


async def inspect(slot: int, token: str, api_id: int, api_hash: str,
                  bin_channel: int) -> Tuple[int, str, str]:
    """Return (slot, username or "-", verdict) for one token."""
    from pyrogram import Client

    client = Client(
        name=f"poolcheck{slot}",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=token,
        in_memory=True,
        no_updates=True,
    )
    try:
        await client.start()
    except Exception as e:
        return slot, "-", f"token rejected ({type(e).__name__}: {e})".strip()

    try:
        username = getattr(getattr(client, "me", None), "username", None) or "-"
        try:
            chat = await client.get_chat(bin_channel)
            title = getattr(chat, "title", None) or str(bin_channel)
            try:
                member = await client.get_chat_member(bin_channel, client.me.id)
                role = str(getattr(member, "status", "member")).split(".")[-1]
            except Exception:
                role = "unknown"
            return slot, username, f"OK - can read '{title}' ({role})"
        except Exception as e:
            return slot, username, f"CANNOT READ BIN_CHANNEL ({type(e).__name__}: {e})"
    finally:
        try:
            await client.stop()
        except Exception:
            pass


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="comma-separated MULTI_TOKEN numbers to check")
    parser.add_argument("--limit", type=int, help="check only the first N configured slots")
    args = parser.parse_args()

    api_id = int(os.getenv("API_ID", "0") or 0)
    api_hash = os.getenv("API_HASH", "").strip().strip('"').strip("'")
    bin_channel = int(os.getenv("BIN_CHANNEL", "0") or 0)

    if not api_id or not api_hash or not bin_channel:
        print("API_ID, API_HASH and BIN_CHANNEL must be set (config.env or environment).")
        return 2

    slots = configured_slots()
    if args.only:
        wanted = {int(item) for item in args.only.replace(" ", "").split(",") if item}
        slots = {index: token for index, token in slots.items() if index in wanted}
    if args.limit:
        slots = dict(list(slots.items())[:args.limit])

    print(f"BIN_CHANNEL: {bin_channel}")
    if not slots:
        print("No MULTI_TOKEN* slots are configured - the primary bot serves everything.")
        return 0

    print(f"Configured slots: {sorted(slots)}\n")
    print(f"{'slot':>5}  {'token':<12}  {'bot':<24}  verdict")
    print("-" * 92)

    failures = 0
    for index, token in slots.items():
        slot, username, verdict = await inspect(index, token, api_id, api_hash, bin_channel)
        if not verdict.startswith("OK"):
            failures += 1
        print(f"{slot:>5}  {mask_token(token):<12}  {('@' + username):<24}  {verdict}")

    empty = missing_slots(configured_slots())
    if empty:
        print(f"\nNot configured (fine, just unused): MULTI_TOKEN{', MULTI_TOKEN'.join(map(str, empty))}")

    if failures:
        print(
            f"\n{failures} token(s) cannot serve. Add those bots to BIN_CHANNEL as "
            "admin/member, or remove the token from the config."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
