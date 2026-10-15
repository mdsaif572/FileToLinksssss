# Thunder/__main__.py

import asyncio
import glob
import importlib.util
import os
import subprocess
import sys
import time
from datetime import datetime

from pathlib import Path

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    from uvloop import install
    install()
except ImportError:
    pass
from aiohttp import web
from pyrogram import idle
from pyrogram.errors import FloodWait, MessageNotModified

from Thunder import __version__
from Thunder.bot import StreamBot
from Thunder.bot.clients import cleanup_clients, initialize_clients
from Thunder.server import web_server
from Thunder.utils.commands import set_commands
from Thunder.utils.database import db
from Thunder.utils.keepalive import ping_server
from Thunder.utils.canonical_files import drain_background_touch_tasks
from Thunder.utils.logger import logger
from Thunder.utils.messages import MSG_ADMIN_RESTART_DONE
from Thunder.utils.metrics import FailureAlerter, metrics
from Thunder.utils.rate_limiter import rate_limiter, request_executor

from Thunder.vars import Var


PLUGIN_PATH = "Thunder/bot/plugins/*.py"
VERSION = __version__


def print_banner():
    banner = f"""
╔═══════════════════════════════════════════════════════════════════╗
║                                                                   ║
║   ████████╗██╗  ██╗██╗   ██╗███╗   ██╗██████╗ ███████╗██████╗     ║
║   ╚══██╔══╝██║  ██║██║   ██║████╗  ██║██╔══██╗██╔════╝██╔══██╗    ║
║      ██║   ███████║██║   ██║██╔██╗ ██║██║  ██║█████╗  ██████╔╝    ║
║      ██║   ██╔══██║██║   ██║██║╚██╗██║██║  ██║██╔══╝  ██╔══██╗    ║
║      ██║   ██║  ██║╚██████╔╝██║ ╚████║██████╔╝███████╗██║  ██║    ║
║      ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚═════╝ ╚══════╝╚═╝  ╚═╝    ║
║                                                                   ║
║                  File Streaming Bot v{VERSION}                        ║
╚═══════════════════════════════════════════════════════════════════╝
"""
    print(banner)


def schedule_index_ensure() -> None:
    task = asyncio.create_task(
        db.ensure_indexes(raise_on_error=False),
        name="ensure_database_indexes"
    )

    def _log_index_failure(done_task: asyncio.Task) -> None:
        try:
            if done_task.cancelled():
                return
            created_indexes = done_task.result()
            if created_indexes:
                print("   ✓ Database indexes ensured.")
            else:
                print("   ▶ Database indexes could not be ensured during startup.")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Background database index ensure failed: {e}", exc_info=True)

    task.add_done_callback(_log_index_failure)


async def import_plugins():
    print("╠════════════════════ IMPORTING PLUGINS ════════════════════╣")
    plugins = glob.glob(PLUGIN_PATH)
    if not plugins:
        print("   ▶ No plugins found to import!")
        return 0

    success_count = 0
    failed_plugins = []

    for file_path in plugins:
        try:
            plugin_path = Path(file_path)
            plugin_name = plugin_path.stem
            import_path = f"Thunder.bot.plugins.{plugin_name}"

            spec = importlib.util.spec_from_file_location(
                import_path, plugin_path
            )
            if spec is None or spec.loader is None:
                logger.error(f"Invalid plugin specification for {plugin_name}")
                failed_plugins.append(plugin_name)
                continue

            module = importlib.util.module_from_spec(spec)
            sys.modules[import_path] = module
            spec.loader.exec_module(module)
            success_count += 1

        except Exception as e:
            plugin_name = Path(file_path).stem
            logger.error(f"   ✖ Failed to import plugin {plugin_name}: {e}")
            failed_plugins.append(plugin_name)

    print(
        f"   ▶ Total: {len(plugins)} | Success: {success_count} | "
        f"Failed: {len(failed_plugins)}"
    )
    if failed_plugins:
        print(f"   ▶ Failed plugins: {', '.join(failed_plugins)}")

    return success_count


async def start_services():
    start_time = datetime.now()
    print_banner()
    print("╔════════════════ INITIALIZING BOT SERVICES ════════════════╗")

    print("   ▶ Starting Telegram Bot initialization...")
    try:
        try:
            await StreamBot.start()
        except FloodWait as e:
            logger.debug(f"FloodWait in bot start, sleeping for {e.value}s")
            await asyncio.sleep(e.value)
            await StreamBot.start()
        
        try:
            bot_info = await StreamBot.get_me()
        except FloodWait as e:
            logger.debug(f"FloodWait in get_me, sleeping for {e.value}s")
            await asyncio.sleep(e.value)
            bot_info = await StreamBot.get_me()
        
        StreamBot.username = bot_info.username
        print(f"   ✓ Bot initialized successfully as @{StreamBot.username}")

        await set_commands()
        print("   ✓ Bot commands set successfully.")
        schedule_index_ensure()

        restart_message_data = await db.get_restart_message()
        if restart_message_data:
            try:
                try:
                    await StreamBot.edit_message_text(
                        chat_id=restart_message_data["chat_id"],
                        message_id=restart_message_data["message_id"],
                        text=MSG_ADMIN_RESTART_DONE,
                    )
                except FloodWait as e:
                    logger.debug(f"FloodWait in restart message edit, sleeping for {e.value}s")
                    await asyncio.sleep(e.value)
                    await StreamBot.edit_message_text(
                        chat_id=restart_message_data["chat_id"],
                        message_id=restart_message_data["message_id"],
                        text=MSG_ADMIN_RESTART_DONE,
                    )
                except MessageNotModified:
                    pass
                await db.delete_restart_message(
                    restart_message_data["message_id"]
                )
            except Exception as e:
                logger.error(
                    f"Error processing restart message: {e}", exc_info=True
                )
        else:
            pass

    except Exception as e:
        logger.error(
            f"   ✖ Failed to initialize Telegram Bot: {e}", exc_info=True
        )
        return

    print("   ▶ Starting Client initialization...")
    try:
        await initialize_clients()
    except Exception as e:
        logger.error(f"   ✖ Failed to initialize clients: {e}", exc_info=True)
        return

    await import_plugins()

    print("   ▶ Starting Request Executor initialization...")
    try:
        request_executor_task = asyncio.create_task(
            request_executor(), name="request_executor_task"
        )
        print("   ✓ Request executor service started")
    except Exception as e:
        logger.error(
            f"   ✖ Failed to start request executor: {e}", exc_info=True
        )
        return

    alert_task = None
    try:
        from Thunder.utils.ip_blocker import load_banned_ips

        await load_banned_ips()
    except Exception as e:
        logger.warning(f"Could not load banned caller IPs: {e}")

    print("   ▶ Starting Web Server initialization...")
    try:
        app_runner = web.AppRunner(await web_server())
        await app_runner.setup()
        bind_address = Var.BIND_ADDRESS
        site = web.TCPSite(app_runner, bind_address, Var.PORT)
        await site.start()

        keepalive_task = asyncio.create_task(
            ping_server(), name="keepalive_task"
        )
        print("   ✓ Keep-alive service started")

        if Var.ALERT_ON_STREAM_FAILURES and Var.OWNER_ID:
            alert_task = asyncio.create_task(
                FailureAlerter(
                    metrics,
                    threshold=Var.STREAM_FAILURE_ALERT_THRESHOLD,
                    window_seconds=Var.STREAM_FAILURE_ALERT_WINDOW_MINUTES * 60,
                ).watch(
                    StreamBot, Var.OWNER_ID,
                    on_error=lambda err: logger.warning(
                        f"Could not send stream failure alert: {err}")
                ),
                name="stream_failure_alerter",
            )
            print("   ✓ Stream failure alerting enabled")

    except Exception as e:
        logger.error(f"   ✖ Failed to start Web Server: {e}", exc_info=True)
        if 'request_executor_task' in locals() and not request_executor_task.done():
            request_executor_task.cancel()
            try:
                await request_executor_task
            except asyncio.CancelledError:
                pass
        try:
            await StreamBot.stop()
        except Exception:
            pass
        try:
            await cleanup_clients()
        except Exception:
            pass
        try:
            await rate_limiter.shutdown()
        except Exception:
            pass
        try:
            await db.close()
        except Exception as e:
            logger.error(f"Error during database cleanup: {e}", exc_info=True)
        try:
            await drain_background_touch_tasks()
        except Exception as e:
            logger.error(f"Error during canonical touch task cleanup: {e}", exc_info=True)
        return

    elapsed_time = (datetime.now() - start_time).total_seconds()
    print("╠═══════════════════════════════════════════════════════════╣")
    print(f"   ▶ Bot Name: {bot_info.first_name}")
    print(f"   ▶ Username: @{bot_info.username}")
    print(f"   ▶ Server: {bind_address}:{Var.PORT}")
    print(f"   ▶ Startup Time: {elapsed_time:.2f} seconds")
    print("╚═══════════════════════════════════════════════════════════╝")
    print("   ▶ Bot is now running! Press CTRL+C to stop.")

    background_tasks = [
        request_executor_task,
        keepalive_task
    ]
    if alert_task is not None:
        background_tasks.append(alert_task)

    try:
        await idle()
    finally:
        print("   ▶ Shutting down services...")

        for task in background_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        try:
            await rate_limiter.shutdown()
        except Exception as e:
            logger.error(f"Error during rate limiter cleanup: {e}")

        try:
            await cleanup_clients()
        except Exception as e:
            logger.error(f"Error during client cleanup: {e}")

        try:
            await drain_background_touch_tasks()
        except Exception as e:
            logger.error(f"Error during canonical touch task cleanup: {e}", exc_info=True)

        if 'app_runner' in locals() and app_runner is not None:
            try:
                await app_runner.cleanup()
            except Exception as e:
                logger.error(f"Error during web server cleanup: {e}")

        try:
            await db.close()
            print("   ✓ Database connection closed")
        except Exception as e:
            logger.error("Error during database cleanup", exc_info=True)


if __name__ == '__main__':
    # Auto-fetch code from UPSTREAM_REPO on boot if configured
    if os.getenv("UPSTREAM_REPO") and not os.getenv("UPSTREAM_SYNCED"):
        try:
            marker = Path(__file__).resolve().parent.parent / ".upstream_synced"
            already_synced = False
            if marker.exists():
                try:
                    with open(marker, "r") as f:
                        if (time.time() - float(f.read().strip())) < 120:
                            already_synced = True
                except Exception:
                    pass

            if not already_synced:
                update_script = Path(__file__).resolve().parent.parent / "update.py"
                if update_script.exists():
                    res = subprocess.run([sys.executable, str(update_script)], check=False)
                    if res.returncode == 0:
                        os.environ["UPSTREAM_SYNCED"] = "1"
                        if sys.platform == "win32":
                            sys.exit(subprocess.call([sys.executable, "-m", "Thunder"]))
                        else:
                            os.execv(sys.executable, [sys.executable, "-m", "Thunder"])
        except Exception as e:
            logger.error(f"Error during auto-update check on boot: {e}")

    try:
        asyncio.run(start_services())
    except KeyboardInterrupt:
        print("╔═══════════════════════════════════════════════════════════╗")
        print("║                   Bot stopped by user (CTRL+C)            ║")
        print("╚═══════════════════════════════════════════════════════════╝")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
