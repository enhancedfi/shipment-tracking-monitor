"""Small HTTP helper around urllib. Tests pass a fake transport."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

class CarrierError(RuntimeError):
    pass


class CarrierAuthError(CarrierError):
    pass


class CarrierRateLimitError(CarrierError):
    pass


class CarrierTemporaryError(CarrierError):
    pass


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def decode_json(status: int, raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise CarrierTemporaryError(f"carrier returned HTTP {status} with a non-JSON body") from error
    if not isinstance(parsed, dict):
        raise CarrierTemporaryError(f"carrier returned HTTP {status} with an unexpected JSON type")
    return parsed


def raise_for_status(status: int, body: dict, *, auth: bool = False) -> None:
    if status == 401 or (auth and status in {400, 403}):
        raise CarrierAuthError(f"carrier rejected credentials (HTTP {status})")
    if status == 429:
        raise CarrierRateLimitError("carrier asked us to slow down (HTTP 429)")
    if status >= 500:
        raise CarrierTemporaryError(f"carrier unavailable (HTTP {status})")
    if status >= 400:
        raise CarrierError(f"carrier request failed (HTTP {status})")
