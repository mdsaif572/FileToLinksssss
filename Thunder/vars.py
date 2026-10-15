# Thunder/vars.py

import os

from dotenv import load_dotenv
from typing import Set, Optional
from Thunder.utils.logger import logger

load_dotenv("config.env")

def str_to_bool(val: str) -> bool:
    return val.lower() in ("true", "1", "t", "y", "yes")

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
    MAX_CONCURRENT_PER_USER: int = int(os.getenv("MAX_CONCURRENT_PER_USER", "3"))

    ALERT_ON_STREAM_FAILURES: bool = str_to_bool(os.getenv("ALERT_ON_STREAM_FAILURES", "True"))
    STREAM_FAILURE_ALERT_THRESHOLD: int = int(os.getenv("STREAM_FAILURE_ALERT_THRESHOLD", "5"))
    STREAM_FAILURE_ALERT_WINDOW_MINUTES: int = int(os.getenv("STREAM_FAILURE_ALERT_WINDOW_MINUTES", "5"))
    UPSTREAM_REPO: str = os.getenv("UPSTREAM_REPO", "").strip().strip('"').strip("'")
    UPSTREAM_BRANCH: str = os.getenv("UPSTREAM_BRANCH", "main").strip().strip('"').strip("'") or "main"
