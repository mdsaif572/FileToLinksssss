# Thunder/server/admin_routes.py
"""HTTP surface of the web admin dashboard.

Registered before the streaming routes so ``/admin``, ``/api/admin/*`` and
``/healthz`` are matched ahead of the ``/{path:.+}`` media catch-all. Everything
except the login view, the login POST and ``/healthz`` requires a signed session
cookie; the password lives in ``WEB_ADMIN_PASSWORD``.
"""

import time
from typing import Any, Dict

from aiohttp import web

from Thunder import StartTime, __version__
from Thunder.bot import client_channels, multi_clients, work_loads
from Thunder.bot.clients import _channel_label
from Thunder.server.admin_auth import (COOKIE_NAME, clear_login_failures,
                                       create_admin_session, login_allowed,
                                       password_is_configured,
                                       record_login_failure,
                                       session_cookie_kwargs,
                                       verify_admin_password,
                                       verify_admin_session)
from Thunder.server import stream_routes as stream_routes_ref
from Thunder.utils import ip_blocker
from Thunder.utils.logger import logger
from Thunder.utils.metrics import metrics
from Thunder.utils.render_template import template_env
from Thunder.vars import Var

routes = web.RouteTableDef()


async def _render(template_name: str, **context) -> str:
    template = template_env.get_template(template_name)
    return await template.render_async(**context)


def _require_admin(request: web.Request) -> None:
    if not verify_admin_session(request):
        raise web.HTTPUnauthorized(
            text="Admin session required",
            headers={"WWW-Authenticate": "Cookie"},
        )


def _process_usage() -> Dict[str, Any]:
    try:
        import psutil

        process = psutil.Process()
        return {
            "rss_mb": round(process.memory_info().rss / (1024 * 1024), 1),
            "cpu_percent": process.cpu_percent(interval=None),
        }
    except Exception as e:
        logger.debug(f"Process usage unavailable: {e}")
        return {}


def build_stats() -> Dict[str, Any]:
    """Everything the dashboard polls, assembled from live in-process state."""
    channels = list(getattr(Var, "BIN_CHANNELS", None) or [Var.BIN_CHANNEL])

    bots = []
    for client_id in sorted(multi_clients):
        client = multi_clients[client_id]
        readable = client_channels.get(client_id, frozenset())
        bots.append({
            "id": client_id,
            "name": getattr(getattr(client, "me", None), "username", None),
            "dc": stream_routes_ref._client_dc(client),
            "load": int(work_loads.get(client_id, 0)),
            "channels": [
                {
                    "id": int(channel),
                    "label": _channel_label(channel),
                    "readable": int(channel) in readable,
                }
                for channel in channels
            ],
        })

    uptime = time.time() - StartTime
    return {
        "server": {
            "version": __version__,
            "uptime": time.strftime("%H:%M:%S", time.gmtime(uptime)),
            "uptime_seconds": int(uptime),
            "channels": [{"id": int(c), "label": _channel_label(c)} for c in channels],
        },
        "metrics": metrics.snapshot(),
        "bots": bots,
        "callers": {
            str(key): int(value)
            for key, value in stream_routes_ref.user_streams.snapshot().items()
        },
        "limits": {
            "per_user": stream_routes_ref.user_streams.limit,
            "per_client": stream_routes_ref.MAX_CONCURRENT_PER_CLIENT,
        },
        "banned_ips": ip_blocker.get_banned_ips(),
        "process": _process_usage(),
    }


@routes.get("/healthz")
async def healthz(request: web.Request):
    """Unauthenticated liveness probe: no configuration or secrets in it."""
    return web.json_response({
        "status": "ok",
        "version": __version__,
        "clients": len(multi_clients),
        "active_streams": metrics.active_streams(),
    })


@routes.get("/admin")
async def admin_home(request: web.Request):
    if verify_admin_session(request):
        raise web.HTTPFound("/admin/dashboard")

    error = "Incorrect password. Try again." if request.query.get("error") else None
    html = await _render(
        "admin_login.html",
        configured=password_is_configured(),
        error=error,
    )
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


@routes.post("/admin/login")
async def admin_login(request: web.Request):
    caller = stream_routes_ref.caller_key(request)

    if not password_is_configured():
        raise web.HTTPFound("/admin?error=1")

    if not login_allowed(caller):
        logger.warning(f"Too many admin login attempts from {caller}")
        raise web.HTTPTooManyRequests(text="Too many attempts. Wait a few minutes.")

    form = await request.post()
    submitted = form.get("password")
    if not verify_admin_password(submitted if isinstance(submitted, str) else ""):
        record_login_failure(caller)
        logger.warning(f"Failed admin login from {caller}")
        raise web.HTTPFound("/admin?error=1")

    clear_login_failures(caller)
    response = web.HTTPFound("/admin/dashboard")
    response.set_cookie(COOKIE_NAME, create_admin_session(), **session_cookie_kwargs())
    logger.info(f"Admin login from {caller}")
    return response


@routes.post("/admin/logout")
async def admin_logout(request: web.Request):
    response = web.HTTPFound("/admin")
    response.del_cookie(COOKIE_NAME, path="/")
    return response


@routes.get("/admin/dashboard")
async def admin_dashboard(request: web.Request):
    if not verify_admin_session(request):
        raise web.HTTPFound("/admin")

    html = await _render("admin_dashboard.html", version=__version__)
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


@routes.get("/api/admin/stats")
async def admin_stats(request: web.Request):
    _require_admin(request)
    return web.json_response(build_stats(), headers={"Cache-Control": "no-store"})


@routes.post("/api/admin/ban_ip")
async def admin_ban_ip(request: web.Request):
    _require_admin(request)
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Expected a JSON body")

    ip = str(payload.get("ip") or "").strip()
    if not ip:
        return web.json_response({"error": "ip is required"}, status=400)

    record = await ip_blocker.ban_ip(ip, reason=str(payload.get("reason") or ""))
    return web.json_response({"ok": True, "banned": record})


@routes.post("/api/admin/unban_ip")
async def admin_unban_ip(request: web.Request):
    _require_admin(request)
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Expected a JSON body")

    ip = str(payload.get("ip") or "").strip()
    if not ip:
        return web.json_response({"error": "ip is required"}, status=400)

    removed = await ip_blocker.unban_ip(ip)
    return web.json_response({"ok": True, "removed": removed})
