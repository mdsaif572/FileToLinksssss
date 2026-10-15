# Thunder/bot/clients.py

import asyncio
import os
import time

from pyrogram import Client
from pyrogram.errors import FloodWait

from Thunder.bot import StreamBot, client_channels, multi_clients, work_loads
from Thunder.utils.config_parser import TokenParser
from Thunder.utils.logger import logger
from Thunder.vars import Var

def _channel_label(channel_id: int) -> str:
    """Human name for a storage channel: BIN_CHANNEL, BIN_CHANNEL_2, ..."""
    channels = list(getattr(Var, "BIN_CHANNELS", None) or [Var.BIN_CHANNEL])
    try:
        index = channels.index(int(channel_id))
    except ValueError:
        return f"channel {channel_id}"
    return "BIN_CHANNEL" if index == 0 else f"BIN_CHANNEL_{index + 1}"


async def _readable_channels(client, label: str) -> frozenset:
    """Which storage channels this client can read.

    A client can only read a channel it administers (Telegram has no other way to
    give a bot access to a channel), so a single channel it cannot read means it
    can never serve files stored there. A client that cannot read *any* channel
    has no file reference of its own - its requests would fail with
    FILE_REFERENCE_EXPIRED or silently borrow the primary bot's - and is left out
    of the pool entirely.
    """
    readable = set()
    for channel in (getattr(Var, "BIN_CHANNELS", None) or [Var.BIN_CHANNEL]):
        channel = int(channel)
        chat = None
        for _attempt in range(2):
            try:
                chat = await client.get_chat(channel)
                break
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.warning(
                    f"   ✖ {label} cannot read {_channel_label(channel)} ({e})"
                )
                break

        if chat is not None:
            readable.add(channel)
            print(f"   ✓ {label} can read {_channel_label(channel)}")
            logger.info(f"   ✓ {label} can read {_channel_label(channel)}")

    return frozenset(readable)


SHUTDOWN_TIMEOUT_SECONDS = 20.0

# How many pool clients may start (and be verified) at the same time. Starting a
# hundred hundred MTProto sessions in one burst invites throttling, and the
# platform gives the process a limited boot budget (Heroku: 60s, R10 after).
POOL_START_CONCURRENCY = max(int(os.getenv("POOL_START_CONCURRENCY", "10")), 1)


async def _stop_client(client) -> None:
    try:
        try:
            await client.stop()
        except FloodWait as e:
            await asyncio.sleep(e.value)
            await client.stop()
    except Exception as e:
        logger.error(f"Error stopping client: {e}", exc_info=True)


async def cleanup_clients():
    """Stop every client, in parallel and within the platform's grace period.

    Heroku sends SIGTERM and SIGKILLs whatever is still alive 30 seconds later
    (R12 "Exit timeout", exit status 137), so stopping a large pool one client at
    a time could never finish in time.
    """
    clients = list(multi_clients.values())
    if not clients:
        return

    try:
        await asyncio.wait_for(
            asyncio.gather(*(_stop_client(client) for client in clients),
                           return_exceptions=True),
            timeout=SHUTDOWN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"Stopping {len(clients)} clients took longer than "
            f"{SHUTDOWN_TIMEOUT_SECONDS:.0f}s; continuing shutdown"
        )

def _log_pool_memory(usable_clients: int) -> None:
    """Report resident memory once the pool is up (best effort, never fatal)."""
    try:
        import psutil

        rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        per_client = rss_mb / usable_clients if usable_clients else 0.0
        logger.info(
            f"   process memory: {rss_mb:.0f} MB with {usable_clients} client(s) "
            f"(~{per_client:.1f} MB each)"
        )
    except Exception as e:
        logger.debug(f"Could not read memory usage: {e}")


async def initialize_clients():
    print("╠══════════════════ INITIALIZING CLIENTS ═══════════════════╣")
    multi_clients[0] = StreamBot
    work_loads[0] = 0
    primary_readable = await _readable_channels(StreamBot, "Primary client")
    client_channels[0] = primary_readable
    if primary_readable:
        print("   ✓ Primary client initialized")
    else:
        logger.error(
            "   ✖ Primary client cannot read any storage channel - add it to "
            "BIN_CHANNEL (as admin) or no link will ever stream."
        )
    try:
        all_tokens = TokenParser().parse_from_env()
        if not all_tokens:
            print("   ◎ No additional clients found.")
            return
    except Exception as e:
        logger.error(f"   ✖ Error parsing additional tokens: {e}", exc_info=True)
        print("   ▶ Primary client will be used.")
        return

    skipped_clients = []

    async def start_client(client_id, token):
        try:
            if client_id == max(all_tokens):
                await asyncio.sleep(2)
            client = Client(
                api_hash=Var.API_HASH,
                api_id=Var.API_ID,
                bot_token=token,
                in_memory=True,
                name=str(client_id),
                no_updates=True,
                max_concurrent_transmissions=1000,
                sleep_threshold=Var.SLEEP_THRESHOLD
            )
            try:
                await client.start()
            except FloodWait as e:
                await asyncio.sleep(e.value)
                await client.start()

            username = getattr(getattr(client, "me", None), "username", None)
            label = f"Client ID {client_id}" + (f" (@{username})" if username else "")
            readable = await _readable_channels(client, label)
            if not readable:
                logger.warning(
                    f"   ✖ {label} cannot read any storage channel; "
                    "leaving it out of the streaming pool"
                )
                skipped_clients.append(client_id)
                try:
                    await client.stop()
                except Exception as stop_error:
                    logger.debug(f"Error stopping unusable client {client_id}: {stop_error}")
                return None

            client_channels[client_id] = readable
            work_loads[client_id] = 0
            print(f"   ◎ {label} started")
            return client_id, client
        except Exception as e:
            logger.error(f"   ✖ Failed to start Client ID {client_id}. Error: {e}", exc_info=True)
            return None

    pool_started_at = time.monotonic()
    start_slots = asyncio.Semaphore(POOL_START_CONCURRENCY)

    async def start_bounded(client_id, token):
        async with start_slots:
            return await start_client(client_id, token)

    clients = await asyncio.gather(
        *[start_bounded(i, token) for i, token in all_tokens.items() if token]
    )
    clients = [client for client in clients if client]

    multi_clients.update(dict(clients))

    # Sizing a large pool needs facts, not guesses: the platform gives the
    # process a limited boot budget and one resident process a memory ceiling.
    elapsed = time.monotonic() - pool_started_at
    usable = len(multi_clients)
    rate = usable / elapsed if elapsed > 0 else 0.0
    logger.info(
        f"Pool started {usable} client(s) in {elapsed:.1f}s "
        f"(concurrency {POOL_START_CONCURRENCY}, {rate:.1f} clients/s)"
    )
    _log_pool_memory(usable)
    if usable and rate > 0:
        for target in (100, 200, 400):
            if target > usable:
                logger.info(
                    f"   {target} clients at this rate: ~{target / rate:.0f}s of boot "
                    "time (raise POOL_START_CONCURRENCY if the host allows it)"
                )

    per_channel = {
        channel: sorted(
            client_id for client_id, channels in client_channels.items()
            if channel in channels
        )
        for channel in (getattr(Var, "BIN_CHANNELS", None) or [Var.BIN_CHANNEL])
    }
    logger.info(
        f"Streaming pool ready: clients {sorted(multi_clients)} "
        f"({len(multi_clients)} usable, {len(skipped_clients)} skipped"
        + (f": {sorted(skipped_clients)} - see MULTI_TOKEN<id> in the config"
           if skipped_clients else "")
        + ")"
    )
    for channel, client_ids in per_channel.items():
        logger.info(
            f"   {_channel_label(channel)} ({channel}): {len(client_ids)} client(s)"
        )
    
    if len(multi_clients) > 1:
        Var.MULTI_CLIENT = True
        print("╠══════════════════════ MULTI-CLIENT ═══════════════════════╣")
        print(f"   ◎ Total Clients: {len(multi_clients)} (Including primary client)")
        
        print("   ▶ Initial workload distribution:")
        for client_id, load in work_loads.items():
            print(f"   • Client {client_id}: {load} tasks")
            
    else:
        print("╠═══════════════════════════════════════════════════════════╣")
        print("   ▶ No additional clients were initialized")
        print("   ▶ Primary client will handle all requests")
