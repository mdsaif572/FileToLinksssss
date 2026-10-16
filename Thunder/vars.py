# Thunder/vars.py

import os

from dotenv import load_dotenv
from typing import Set, Optional
from Thunder.utils.logger import logger

load_dotenv("config.env")

def str_to_bool(val: str) -> bool:
    return val.lower() in ("true", "1", "t", "y", "yes")

def parse_bin_channels(environ, primary: int) -> list:
    """Every configured storage channel: primary first, extras in key order.

    Telegram caps channel administrators at 50 (bots can only be added to a
    channel as an administrator), so a large bot pool needs more than one
    storage channel; files are then spread over them by :func:`choose_bin_channel`.
    """
    channels = [int(primary)]
    for index in range(2, 50):
        raw = (environ.get(f"BIN_CHANNEL_{index}") or "").strip().strip('"').strip("'")
        if not raw:
            continue
        try:
            channel = int(raw)
        except ValueError:
            logger.warning(f"BIN_CHANNEL_{index} is not an integer id; ignoring it")
            continue
        if channel not in channels:
            channels.append(channel)
    return channels


def str_to_int_set(val: str) -> Set[int]:
    if not val:
        return set()
    result: Set[int] = set()
    for x in val.split():
        try:
            result.add(int(x))
        except (TypeError, ValueError):
            continue
    return result



class Var:
    API_ID: int = int(os.getenv("API_ID", "0"))
    API_HASH: str = os.getenv("API_HASH", "")
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

    if not all([API_ID, API_HASH, BOT_TOKEN]):
        logger.critical("Missing required Telegram API configuration")
        raise ValueError("Missing required Telegram API configuration")

    NAME: str = os.getenv("NAME", "CloudStationF2L")
    SLEEP_THRESHOLD: int = int(os.getenv("SLEEP_THRESHOLD", "600"))
    WORKERS: int = int(os.getenv("WORKERS", "8"))

    BIN_CHANNEL: int = int(os.getenv("BIN_CHANNEL", "0"))

    if not BIN_CHANNEL:
        logger.critical("BIN_CHANNEL is required")
        raise ValueError("BIN_CHANNEL is required")

    # Optional extra storage channels: BIN_CHANNEL_2, BIN_CHANNEL_3, ...
    BIN_CHANNELS: list = parse_bin_channels(os.environ, BIN_CHANNEL)

    PORT: int = int(os.getenv("PORT", "8080"))
    BIND_ADDRESS: str = os.getenv("BIND_ADDRESS", "0.0.0.0")
    PING_INTERVAL: int = int(os.getenv("PING_INTERVAL", "840"))
    NO_PORT: bool = str_to_bool(os.getenv("NO_PORT", "True"))

    OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))

    if not OWNER_ID:
        logger.warning("WARNING: OWNER_ID is not set. No user will be granted owner access.")

    FQDN: str = os.getenv("FQDN", "") or BIND_ADDRESS
    HAS_SSL: bool = str_to_bool(os.getenv("HAS_SSL", "True"))
    PROTOCOL: str = "https" if HAS_SSL else "http"
    PORT_SEGMENT: str = "" if NO_PORT else f":{PORT}"
    URL: str = f"{PROTOCOL}://{FQDN}{PORT_SEGMENT}/"

    SET_COMMANDS: bool = str_to_bool(os.getenv("SET_COMMANDS", "True"))

    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    if not DATABASE_URL:
        logger.critical("DATABASE_URL is required")
        raise ValueError("DATABASE_URL is required")

    MONGO_MAX_POOL_SIZE: int = int(os.getenv("MONGO_MAX_POOL_SIZE", "200"))
    MONGO_MIN_POOL_SIZE: int = int(os.getenv("MONGO_MIN_POOL_SIZE", "20"))
    MONGO_CONNECT_TIMEOUT_MS: int = int(os.getenv("MONGO_CONNECT_TIMEOUT_MS", "5000"))
    MONGO_SERVER_SELECTION_TIMEOUT_MS: int = int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "10000"))
    MONGO_READ_PREFERENCE: str = os.getenv("MONGO_READ_PREFERENCE", "secondaryPreferred")

    MAX_BATCH_FILES: int = int(os.getenv("MAX_BATCH_FILES", "50"))

    # 0 = links never expire; otherwise every new canonical link lives this long
    LINK_TTL_HOURS: int = int(os.getenv("LINK_TTL_HOURS", "0"))

    # Users may send a direct URL and get a Cloud Station link back
    URL_UPLOAD_ENABLED: bool = str_to_bool(os.getenv("URL_UPLOAD_ENABLED", "True"))

    # Password for the /admin web dashboard. Empty disables it entirely (the page
    # then only shows a setup warning and refuses every request).
    WEB_ADMIN_PASSWORD: str = os.getenv("WEB_ADMIN_PASSWORD", "").strip().strip('"').strip("'")

    CHANNEL: bool = str_to_bool(os.getenv("CHANNEL", "False"))

    BANNED_CHANNELS: Set[int] = str_to_int_set(os.getenv("BANNED_CHANNELS", ""))

    MULTI_CLIENT: bool = False

    FORCE_CHANNEL_ID: Optional[int] = None

    force_channel_env = os.getenv("FORCE_CHANNEL_ID", "").strip()

    if force_channel_env:
        try:
            FORCE_CHANNEL_ID = int(force_channel_env)
        except ValueError:
            logger.warning(f"Invalid FORCE_CHANNEL_ID '{force_channel_env}' in environment; must be an integer.")

    TOKEN_ENABLED: bool = str_to_bool(os.getenv("TOKEN_ENABLED", "False"))
    TOKEN_TTL_HOURS: int = int(os.getenv("TOKEN_TTL_HOURS", "24"))

    SHORTEN_ENABLED: bool = str_to_bool(os.getenv("SHORTEN_ENABLED", "False"))
    SHORTEN_MEDIA_LINKS: bool = str_to_bool(os.getenv("SHORTEN_MEDIA_LINKS", "False"))
    URL_SHORTENER_API_KEY: str = os.getenv("URL_SHORTENER_API_KEY", "")
    URL_SHORTENER_SITE: str = os.getenv("URL_SHORTENER_SITE", "")

    GLOBAL_RATE_LIMIT: bool = str_to_bool(os.getenv("GLOBAL_RATE_LIMIT", "False"))
    MAX_GLOBAL_REQUESTS_PER_MINUTE: int = int(os.getenv("MAX_GLOBAL_REQUESTS_PER_MINUTE", "4"))

    RATE_LIMIT_ENABLED: bool = str_to_bool(os.getenv("RATE_LIMIT_ENABLED", "False"))
    MAX_FILES_PER_PERIOD: int = int(os.getenv("MAX_FILES_PER_PERIOD", "2"))
    RATE_LIMIT_PERIOD_MINUTES: int = int(os.getenv("RATE_LIMIT_PERIOD_MINUTES", "1"))
    MAX_QUEUE_SIZE: int = int(os.getenv("MAX_QUEUE_SIZE", "100"))

    MAX_CONCURRENT_PER_CLIENT: int = int(os.getenv("MAX_CONCURRENT_PER_CLIENT", "16"))
    # Simultaneous streams allowed per caller (browser/player) address.
    # A player legitimately opens several range requests at once, so keep this
    # comfortably above one - it exists to stop one host starving everyone.
    MAX_CONCURRENT_PER_USER: int = int(os.getenv("MAX_CONCURRENT_PER_USER", "8"))

    # How long a single 1 MiB chunk may take before the read counts as stalled.
    # Keep it at or above 30s: wzgram sleeps `upload.GetFile` FloodWaits of up to
    # 30s internally, and a shorter timeout cancels the RPC before that wait can
    # finish - which re-rolls the FloodWait (usually a longer one) instead of
    # letting the read complete.
    STREAM_CHUNK_TIMEOUT_SECONDS: float = float(os.getenv("STREAM_CHUNK_TIMEOUT_SECONDS", "30"))
    # Total time a stream may spend with no data arriving before it gives up.
    # Must stay below the host's idle-connection window (Heroku router: H15 after
    # 55s of silence) - the retries are useless once the router has dropped the
    # client, and a response that goes silent for longer than that is killed
    # mid-recovery.
    STREAM_STALL_BUDGET_SECONDS: float = float(os.getenv("STREAM_STALL_BUDGET_SECONDS", "45"))
    # Budget for uploading the log document on /log. An MTProto upload queues
    # behind the client's transfer budget while files are streaming, so this has
    # to cover real streaming load - a 25s cap cancelled the upload while it was
    # still waiting for a slot.
    LOG_UPLOAD_TIMEOUT_SECONDS: float = float(os.getenv("LOG_UPLOAD_TIMEOUT_SECONDS", "120"))
    # Longest FloodWait the log upload is willing to sit out before falling back
    # to sending the log tail as text.
    LOG_UPLOAD_FLOODWAIT_CAP_SECONDS: float = float(os.getenv("LOG_UPLOAD_FLOODWAIT_CAP_SECONDS", "60"))

    ALERT_ON_STREAM_FAILURES: bool = str_to_bool(os.getenv("ALERT_ON_STREAM_FAILURES", "True"))
    STREAM_FAILURE_ALERT_THRESHOLD: int = int(os.getenv("STREAM_FAILURE_ALERT_THRESHOLD", "5"))
    STREAM_FAILURE_ALERT_WINDOW_MINUTES: int = int(os.getenv("STREAM_FAILURE_ALERT_WINDOW_MINUTES", "5"))
    UPSTREAM_REPO: str = os.getenv("UPSTREAM_REPO", "").strip().strip('"').strip("'")
    UPSTREAM_BRANCH: str = os.getenv("UPSTREAM_BRANCH", "main").strip().strip('"').strip("'") or "main"
