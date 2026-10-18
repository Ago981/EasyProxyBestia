"""Shared helpers for the admin route modules (re-exported from the service layer)."""

from services.proxy_shared import admin_guard as admin, json_response, unauthorized

__all__ = ["admin", "json_response", "unauthorized"]
