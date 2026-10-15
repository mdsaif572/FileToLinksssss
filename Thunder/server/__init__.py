# Thunder/server/__init__.py

from aiohttp import web


async def web_server():
    from .admin_routes import routes as admin_routes
    from .stream_routes import routes

    web_app = web.Application(client_max_size=50 * 1024 * 1024)
    # Admin first: stream_routes ends with a /{path:.+} catch-all that would
    # otherwise swallow /admin, /api/admin/* and /healthz.
    web_app.add_routes(admin_routes)
    web_app.add_routes(routes)
    return web_app
