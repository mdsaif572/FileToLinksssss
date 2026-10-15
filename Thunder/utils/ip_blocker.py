# Thunder/utils/ip_blocker.py
"""Caller-IP blocking for the streaming routes.

The check runs on every media request, so it is an O(1) set lookup against an
in-memory cache; MongoDB is only the durable copy. A ban takes effect the moment
it is issued (no restart) and survives a restart by loading the cache at boot.
"""

from typing import Dict, List, Optional

from Thunder.utils.logger import logger

# ip -> record. Kept as a dict so the dashboard can show why/when.
_BANNED_IPS: Dict[str, dict] = {}


def normalise_ip(ip: Optional[str]) -> str:
    """Trim a caller key into the form used as the cache key."""
    return (ip or "").strip()


def is_ip_banned(ip: Optional[str]) -> bool:
    """O(1) check used by the streaming routes."""
    key = normalise_ip(ip)
    return bool(key) and key in _BANNED_IPS


def get_banned_ips() -> List[dict]:
    """Banned callers, newest first, without leaking the internal dict."""
    return sorted(
        (dict(record) for record in _BANNED_IPS.values()),
        key=lambda record: str(record.get("banned_at") or ""),
        reverse=True,
    )


def clear_cache() -> None:
    """Forget every cached ban (tests, and before a fresh load)."""
    _BANNED_IPS.clear()


def _remember(record: dict) -> dict:
    _BANNED_IPS[str(record["ip"])] = dict(record)
    return record


async def load_banned_ips() -> int:
    """Fill the cache from the database. Returns how many bans were loaded."""
    from Thunder.utils.database import db

    try:
        records = await db.get_banned_ips()
    except Exception as e:
        logger.warning(f"Could not load banned IPs: {e}")
        return 0

    clear_cache()
    for record in records:
        if record.get("ip"):
            _remember(record)
    if _BANNED_IPS:
        logger.info(f"Loaded {len(_BANNED_IPS)} banned caller IP(s)")
    return len(_BANNED_IPS)


async def ban_ip(ip: str, reason: str = "", banned_by: Optional[int] = None) -> dict:
    """Block a caller. In-memory first (immediate), then persisted."""
    from Thunder.utils.database import db
    import datetime

    key = normalise_ip(ip)
    if not key:
        raise ValueError("ban_ip needs a non-empty caller address")

    record = {
        "ip": key,
        "reason": reason,
        "banned_by": banned_by,
        "banned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _remember(record)

    try:
        await db.add_banned_ip(key, reason=reason, banned_by=banned_by)
    except Exception as e:
        logger.error(f"Ban for {key} is only in memory: {e}")
    return record


async def unban_ip(ip: str) -> bool:
    """Restore access immediately, then drop the durable copy."""
    from Thunder.utils.database import db

    key = normalise_ip(ip)
    removed = _BANNED_IPS.pop(key, None) is not None

    try:
        persisted = await db.remove_banned_ip(key)
    except Exception as e:
        logger.error(f"Unban for {key} only applied in memory: {e}")
        persisted = False

    return removed or persisted
