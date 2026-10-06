import asyncio
import base64
import json
import logging
import os
import re
import time
import urllib.parse
from typing import Dict, Any, Optional

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

from extractors.base import BaseExtractor, ExtractorError
from config import get_connector_for_proxy, get_preferred_proxy_for_url
import config as _cfg

logger = logging.getLogger(__name__)

# --- Cryptographic & Protocol Constants ---
AES_KEY = b"a7981cc9eb2f4d19dcfea57b101ecd89"
AES_IV = b"8017d3a8f1400d2f"

DEFAULT_DATA_API_BASE = "https://apis-data10.tcdru136ovur.ru"
DEFAULT_PLAYER_REFERER = "https://nadia01eo.tn76degree12ec3out.cfd/"
DEFAULT_STREAM_DIGIT = "seth"
SITE_URL = "https://www.fctv33hd.rest"


def rot47(text: str) -> str:
    res = []
    for c in text:
        code = ord(c)
        if 0x21 <= code <= 0x4f:
            res.append(chr(code + 0x2f))
        elif 0x50 <= code <= 0x7e:
            res.append(chr(code - 0x2f))
        else:
            res.append(c)
    return "".join(res)


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def encrypt_aes_cbc(data: bytes, key: bytes, iv: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return cipher.encrypt(pkcs7_pad(data))
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        encryptor = cipher.encryptor()
        return encryptor.update(pkcs7_pad(data)) + encryptor.finalize()


def build_signed_stream_url(obfuscated_url: str, session_token: str) -> str:
    decoded = rot47(obfuscated_url)[8:]
    parsed = urllib.parse.urlsplit(decoded)
    encrypted = encrypt_aes_cbc(session_token.encode("utf-8"), AES_KEY, AES_IV)
    token_b64 = base64.b64encode(encrypted).decode("utf-8")
    token = f"{urllib.parse.quote(token_b64, safe='')}a"
    path_with_token = f"/token-{token}{parsed.path}"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{parsed.netloc}{path_with_token}{query}"


def read_varint(buffer: bytes, offset: int = 0):
    value = 0
    shift = 0
    index = offset
    length = len(buffer)
    while index < length:
        byte = buffer[index]
        index += 1
        value |= (byte & 0x7f) << shift
        if (byte & 0x80) == 0:
            break
        shift += 7
    return value, index


def read_length_delimited(buffer: bytes, offset: int = 0):
    length, start = read_varint(buffer, offset)
    return buffer[start:start + length], start + length


def read_fields(buffer: bytes):
    fields = {}
    offset = 0
    length = len(buffer)
    while offset < length:
        tag, next_offset = read_varint(buffer, offset)
        offset = next_offset
        field = tag >> 3
        wire = tag & 0x7
        if wire == 0:
            val, after = read_varint(buffer, offset)
            offset = after
            fields.setdefault(field, []).append(val)
            continue
        if wire == 2:
            chunk, after = read_length_delimited(buffer, offset)
            offset = after
            fields.setdefault(field, []).append(chunk)
            continue
        break
    return fields


def parse_api_envelope(buffer: bytes) -> dict:
    fields = read_fields(buffer)
    msg_chunks = fields.get(3, [])
    msg = ""
    if msg_chunks:
        if isinstance(msg_chunks[0], bytes):
            msg = msg_chunks[0].decode("utf-8", errors="replace")
        elif isinstance(msg_chunks[0], str):
            msg = msg_chunks[0]
    return {
        "message": msg,
        "payload": [c for c in fields.get(10, []) if isinstance(c, bytes)]
    }


def parse_stream_item(buffer: bytes) -> dict:
    fields = read_fields(buffer)
    stream_id_chunk = fields.get(1, [None])[0]
    stream_id = None
    if isinstance(stream_id_chunk, int):
        stream_id = str(stream_id_chunk)
    elif isinstance(stream_id_chunk, bytes):
        if len(stream_id_chunk) <= 8:
            stream_id = str(read_varint(stream_id_chunk, 0)[0])
        else:
            stream_id = stream_id_chunk.decode("utf-8", errors="replace")

    name = ""
    name_chunk = fields.get(3, [None])[0]
    if isinstance(name_chunk, bytes):
        name = name_chunk.decode("utf-8", errors="replace")

    url = ""
    url_chunk = fields.get(4, [None])[0]
    if isinstance(url_chunk, bytes):
        url = url_chunk.decode("utf-8", errors="replace")

    site_type_chunk = fields.get(9, [None])[0]
    site_type = None
    if isinstance(site_type_chunk, int):
        site_type = site_type_chunk
    elif isinstance(site_type_chunk, bytes):
        site_type = read_varint(site_type_chunk, 0)[0]

    return {
        "streamId": stream_id,
        "name": name,
        "url": url,
        "siteType": site_type
    }


def parse_stream_detail(buffer: bytes) -> dict:
    env = parse_api_envelope(buffer)
    if not env["payload"]:
        return {}
    fields = read_fields(env["payload"][0])
    stream_buffer = fields.get(2, [None])[0] or fields.get(1, [None])[0] or env["payload"][0]
    if isinstance(stream_buffer, bytes):
        return parse_stream_item(stream_buffer)
    return {}


def parse_fctv33_target(url: str, **kwargs) -> tuple[str, str, int, int]:
    """
    Parses matchId, streamId, sportType, siteType from various URL formats or parameters.
    Supported inputs:
      - fctv33://{matchId}/{streamId}?sportType={sportType}&siteType={siteType}
      - https://fctv33.stream/match/{matchId}/stream/{streamId}
      - https://fctv33.stream/{matchId}/{streamId}
      - https://www.fctv33hd.rest/live/detail?matchId={matchId}&streamId={streamId}
      - Query params or kwargs: matchId, streamId, sportType, siteType
      - Formats like '{matchId}:{streamId}' or '{matchId}/{streamId}'
    """
    match_id = kwargs.get("matchId") or kwargs.get("match_id")
    stream_id = kwargs.get("streamId") or kwargs.get("stream_id")
    sport_type = kwargs.get("sportType") or kwargs.get("sport_type")
    site_type = kwargs.get("siteType") or kwargs.get("site_type")

    raw = str(url or "").strip()

    if "?" in raw:
        path_part, query_part = raw.split("?", 1)
        qs = urllib.parse.parse_qs(query_part)
        if not match_id and ("matchId" in qs or "match_id" in qs):
            match_id = (qs.get("matchId") or qs.get("match_id"))[0]
        if not stream_id and ("streamId" in qs or "stream_id" in qs):
            stream_id = (qs.get("streamId") or qs.get("stream_id"))[0]
        if not sport_type:
            st = qs.get("sportType") or qs.get("sport_type")
            if st:
                try:
                    sport_type = int(st[0])
                except ValueError:
                    pass
        if not site_type:
            st = qs.get("siteType") or qs.get("site_type")
            if st:
                try:
                    site_type = int(st[0])
                except ValueError:
                    pass
        raw = path_part

    if not match_id or not stream_id:
        m = re.search(r'match[/_-](\d+)[/_-]stream[/_-](\d+)', raw, re.IGNORECASE)
        if m:
            match_id, stream_id = m.group(1), m.group(2)
        else:
            m = re.search(r'(?:fctv33://|fctv://|https?://[^/]+/)(?:match/)?(\d+)/(?:stream/)?(\d+)', raw, re.IGNORECASE)
            if m:
                match_id, stream_id = m.group(1), m.group(2)
            else:
                m = re.search(r'^(\d+)[:/](\d+)$', raw)
                if m:
                    match_id, stream_id = m.group(1), m.group(2)
                else:
                    qs = urllib.parse.parse_qs(raw)
                    if not match_id and ("matchId" in qs or "match_id" in qs):
                        match_id = (qs.get("matchId") or qs.get("match_id"))[0]
                    if not stream_id and ("streamId" in qs or "stream_id" in qs):
                        stream_id = (qs.get("streamId") or qs.get("stream_id"))[0]

    try:
        final_sport_type = int(sport_type) if sport_type is not None else 1
    except (ValueError, TypeError):
        final_sport_type = 1

    try:
        final_site_type = int(site_type) if site_type is not None else 2001
    except (ValueError, TypeError):
        final_site_type = 2001

    return str(match_id or "").strip(), str(stream_id or "").strip(), final_sport_type, final_site_type


class Fctv33Extractor(BaseExtractor):
    """
    Native FCTV33 stream extractor for EasyProxy.
    Fetches real-time rb-session and signed stream tokens directly from the FCTV33 data API
    using EasyProxy's egress IP, bypassing CDN IP-binding blocks (487 / 471).
    """

    def __init__(self, request_headers: dict = None, proxies: list = None):
        super().__init__(request_headers or {}, proxies=proxies, extractor_name="fctv33")
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "*/*",
        }
        self.data_api_base = os.getenv("FCTV_DATA_API", DEFAULT_DATA_API_BASE)
        self.player_referer = DEFAULT_PLAYER_REFERER
        self.stream_digit = DEFAULT_STREAM_DIGIT
        self.geo = {"country": "IT", "continent": "EU"}
        self._params_ts = 0
        self._init_lock = asyncio.Lock()
        self.mediaflow_endpoint = "hls_proxy"

    async def _refresh_params_if_needed(self):
        now = time.time()
        if now - self._params_ts < 3600:
            return

        async with self._init_lock:
            if now - self._params_ts < 3600:
                return
            try:
                session = await self._get_session(self.data_api_base)
                cfg_url = f"{self.data_api_base}/api/common/params"
                headers = {
                    "User-Agent": self.base_headers["User-Agent"],
                    "Referer": f"{SITE_URL}/",
                    "Origin": SITE_URL,
                }
                async with session.get(cfg_url, headers=headers, timeout=ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        cfg = json.loads(rot47(text))
                        web_clients = json.loads(cfg.get("common:web:client", "{}"))
                        for digit in ["seth", "trd", "sith", "foth", "fith"]:
                            domain_list = web_clients.get(digit, {}).get("iframePlayerDomains", [])
                            if domain_list:
                                self.stream_digit = digit
                                self.player_referer = f"https://{domain_list[0].rstrip('/')}/"
                                logger.info(f"Fctv33Extractor: Updated stream_digit={digit}, referer={self.player_referer}")
                                break
                        self._params_ts = now
            except Exception as e:
                logger.warning(f"Fctv33Extractor: Failed to refresh player params ({e}), using defaults")
                self._params_ts = now - 3300  # retry sooner on failure

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        match_id, stream_id, sport_type, site_type = parse_fctv33_target(url, **kwargs)
        if not match_id or not stream_id:
            raise ExtractorError(
                f"FCTV33: Missing matchId or streamId (parsed matchId='{match_id}', streamId='{stream_id}') from URL: {url}"
            )

        await self._refresh_params_if_needed()

        data_api = self.data_api_base
        session = await self._get_session(data_api)

        endpoint = f"{data_api}/api/stream/detail"
        params = {
            "streamId": stream_id,
            "matchId": match_id,
            "sportType": str(sport_type),
            "siteType": str(site_type),
            "digit": self.stream_digit,
            "country": self.geo.get("country", "IT"),
            "continent": self.geo.get("continent", "EU"),
        }
        headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": self.player_referer,
            "Origin": self.player_referer.rstrip("/"),
            "Accept": "*/*",
        }

        try:
            async with session.get(endpoint, params=params, headers=headers, timeout=ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise ExtractorError(f"FCTV33 API returned HTTP {resp.status}")

                session_token = resp.headers.get("rb-session")
                if not session_token:
                    raise ExtractorError("FCTV33 API did not return rb-session header")

                content = await resp.read()
                env = parse_api_envelope(content)
                if env.get("message") != "Success":
                    raise ExtractorError(f"FCTV33 API error message: {env.get('message')}")

                detail = parse_stream_detail(content)
                obfuscated_url = detail.get("url")
                if not obfuscated_url:
                    raise ExtractorError("FCTV33 API returned empty stream URL")

                signed_url = build_signed_stream_url(obfuscated_url, session_token)
                logger.info(f"Fctv33Extractor: Resolved stream for match={match_id} stream={stream_id} -> {signed_url[:70]}...")

                return {
                    "destination_url": signed_url,
                    "request_headers": {
                        "User-Agent": self.base_headers["User-Agent"],
                        "Referer": self.player_referer,
                        "Origin": self.player_referer.rstrip("/"),
                    },
                    "mediaflow_endpoint": "hls_proxy",
                }
        except ExtractorError:
            raise
        except Exception as e:
            logger.error(f"Fctv33Extractor error: {e}", exc_info=True)
            raise ExtractorError(f"FCTV33 extraction failed: {e}")
