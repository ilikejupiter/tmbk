# -*- coding: utf-8 -*-
"""
registration.py - Registration / Info Client (XL) - Modern & Stable
Compatible: Python 3.9.18

Upgrades:
- Centralized _send() wrapper with strict response normalization.
- Lightweight input validation (non-empty checks), but stays backward compatible.
- Optional timeout support (works even if send_api_request has no timeout kwarg).
- Never crashes callers: always returns dict with keys: status, message, data.
- Keeps legacy global functions: validate_puk(), dukcapil().

Note:
- These endpoints do NOT require id_token (kept as "" like legacy).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.client.engsel import send_api_request

logger = logging.getLogger(__name__)

ApiResponse = Dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_str(v: Any) -> str:
    try:
        return "" if v is None else str(v)
    except Exception:
        return ""


def _safe_response(*, status: str, message: str = "", data: Any = None) -> ApiResponse:
    return {"status": status, "message": message, "data": data}


def _coerce_response(res: Any) -> ApiResponse:
    """
    Normalize API response so callers always get a safe shape.
    """
    if not isinstance(res, dict):
        return _safe_response(status="Failed", message="Invalid response type", data=None)

    status = _as_str(res.get("status")).strip()
    if not status:
        status = "SUCCESS" if res.get("data") is not None else "Failed"

    message = _as_str(res.get("message")).strip()
    data = res.get("data", None)
    return _safe_response(status=status, message=message, data=data)


def _non_empty(value: Any, name: str) -> Optional[str]:
    s = _as_str(value).strip()
    if not s:
        return None
    return s


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class RegistrationClient:
    """
    Client untuk endpoint registration/info yang tidak butuh id_token.

    `timeout` optional: dipakai hanya jika send_api_request mendukung.
    """

    def __init__(self, api_key: str, *, timeout: Optional[int] = None):
        self.api_key = _as_str(api_key).strip()
        self.timeout = timeout

    def _send(self, path: str, payload: Dict[str, Any], id_token: str = "") -> ApiResponse:
        """
        Wrapper request:
        - Add default fields (is_enterprise/lang)
        - Catch errors -> return safe response
        """
        if not self.api_key:
            return _safe_response(status="Failed", message="API key is empty", data=None)

        final_payload: Dict[str, Any] = {"is_enterprise": False, "lang": "en"}
        final_payload.update(payload or {})

        try:
            try:
                res = send_api_request(self.api_key, path, final_payload, id_token, "POST", timeout=self.timeout)
            except TypeError:
                # Compatibility with older signatures
                res = send_api_request(self.api_key, path, final_payload, id_token, "POST")
        except Exception as e:
            logger.error("Request error on %s: %s", path, e)
            return _safe_response(status="Failed", message=str(e), data=None)

        return _coerce_response(res)

    # -------------------------------------------------------------- public ----
    def validate_puk(self, msisdn: str, puk: str) -> ApiResponse:
        """
        Validate PUK for given msisdn.
        Legacy behavior: no id_token required.
        """
        m = _non_empty(msisdn, "msisdn")
        p = _non_empty(puk, "puk")
        if not m:
            return _safe_response(status="Failed", message="msisdn is missing", data=None)
        if not p:
            return _safe_response(status="Failed", message="puk is missing", data=None)

        payload = {"msisdn": m, "puk": p, "is_enc": False}
        return self._send("api/v8/infos/validate-puk", payload, id_token="")

    def dukcapil(self, msisdn: str, kk: str, nik: str) -> ApiResponse:
        """
        Dukcapil verification for registration.
        Legacy behavior: no id_token required.
        """
        m = _non_empty(msisdn, "msisdn")
        k = _non_empty(kk, "kk")
        n = _non_empty(nik, "nik")

        if not m:
            return _safe_response(status="Failed", message="msisdn is missing", data=None)
        if not k:
            return _safe_response(status="Failed", message="kk is missing", data=None)
        if not n:
            return _safe_response(status="Failed", message="nik is missing", data=None)

        payload = {"msisdn": m, "kk": k, "nik": n}
        return self._send("api/v8/auth/regist/dukcapil", payload, id_token="")


# ---------------------------------------------------------------------------
# Compatibility layer (drop-in global functions)
# ---------------------------------------------------------------------------

def validate_puk(api_key: str, msisdn: str, puk: str) -> dict:
    return RegistrationClient(api_key).validate_puk(msisdn, puk)

def dukcapil(api_key: str, msisdn: str, kk: str, nik: str) -> dict:
    return RegistrationClient(api_key).dukcapil(msisdn, kk, nik)