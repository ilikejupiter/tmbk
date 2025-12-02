# -*- coding: utf-8 -*-
"""
Family Plan Client - Modern & Stable
Compatible: Python 3.9.18

Improvements:
- Centralized request wrapper with strict normalization of responses.
- Validates api_key and id_token before calling API.
- Optional timeout support (works whether send_api_request has timeout param or not).
- Resilient import of format_quota_byte (multiple fallbacks).
- Backward compatible: keeps global functions (get_family_data, validate_msisdn, change_member, remove_member, set_quota_limit).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from app.client.engsel import send_api_request

logger = logging.getLogger(__name__)

TokenDict = Dict[str, str]
ApiResponse = Dict[str, Any]

# ---------------------------------------------------------------------------
# Resilient import for quota formatting
# ---------------------------------------------------------------------------
try:
    # original legacy path
    from app.menus.util import format_quota_byte  # type: ignore
except Exception:
    try:
        # newer/common path in your refactors
        from app.util import format_quota_byte  # type: ignore
    except Exception:
        # last-resort fallback (simple)
        def format_quota_byte(size: Any) -> str:  # type: ignore
            try:
                n = float(size)
            except Exception:
                n = 0.0
            units = ["B", "KB", "MB", "GB", "TB", "PB"]
            idx = 0
            while n >= 1024.0 and idx < len(units) - 1:
                n /= 1024.0
                idx += 1
            return f"{int(n)} B" if idx == 0 else f"{n:.2f} {units[idx]}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _as_str(v: Any) -> str:
    try:
        return "" if v is None else str(v)
    except Exception:
        return ""


def _safe_response(*, status: str, message: str = "", data: Any = None) -> ApiResponse:
    return {"status": status, "message": message, "data": data}


def _normalize_tokens(tokens: Mapping[str, Any]) -> TokenDict:
    return {
        "access_token": _as_str(tokens.get("access_token")).strip(),
        "id_token": _as_str(tokens.get("id_token")).strip(),
        "refresh_token": _as_str(tokens.get("refresh_token")).strip(),
        "token_type": _as_str(tokens.get("token_type")).strip(),
        "scope": _as_str(tokens.get("scope")).strip(),
        "expires_in": _as_str(tokens.get("expires_in")).strip(),
    }


def _coerce_api_response(res: Any) -> ApiResponse:
    if not isinstance(res, dict):
        return _safe_response(status="Failed", message="Invalid response type", data=None)

    status = _as_str(res.get("status")).strip()
    if not status:
        status = "SUCCESS" if res.get("data") is not None else "Failed"

    message = _as_str(res.get("message")).strip()
    data = res.get("data", None)

    return _safe_response(status=status, message=message, data=data)


def _to_int(v: Any, default: int = 0) -> int:
    try:
        if isinstance(v, bool):
            return int(v)
        return int(str(v).strip())
    except Exception:
        return default


# =============================================================================
# Client
# =============================================================================

class FamilyPlanClient:
    """
    Client untuk manajemen XL Family Plan:
    - Membaca data grup
    - Validasi calon member
    - Mengubah/menghapus member
    - Mengatur batas kuota per member
    """

    def __init__(self, api_key: str, *, timeout: Optional[int] = None):
        self.api_key = _as_str(api_key).strip()
        self.timeout = timeout  # optional, only used if send_api_request supports it

    # ------------------------------------------------------------------ core --
    def _send_request(
        self,
        path: str,
        payload: Optional[Dict[str, Any]],
        id_token: str,
        description: str = "",
        *,
        method: str = "POST",
    ) -> ApiResponse:
        """
        Wrapper internal:
        - Tambah default payload (is_enterprise/lang)
        - Validasi api_key + id_token
        - Try/except → kembalikan structure aman
        - Normalisasi response supaya konsisten
        """
        if not self.api_key:
            return _safe_response(status="Failed", message="API key is empty", data=None)

        idt = _as_str(id_token).strip()
        if not idt:
            return _safe_response(status="Failed", message="id_token is missing", data=None)

        final_payload: Dict[str, Any] = {"is_enterprise": False, "lang": "en"}
        if payload:
            final_payload.update(payload)

        if description:
            logger.info(description)

        try:
            # Compatibility: some send_api_request implementations accept timeout kwarg
            try:
                res = send_api_request(self.api_key, path, final_payload, idt, method, timeout=self.timeout)
            except TypeError:
                res = send_api_request(self.api_key, path, final_payload, idt, method)
        except Exception as e:
            logger.error("Error executing %s: %s", path, e)
            return _safe_response(status="Failed", message=str(e), data=None)

        return _coerce_api_response(res)

    # -------------------------------------------------------------- public ----
    def get_family_data(self, tokens: Mapping[str, Any]) -> ApiResponse:
        """Mengambil data dashboard family plan (slot & member)."""
        t = _normalize_tokens(tokens)
        return self._send_request(
            path="sharings/api/v8/family-plan/member-info",
            payload={"group_id": 0},
            id_token=t.get("id_token", ""),
            description="Fetching family plan data...",
        )

    def validate_msisdn(self, tokens: Mapping[str, Any], msisdn: str) -> ApiResponse:
        """Validasi eligibility MSISDN."""
        t = _normalize_tokens(tokens)
        m = _as_str(msisdn).strip()
        if not m:
            return _safe_response(status="Failed", message="msisdn is missing", data=None)

        payload = {
            "msisdn": m,
            "with_bizon": True,
            "with_family_plan": True,
            "with_optimus": True,
            "with_regist_status": True,
            "with_enterprise": True,
        }
        return self._send_request(
            path="api/v8/auth/check-dukcapil",
            payload=payload,
            id_token=t.get("id_token", ""),
            description=f"Validating MSISDN candidate {m}...",
        )

    def change_member(
        self,
        tokens: Mapping[str, Any],
        parent_alias: str,
        alias: str,
        slot_id: int,
        family_member_id: str,
        new_msisdn: str,
    ) -> ApiResponse:
        """Menambahkan atau mengganti member pada slot tertentu."""
        t = _normalize_tokens(tokens)
        slot = _to_int(slot_id, default=-1)
        if slot < 0:
            return _safe_response(status="Failed", message="slot_id must be a non-negative integer", data=None)

        fid = _as_str(family_member_id).strip()
        nm = _as_str(new_msisdn).strip()
        if not fid:
            return _safe_response(status="Failed", message="family_member_id is missing", data=None)
        if not nm:
            return _safe_response(status="Failed", message="new_msisdn is missing", data=None)

        payload = {
            "parent_alias": _as_str(parent_alias).strip(),
            "slot_id": slot,
            "alias": _as_str(alias).strip(),
            "msisdn": nm,
            "family_member_id": fid,
        }
        return self._send_request(
            path="sharings/api/v8/family-plan/change-member",
            payload=payload,
            id_token=t.get("id_token", ""),
            description=f"Assigning {nm} to slot {slot}...",
        )

    def remove_member(self, tokens: Mapping[str, Any], family_member_id: str) -> ApiResponse:
        """Menghapus member dari Family Plan."""
        t = _normalize_tokens(tokens)
        fid = _as_str(family_member_id).strip()
        if not fid:
            return _safe_response(status="Failed", message="family_member_id is missing", data=None)

        return self._send_request(
            path="sharings/api/v8/family-plan/remove-member",
            payload={"family_member_id": fid},
            id_token=t.get("id_token", ""),
            description=f"Removing family member ID {fid}...",
        )

    def set_quota_limit(
        self,
        tokens: Mapping[str, Any],
        original_allocation: int,
        new_allocation: int,
        family_member_id: str,
    ) -> ApiResponse:
        """Mengatur batas kuota member (byte)."""
        t = _normalize_tokens(tokens)
        fid = _as_str(family_member_id).strip()
        if not fid:
            return _safe_response(status="Failed", message="family_member_id is missing", data=None)

        orig = _to_int(original_allocation, default=-1)
        new = _to_int(new_allocation, default=-1)
        if orig < 0 or new < 0:
            return _safe_response(status="Failed", message="allocations must be non-negative integers", data=None)

        formatted_quota = format_quota_byte(new)

        payload = {
            "member_allocations": [
                {
                    "family_member_id": fid,
                    "original_allocation": orig,
                    "new_allocation": new,
                    # Fields for API compatibility
                    "new_text_allocation": 0,
                    "original_text_allocation": 0,
                    "original_voice_allocation": 0,
                    "new_voice_allocation": 0,
                    "message": "",
                    "status": "",
                }
            ]
        }

        return self._send_request(
            path="sharings/api/v8/family-plan/allocate-quota",
            payload=payload,
            id_token=t.get("id_token", ""),
            description=f"Setting quota limit for {fid} to {formatted_quota}...",
        )


# =============================================================================
# COMPAT LAYER (drop-in global functions)
# =============================================================================

def get_family_data(api_key: str, tokens: dict) -> dict:
    return FamilyPlanClient(api_key).get_family_data(tokens)

def validate_msisdn(api_key: str, tokens: dict, msisdn: str) -> dict:
    return FamilyPlanClient(api_key).validate_msisdn(tokens, msisdn)

def change_member(
    api_key: str,
    tokens: dict,
    parent_alias: str,
    alias: str,
    slot_id: int,
    family_member_id: str,
    new_msisdn: str,
) -> dict:
    return FamilyPlanClient(api_key).change_member(tokens, parent_alias, alias, slot_id, family_member_id, new_msisdn)

def remove_member(api_key: str, tokens: dict, family_member_id: str) -> dict:
    return FamilyPlanClient(api_key).remove_member(tokens, family_member_id)

def set_quota_limit(
    api_key: str,
    tokens: dict,
    original_allocation: int,
    new_allocation: int,
    family_member_id: str,
) -> dict:
    return FamilyPlanClient(api_key).set_quota_limit(tokens, original_allocation, new_allocation, family_member_id)