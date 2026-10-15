#!/usr/bin/env python3
"""Boot the real HTTP app without Telegram and exercise the admin dashboard.

Useful on a VPS or in CI: it starts the same aiohttp application the bot serves,
then walks through health, login, the stats API and the ban/unban workflow. No
Telegram credentials or database are required (ban persistence is skipped when
MongoDB is unavailable, exactly as in production).

    python scripts/dashboard_check.py          # random free port
    python scripts/dashboard_check.py --port 8099
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = os.getenv("SMOKE_ADMIN_PASSWORD", "dashboard-check-pass")

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:DASHBOARD-CHECK")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/dashboard_check")
os.environ.setdefault("HAS_SSL", "False")
os.environ["WEB_ADMIN_PASSWORD"] = PASSWORD


async def main(port: int) -> int:
    from aiohttp import ClientSession, CookieJar, web

    from Thunder.bot import client_channels, multi_clients, work_loads
    from Thunder.server import web_server
    from Thunder.utils import ip_blocker
    from Thunder.vars import Var

    app = await web_server()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base = f"http://127.0.0.1:{port}"

    # one stub client so the pool table has a row
    class _Stub:
        class storage:
            dc_id = 2

        me = None

    multi_clients.clear()
    multi_clients[0] = _Stub()
    work_loads[0] = 0
    client_channels[0] = frozenset({int(channel) for channel in Var.BIN_CHANNELS})

    failures = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not condition:
            failures.append(label)

    print(f"\nDashboard check on {base}\n")

    # CookieJar(unsafe=True): aiohttp's default jar drops cookies for bare IPs
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as session:
        async with session.get(f"{base}/healthz") as response:
            body = await response.json()
        check("/healthz is public", response.status == 200 and body["status"] == "ok")
        check("/healthz leaks no secrets",
              not any(secret in str(body) for secret in (PASSWORD, Var.BOT_TOKEN)))

        async with session.get(f"{base}/admin") as response:
            html = await response.text()
        check("/admin renders the login page",
              response.status == 200 and "Cloud Station Admin" in html)

        async with session.get(f"{base}/api/admin/stats") as response:
            check("stats requires a session", response.status == 401)

        async with session.post(f"{base}/admin/login", data={"password": "wrong"},
                                allow_redirects=False) as response:
            check("wrong password is rejected",
                  response.status == 302 and "error=1" in (response.headers.get("Location") or ""))

        async with session.post(f"{base}/admin/login", data={"password": PASSWORD},
                                allow_redirects=False) as response:
            location = response.headers.get("Location")
            await response.read()
        check("login sets a session and redirects",
              response.status == 302 and location == "/admin/dashboard")

        async with session.get(f"{base}/admin/dashboard") as response:
            dashboard = await response.text()
        check("dashboard renders with a session",
              response.status == 200 and "Bot pool" in dashboard)

        async with session.get(f"{base}/api/admin/stats") as response:
            stats = await response.json()
        check("stats payload is complete",
              response.status == 200 and {"bots", "callers", "metrics", "limits", "banned_ips"} <= set(stats))

        async with session.post(f"{base}/api/admin/ban_ip", json={"ip": "8.8.8.8", "reason": "check"}) as response:
            await response.json()
        check("ban applies", ip_blocker.is_ip_banned("8.8.8.8"))

        async with session.get(f"{base}/api/admin/stats") as response:
            stats = await response.json()
        check("banned caller is listed", [e["ip"] for e in stats["banned_ips"]] == ["8.8.8.8"])

        async with session.post(f"{base}/api/admin/unban_ip", json={"ip": "8.8.8.8"}) as response:
            await response.json()
        check("unban applies", not ip_blocker.is_ip_banned("8.8.8.8"))

        async with session.post(f"{base}/admin/logout", allow_redirects=False) as response:
            await response.read()
        async with session.get(f"{base}/api/admin/stats") as response:
            check("logout ends the session", response.status == 401)

    await runner.cleanup()

    print(f"\n{'all checks passed' if not failures else 'FAILURES: ' + ', '.join(failures)}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="port to bind (0 = pick a free one)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.port)))
