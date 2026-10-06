# AGENTS.md — New extractor (mandatory contract)

References (standard contract, nothing else needed):
- Direct aiohttp → `fctv33.py`.
- Node runner (wasm/obfs) → `cinejoy.py`.

## 1. Extractor file — `extractors/<name>.py`

```python
from config import get_preferred_proxy_for_url, get_connector_for_proxy
import config as _cfg
from extractors.base import BaseExtractor, ExtractorError
from aiohttp import ClientSession, ClientTimeout, TCPConnector

class <Name>Extractor(BaseExtractor):
    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies=proxies, extractor_name="<name>")
        self.mediaflow_endpoint = "hls_proxy"  # hls_proxy | hls_manifest_proxy | proxy_stream_endpoint (.mp4) | mpd_manifest_proxy
        self.bypass_warp_active = bool(bypass_warp)
        self.last_used_proxy = None
        self._forced_proxy = None
        self._force_direct = False

    async def _get_session(self, url: str = None):
        forced = getattr(self, "_forced_proxy", None)
        force_direct = bool(getattr(self, "_force_direct", False))
        if force_direct:
            proxy_url = None
        elif forced:
            proxy_url = forced
            if proxy_url and _cfg.is_warp_proxy_url(proxy_url) and self.bypass_warp_active:
                proxy_url = None
        else:
            proxy_url = await get_preferred_proxy_for_url(url, self.extractor_name, self.proxies or _cfg.GLOBAL_PROXIES, self.bypass_warp_active)
        if proxy_url is None and not _cfg.is_direct_connection_allowed(self.bypass_warp_active):
            raise ExtractorError("<Name>: direct fallback disabled; no proxy route available")
        async with self._session_lock:
            self.session = self._route_sessions.get(proxy_url)
            self._session_proxy = proxy_url
            if self.session is None or self.session.closed:
                timeout = ClientTimeout(total=60, connect=30, sock_read=30)
                connector = get_connector_for_proxy(proxy_url) if proxy_url else TCPConnector(limit=0, limit_per_host=0, keepalive_timeout=15, enable_cleanup_closed=True, use_dns_cache=True)
                self.session = ClientSession(timeout=timeout, connector=connector, headers={'User-Agent': self.base_headers["User-Agent"]})
                self._route_sessions[proxy_url] = self.session
            self.last_used_proxy = proxy_url
            return self.session

    async def extract(self, url: str, **kwargs) -> dict:
        # MANDATORY at the top of extract: routing merge (proxy_streaming re-extract passes bypass_warp/proxy as kwargs, not via context)
        raw_proxy = kwargs.get("proxy")
        bypass_proxies = str(raw_proxy or "").lower() in {"off", "none", "no"} or _cfg.BYPASS_PROXIES_CONTEXT.get()
        bypass_warp = bool(kwargs.get("bypass_warp") or str(kwargs.get("warp", "")).lower() == "off" or _cfg.BYPASS_WARP_CONTEXT.get() or self.bypass_warp_active)
        self.bypass_warp_active = bypass_warp
        direct_requested = str(kwargs.get("direct", "")).lower() in {"1", "true", "yes", "on"} or (bypass_proxies and bypass_warp)
        if direct_requested or (bypass_proxies and bypass_warp):
            self._forced_proxy, self._force_direct = None, True
        elif bypass_proxies:
            self._forced_proxy, self._force_direct = (_cfg.WARP_PROXY_URL if _cfg._get_dynamic_warp_enabled() else None), False
            if not self._forced_proxy:
                self._force_direct = True
        elif raw_proxy and str(raw_proxy).lower() not in {"on", "auto", "true", ""}:
            self._forced_proxy, self._force_direct = str(raw_proxy), False
        else:
            self._forced_proxy, self._force_direct = None, False

        session = await self._get_session(url)
        # ... your logic ...
        return {
            "destination_url": signed_url,
            "request_headers": {"User-Agent": ..., "Referer": ..., "Origin": ...},
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self.last_used_proxy,  # MANDATORY: downstream uses it for segments
            "force_direct": self._force_direct,      # MANDATORY
            "bypass_warp": self.bypass_warp_active,  # MANDATORY
        }
```

Notes:
- Node runner (wasm/obfs) → copy `cinejoy.py`: `get_http_bridge_for_proxy(proxy)` + `env["<NAME>_PROXY"]`, still return the 3 fields above.
- Stdlib/aiohttp for HTTP only. No new dependencies.
- Optional, only if needed: `force_ipv4 = True` (IPv4-bound tokens / CDN behind CF, see `vavoo.py`), `REQUEST_TIMEOUT_TOTAL = 60+` (slow solvers; default 30 in `proxy_extractor.py`), `disable_ssl` / `captured_manifest(s)` in the return dict.

## 2. `extractors/registry_imports.py` — import + `__all__`

```python
try:
    from extractors.<name> import <Name>Extractor
    logger.info("✅ <Name>Extractor module loaded.")
except Exception as e:
    logger.warning("⚠️ <Name>Extractor failed to load: %s", e)
    <Name>Extractor = None
# + add "<Name>Extractor" to __all__
```

Without this, `available_extractors` (admin warp_off/proxy_off/max_res toggles) won't see it. The admin toggles are then automatic via `extractor_name` — no further changes needed.

## 3. `extractors/registry_resolver.py` — BOTH branches

Forced host (`?host=<name>`) and auto-detect:

```python
# host branch (section 1)
elif host in {"<name>", "<alias1>"}:
    key = _cache_key("<name>", bypass_warp)
    if <Name>Extractor is None:
        raise RuntimeError("<Name>Extractor module not available")
    proxy = get_proxy_for_url(url, bypass_warp=bypass_warp, extractor_name="<name>")
    proxy_list = _build_proxy_list(proxy, "<name>")
    if key not in self.extractors:
        self.extractors[key] = <Name>Extractor(request_headers, proxies=proxy_list, bypass_warp=bypass_warp)
    return self.extractors[key]

# auto-detect branch (bottom of the function, before the GenericHLS fallback)
elif "<domain>" in url.lower() or url.lower().startswith("<name>://"):
    ...same as the host branch...
```

FORBIDDEN: `get_proxy_for_url("<name>")` with a literal — it breaks `TRANSPORT_ROUTES` (substring match on the real URL), `warp/proxy_exclude_domains` (hostname parse → `None`) and `extractor_proxies`. Always use `url` + `extractor_name="<name>"`. Always pass `bypass_warp=` to the constructor.

## 4. `services/proxy_extractor.py` — help JSON

Add `"<name>"` to `available_hosts` + an example `.../extractor/video?host=<name>&d=...`.

## 5. UI — TWO static templates (not auto-generated)

- `templates/url_generator.html` → `extractorHostSelect`: `<option value="<name>">...</option>` (the DUAL selects clone from it via JS, once is enough).
- `templates/recordings.html` → `datalist`: `<option value="<name>"></option>`.

## 6. `extractors/extractor_test_urls.json`

`"<name>": "https://..."` — feeds the extractor test API/UI.

## 7. Verify

```powershell
python -m py_compile extractors/<name>.py extractors/registry_resolver.py extractors/registry_imports.py
```

Smoke test: `/extractor/video?host=<name>&d=<test-url>` then the same with `&warp=off` and `&proxy=off` — both must come out on the right route.

Pre-PR checklist: `?warp=off`, `?proxy=off`, forced `?proxy=<alias>` and `TRANSPORT_ROUTES` honored; admin `warp_off/proxy_off` work (separate cache key); 403 re-extract reuses the right route (returns the 3 fields); URL generator + recordings show the host; `available_hosts` lists it.
