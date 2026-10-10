from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
DEFAULT_CACHE_TTL = 30
MAX_LISTEN_CACHE_ENTRIES = 256


class VergeOSClientError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class CachedResponse:
    body: object
    expires_at: float


class VergeOSClient:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        insecure_tls: bool = False,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._insecure_tls = insecure_tls
        self._cache_ttl = max(0, cache_ttl_seconds)
        self._cache: dict[str, CachedResponse] = {}
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._api_url,
                timeout=httpx.Timeout(connect=10, read=DEFAULT_TIMEOUT, write=DEFAULT_TIMEOUT, pool=10),
                verify=not self._insecure_tls,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                    "User-Agent": "hermeshq-cloud-broker/1.0",
                },
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def invalidate_cache(self, prefix: str = "") -> None:
        if not prefix:
            self._cache.clear()
            return
        for key in [k for k in self._cache if k.startswith(prefix)]:
            self._cache.pop(key, None)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        use_cache: bool = False,
    ) -> object:
        cache_key = ""
        if use_cache and method.upper() == "GET":
            normalized_params = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
            cache_key = hashlib.sha256(f"{path}?{normalized_params}".encode()).hexdigest()
            cached = self._cache.get(cache_key)
            if cached is not None and cached.expires_at > time.monotonic():
                return cached.body
        client = await self._ensure_client()
        try:
            response = await client.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise VergeOSClientError("VergeOS API is unreachable") from exc
        if response.status_code == 429:
            raise VergeOSClientError("VergeOS API rate limit reached — retry later", status_code=429)
        if response.status_code == 401:
            raise VergeOSClientError("Tenant credential rejected by VergeOS (invalid or expired key)", status_code=401)
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str((response.json() or {}).get("err") or response.text or "")[:300]
            except (json.JSONDecodeError, ValueError):
                detail = (response.text or "")[:300]
            raise VergeOSClientError(
                f"VergeOS API error ({response.status_code}): {detail}",
                status_code=response.status_code,
            )
        try:
            body = response.json() if response.content else None
        except (json.JSONDecodeError, ValueError):
            body = {"raw": response.text[:2000]}
        if cache_key:
            if len(self._cache) >= MAX_LISTEN_CACHE_ENTRIES:
                oldest = min(self._cache, key=lambda k: self._cache[k].expires_at)
                self._cache.pop(oldest, None)
            self._cache[cache_key] = CachedResponse(body=body, expires_at=time.monotonic() + self._cache_ttl)
        return body

    async def get(self, path: str, params: dict | None = None) -> object:
        return await self._request("GET", path, params=params, use_cache=True)

    async def post(self, path: str, json_body: dict | None = None) -> object:
        result = await self._request("POST", path, json_body=json_body)
        self.invalidate_cache()
        return result

    async def put(self, path: str, json_body: dict | None = None) -> object:
        result = await self._request("PUT", path, json_body=json_body)
        self.invalidate_cache()
        return result

    async def delete(self, path: str) -> object:
        result = await self._request("DELETE", path)
        self.invalidate_cache()
        return result

    async def health(self) -> dict:
        try:
            body = await self._request("GET", "/api/v4/system", params={"fields": "summary"}, use_cache=False)
        except VergeOSClientError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "system": body}

    @staticmethod
    def build_list_params(
        *,
        fields: str = "most",
        filter_expr: str | None = None,
        sort: str | None = None,
        limit: int | None = None,
    ) -> dict:
        params: dict = {"fields": fields}
        if filter_expr:
            params["filter"] = filter_expr
        if sort:
            params["sort"] = sort
        if limit is not None and limit > 0:
            params["limit"] = str(limit)
        return params

    @staticmethod
    def quote_filter(value: str) -> str:
        return quote(value)
