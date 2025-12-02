# -*- coding: utf-8 -*-
"""
Circle (Family Hub) Client - Modern & Stable
Python 3.9.18 compatible

Fokus perbaikan:
- Client class dengan wrapper request terpusat
- Validasi token/id_token sebelum request (hindari crash & request invalid)
- Enkripsi MSISDN dibungkus _encrypt() dengan fallback signature (2 arg / 1 arg)
- Return shape konsisten: {"status": str, "message": str, "data": Any}
- Backward compatible: fungsi global tetap tersedia
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from app.client.engsel import send_api_request
from app.client.encrypt import encrypt_circle_msisdn

logger = logging.getLogger(__name__)

TokenDict = Dict[str, str]
ApiResponse = Dict[str, Any]

__all__ = [
    "CircleClient",
    "get_group_data",
    "get_group_members",
    "validate_circle_member",
    "invite_circle_member",
    "remove_circle_member",
    "accept_circle_invitation",
    "create_circle",
    "spending_tracker",
    "get_bonus_data",
]


# =============================================================================
# Internal helpers
# =============================================================================

def _as_str(value: Any) -> str:
    try:
        return "" if value is None else str(value)
    except Exception:
        return ""


def _norm_token(tokens: Mapping[str, Any], key: str) -> str:
    v = tokens.get(key)
    s = _as_str(v).strip()
    return s


def _safe_response(
    *,
    status: str,
    message: str = "",
    data: Any = None,
) -> ApiResponse:
    return {"status": status, "message": message, "data": data}


def _coerce_api_response(res: Any) -> ApiResponse:
    """
    Normalize API response supaya caller selalu dapat dict shape aman.
    """
    if not isinstance(res, dict):
        return _safe_response(status="Failed", message="Invalid response type", data=None)

    # Some backends use uppercase, some not. Keep as-is but ensure keys exist.
    status = _as_str(res.get("status") or "").strip() or ("SUCCESS" if res.get("data") is not None else "Failed")
    message = _as_str(res.get("message") or "").strip()
    data = res.get("data", None)
    return _safe_response(status=status, message=message, data=data)


# =============================================================================
# Circle client
# =============================================================================

class CircleClient:
    """
    Client untuk fitur Family Circle / Family Hub.
    """

    def __init__(self, api_key: str):
        self.api_key = _as_str(api_key).strip()

    # ------------------------------------------------------------------ helpers

    def _encrypt(self, msisdn: str) -> str:
        """
        Wrapper enkripsi MSISDN khusus Circle.
        Beberapa implementasi `encrypt_circle_msisdn` menerima (api_key, msisdn),
        ada juga yang hanya (msisdn). Kita coba keduanya biar kompatibel.
        """
        msisdn_s = _as_str(msisdn).strip()
        if not msisdn_s:
            return ""

        try:
            # Coba versi (api_key, msisdn)
            return encrypt_circle_msisdn(self.api_key, msisdn_s)  # type: ignore[misc]
        except TypeError:
            # Fallback ke versi (msisdn)
            try:
                return encrypt_circle_msisdn(msisdn_s)  # type: ignore[call-arg]
            except Exception as e:
                logger.error("Encrypt MSISDN failed: %s", e)
                return ""
        except Exception as e:
            logger.error("Encrypt MSISDN failed: %s", e)
            return ""

    def _send_request(
        self,
        path: str,
        payload: Dict[str, Any],
        id_token: str,
        description: str = "",
        *,
        method: str = "POST",
    ) -> ApiResponse:
        """
        Internal wrapper request.
        Menyisipkan default payload (lang, is_enterprise).
        Mengembalikan dict dengan kunci minimal: status, message, data.
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
            res = send_api_request(self.api_key, path, final_payload, idt, method)
        except Exception as e:
            logger.error("Error executing %s: %s", path, e)
            return _safe_response(status="Failed", message=str(e), data=None)

        return _coerce_api_response(res)

    # ------------------------------------------------------------------ features

    def get_group_data(self, tokens: Mapping[str, Any]) -> ApiResponse:
        """Mengambil status dan detail grup."""
        return self._send_request(
            path="family-hub/api/v8/groups/status",
            payload={},
            id_token=_norm_token(tokens, "id_token"),
            description="Fetching group detail...",
        )

    def get_group_members(self, tokens: Mapping[str, Any], group_id: str) -> ApiResponse:
        """Mengambil daftar anggota grup."""
        gid = _as_str(group_id).strip()
        if not gid:
            return _safe_response(status="Failed", message="group_id is missing", data=None)

        return self._send_request(
            path="family-hub/api/v8/members/info",
            payload={"group_id": gid},
            id_token=_norm_token(tokens, "id_token"),
            description="Fetching group members...",
        )

    def validate_member(self, tokens: Mapping[str, Any], msisdn: str) -> ApiResponse:
        """Validasi apakah nomor eligible masuk circle."""
        enc = self._encrypt(msisdn)
        if not enc:
            return _safe_response(status="Failed", message="Encryption failed", data=None)

        return self._send_request(
            path="family-hub/api/v8/members/validate",
            payload={"msisdn": enc},
            id_token=_norm_token(tokens, "id_token"),
            description=f"Validating member {msisdn}...",
        )

    def invite_member(
        self,
        tokens: Mapping[str, Any],
        msisdn: str,
        name: str,
        group_id: str,
        member_id_parent: str,
    ) -> ApiResponse:
        """Mengundang anggota baru ke circle."""
        enc = self._encrypt(msisdn)
        if not enc:
            return _safe_response(status="Failed", message="Encryption failed", data=None)

        gid = _as_str(group_id).strip()
        parent = _as_str(member_id_parent).strip()
        nm = _as_str(name).strip()

        if not gid:
            return _safe_response(status="Failed", message="group_id is missing", data=None)
        if not parent:
            return _safe_response(status="Failed", message="member_id_parent is missing", data=None)
        if not nm:
            nm = _as_str(msisdn).strip() or "Member"

        payload = {
            "access_token": _norm_token(tokens, "access_token"),
            "group_id": gid,
            "members": [{"msisdn": enc, "name": nm}],
            "member_id_parent": parent,
        }

        return self._send_request(
            path="family-hub/api/v8/members/invite",
            payload=payload,
            id_token=_norm_token(tokens, "id_token"),
            description=f"Inviting {msisdn} to circle...",
        )

    def remove_member(
        self,
        tokens: Mapping[str, Any],
        member_id: str,
        group_id: str,
        member_id_parent: str,
        is_last_member: bool = False,
    ) -> ApiResponse:
        """Menghapus anggota dari circle."""
        mid = _as_str(member_id).strip()
        gid = _as_str(group_id).strip()
        parent = _as_str(member_id_parent).strip()

        if not mid:
            return _safe_response(status="Failed", message="member_id is missing", data=None)
        if not gid:
            return _safe_response(status="Failed", message="group_id is missing", data=None)
        if not parent:
            return _safe_response(status="Failed", message="member_id_parent is missing", data=None)

        payload = {
            "member_id": mid,
            "group_id": gid,
            "is_last_member": bool(is_last_member),
            "member_id_parent": parent,
        }

        return self._send_request(
            path="family-hub/api/v8/members/remove",
            payload=payload,
            id_token=_norm_token(tokens, "id_token"),
            description=f"Removing member ID {mid}...",
        )

    def accept_invitation(self, tokens: Mapping[str, Any], group_id: str, member_id: str) -> ApiResponse:
        """Menerima undangan masuk circle."""
        gid = _as_str(group_id).strip()
        mid = _as_str(member_id).strip()
        if not gid:
            return _safe_response(status="Failed", message="group_id is missing", data=None)
        if not mid:
            return _safe_response(status="Failed", message="member_id is missing", data=None)

        payload = {
            "access_token": _norm_token(tokens, "access_token"),
            "group_id": gid,
            "member_id": mid,
        }
        return self._send_request(
            path="family-hub/api/v8/groups/accept-invitation",
            payload=payload,
            id_token=_norm_token(tokens, "id_token"),
            description=f"Accepting invitation for group {gid}...",
        )

    def create_circle(
        self,
        tokens: Mapping[str, Any],
        parent_name: str,
        group_name: str,
        member_msisdn: str,
        member_name: str,
    ) -> ApiResponse:
        """Membuat Circle baru."""
        enc = self._encrypt(member_msisdn)
        if not enc:
            return _safe_response(status="Failed", message="Encryption failed", data=None)

        pn = _as_str(parent_name).strip() or "Parent"
        gn = _as_str(group_name).strip() or "My Circle"
        mn = _as_str(member_name).strip() or (_as_str(member_msisdn).strip() or "Member")

        payload = {
            "access_token": _norm_token(tokens, "access_token"),
            "parent_name": pn,
            "group_name": gn,
            "members": [{"msisdn": enc, "name": mn}],
        }

        return self._send_request(
            path="family-hub/api/v8/groups/create",
            payload=payload,
            id_token=_norm_token(tokens, "id_token"),
            description=f"Creating Circle '{gn}' with member {member_msisdn}...",
        )

    def get_spending_tracker(self, tokens: Mapping[str, Any], parent_subs_id: str, family_id: str) -> ApiResponse:
        """Mengambil data spending tracker (Gamification)."""
        pid = _as_str(parent_subs_id).strip()
        fid = _as_str(family_id).strip()

        if not pid:
            return _safe_response(status="Failed", message="parent_subs_id is missing", data=None)
        if not fid:
            return _safe_response(status="Failed", message="family_id is missing", data=None)

        return self._send_request(
            path="gamification/api/v8/family-hub/spending-tracker",
            payload={"parent_subs_id": pid, "family_id": fid},
            id_token=_norm_token(tokens, "id_token"),
            description="Fetching spending tracker...",
        )

    def get_bonus_data(self, tokens: Mapping[str, Any], parent_subs_id: str, family_id: str) -> ApiResponse:
        """Mengambil list bonus kuota/hadiah."""
        pid = _as_str(parent_subs_id).strip()
        fid = _as_str(family_id).strip()

        if not pid:
            return _safe_response(status="Failed", message="parent_subs_id is missing", data=None)
        if not fid:
            return _safe_response(status="Failed", message="family_id is missing", data=None)

        return self._send_request(
            path="gamification/api/v8/family-hub/bonus/list",
            payload={"parent_subs_id": pid, "family_id": fid},
            id_token=_norm_token(tokens, "id_token"),
            description="Fetching bonus data...",
        )


# =============================================================================
# COMPATIBILITY LAYER (Backward Compatibility)
# =============================================================================

def get_group_data(api_key: str, tokens: dict) -> dict:
    return CircleClient(api_key).get_group_data(tokens)

def get_group_members(api_key: str, tokens: dict, group_id: str) -> dict:
    return CircleClient(api_key).get_group_members(tokens, group_id)

def validate_circle_member(api_key: str, tokens: dict, msisdn: str) -> dict:
    return CircleClient(api_key).validate_member(tokens, msisdn)

def invite_circle_member(
    api_key: str, tokens: dict, msisdn: str, name: str, group_id: str, member_id_parent: str
) -> dict:
    return CircleClient(api_key).invite_member(tokens, msisdn, name, group_id, member_id_parent)

def remove_circle_member(
    api_key: str, tokens: dict, member_id: str, group_id: str, member_id_parent: str, is_last_member: bool = False
) -> dict:
    return CircleClient(api_key).remove_member(tokens, member_id, group_id, member_id_parent, is_last_member)

def accept_circle_invitation(api_key: str, tokens: dict, group_id: str, member_id: str) -> dict:
    return CircleClient(api_key).accept_invitation(tokens, group_id, member_id)

def create_circle(
    api_key: str, tokens: dict, parent_name: str, group_name: str, member_msisdn: str, member_name: str
) -> dict:
    return CircleClient(api_key).create_circle(tokens, parent_name, group_name, member_msisdn, member_name)

def spending_tracker(api_key: str, tokens: dict, parent_subs_id: str, family_id: str) -> dict:
    return CircleClient(api_key).get_spending_tracker(tokens, parent_subs_id, family_id)

def get_bonus_data(api_key: str, tokens: dict, parent_subs_id: str, family_id: str) -> dict:
    return CircleClient(api_key).get_bonus_data(tokens, parent_subs_id, family_id)