# Thunder/bot/clients.py

import asyncio

from pyrogram import Client
from pyrogram.errors import FloodWait

from Thunder.bot import StreamBot, multi_clients, work_loads
from Thunder.utils.config_parser import TokenParser
from Thunder.utils.logger import logger
from Thunder.vars import Var

async def _can_read_bin_channel(client, label: str) -> bool:
    """True when the client can actually read a message from BIN_CHANNEL.

    A client that cannot has no file reference of its own: its requests would
    either fail with FILE_REFERENCE_EXPIRED or silently fall back to the primary
    bot's reference, which Telegram rejects for the same reason. Better to leave
    it out of the pool than to keep a client that only produces errors.
    """
    chat = None
    for _attempt in range(2):
        try:
            chat = await client.get_chat(int(Var.BIN_CHANNEL))
            break
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.warning(
                f"   ✖ {label} cannot read BIN_CHANNEL ({e}); "
                "leaving it out of the streaming pool"
            )
            return False

    if chat is None:
        logger.warning(f"   ✖ {label} could not read BIN_CHANNEL; leaving it out")
        return False

    print(f"   ✓ {label} can read BIN_CHANNEL")
    return True


async def cleanup_clients():
    for client in multi_clients.values():
        try:
            try:
                await client.stop()
            except FloodWait as e:
                await asyncio.sleep(e.value)
                await client.stop()
        except Exception as e:
            logger.error(f"Error stopping client: {e}", exc_info=True)

async def initialize_clients():
    print("╠══════════════════ INITIALIZING CLIENTS ═══════════════════╣")
    multi_clients[0] = StreamBot
    work_loads[0] = 0
    if await _can_read_bin_channel(StreamBot, "Primary client"):
        print("   ✓ Primary client initialized")
    else:
        logger.error(
            "   ✖ Primary client cannot read BIN_CHANNEL - add it to the channel "
            "(as admin) or no link will ever stream."
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

    async def start_client(client_id, token):
        try:
            if client_id == len(all_tokens):
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

            if not await _can_read_bin_channel(client, f"Client ID {client_id}"):
                try:
                    await client.stop()
                except Exception as stop_error:
                    logger.debug(f"Error stopping unusable client {client_id}: {stop_error}")
                return None

            work_loads[client_id] = 0
            print(f"   ◎ Client ID {client_id} started")
            return client_id, client
        except Exception as e:
            logger.error(f"   ✖ Failed to start Client ID {client_id}. Error: {e}", exc_info=True)
            return None

    clients = await asyncio.gather(*[start_client(i, token) for i, token in all_tokens.items() if token])
    clients = [client for client in clients if client]

    multi_clients.update(dict(clients))
    
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
