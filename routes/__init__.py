"""Shared helpers for the admin route modules."""

import functools

from aiohttp import web

from config import check_password


def json_response(payload, status=200):
    return web.json_response(payload, status=status)


def unauthorized():
    return json_response({"error": "Unauthorized: Invalid API Password"}, status=401)


def admin(handler):
    """Answer 401 before running a handler without a valid API password."""

    @functools.wraps(handler)
    async def wrapper(request, *args, **kwargs):
        if not check_password(request):
            return unauthorized()
        return await handler(request, *args, **kwargs)

    return wrapper


__all__ = ["admin", "json_response", "unauthorized"]
