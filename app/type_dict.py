# -*- coding: utf-8 -*-
"""
Type & runtime-safe helpers for payloads.
Compatible with Python 3.9.18 (stdlib only).

Goals:
- TypedDict interface tetap ada (mudah dipakai untuk payload JSON/dict).
- Runtime validation kuat + normalisasi agar aman di production.
- Error message jelas: field mana yang salah/missing dan kenapa.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Optional, TypedDict, Union, cast

__all__ = [
    "PayloadValidationError",
    "PaymentItem",
    "PackageToBuy",
    "UserToken",
    "UserProfile",
    "validate_payment_item",
    "create_payment_item",
    "validate_user_token",
    "validate_package_to_buy",
    "validate_user_profile",
]

# =============================================================================
# ERRORS
# =============================================================================


class PayloadValidationError(ValueError):
    """Raised when a payload is invalid or cannot be normalized safely."""


def _fail(type_name: str, message: str, field: Optional[str] = None) -> "None":
    if field:
        raise PayloadValidationError(f"{type_name}.{field}: {message}")
    raise PayloadValidationError(f"{type_name}: {message}")


def _require_mapping(data: Any, type_name: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        _fail(type_name, f"must be a mapping/dict, got {type(data).__name__}")
    return cast(Mapping[str, Any], data)


def _require_keys(data: Mapping[str, Any], type_name: str, required: set) -> None:
    missing = required - set(data.keys())
    if missing:
        _fail(type_name, f"missing required keys: {sorted(missing)}")


# =============================================================================
# TYPE DEFINITIONS (Interface)
# =============================================================================

class PaymentItem(TypedDict, total=False):
    """
    Struktur item untuk pembayaran/checkout.
    Note: total=False artinya boleh tidak lengkap, tapi validator kita tetap
    enforce field wajib agar aman saat runtime.
    """
    item_code: str
    product_type: str
    item_price: int
    item_name: str
    tax: int
    token_confirmation: Optional[str]


class PackageToBuy(TypedDict):
    """Payload minimal untuk membeli sebuah paket."""
    family_code: str
    is_enterprise: bool
    variant_name: str
    order: int


class UserToken(TypedDict):
    """Struktur token autentikasi lengkap."""
    access_token: str
    refresh_token: str
    id_token: str
    token_type: str
    expires_in: int
    scope: str


class UserProfile(TypedDict, total=False):
    """Profil pengguna (boleh tidak lengkap)."""
    number: str
    subscriber_id: str
    subscription_type: str
    balance: int
    balance_expired_at: int
    point_info: str


# =============================================================================
# COERCION HELPERS (Internal)
# =============================================================================

_Intish = Union[int, float, str]


def _to_str(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    try:
        s = str(value)
    except Exception:
        return default
    return s


def _clean_number_string(s: str) -> str:
    # Remove spaces and underscores commonly used in numeric formatting.
    return s.strip().replace(" ", "").replace("_", "")


def _to_int(value: Any, *, default: int = 0) -> int:
    """
    Konversi aman ke int.

    Accept:
    - int/bool/float
    - str dengan format:
      "1234", "1,234", "1.234", "1.234,56", "1,234.56", "  12_345 "
    Behavior:
    - Jika ada desimal, dibulatkan ke bawah (truncate) dengan aman.
    - Jika parsing gagal -> default.
    """
    if value is None:
        return default

    # bool adalah subclass int; kita perlakukan eksplisit agar tidak “aneh”.
    if isinstance(value, bool):
        return int(value)

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        # float -> int truncates toward zero (ok untuk harga integer-ish)
        try:
            return int(value)
        except Exception:
            return default

    if isinstance(value, str):
        s = _clean_number_string(value)
        if not s:
            return default

        # Handle leading sign
        sign = 1
        if s[0] == "-":
            sign = -1
            s = s[1:]
        elif s[0] == "+":
            s = s[1:]

        # Now decide separators.
        # Case A: both '.' and ',' exist.
        if "." in s and "," in s:
            # Heuristic:
            # - If last ',' occurs after last '.', assume EU/ID: '.' thousand, ',' decimal -> "1.234,56"
            # - Else assume US: ',' thousand, '.' decimal -> "1,234.56"
            last_dot = s.rfind(".")
            last_comma = s.rfind(",")
            if last_comma > last_dot:
                # "1.234,56" -> thousands '.' removed, decimal part after ',' dropped
                s2 = s.replace(".", "")
                s2 = s2.split(",", 1)[0]
            else:
                # "1,234.56" -> thousands ',' removed, decimal part after '.' dropped
                s2 = s.replace(",", "")
                s2 = s2.split(".", 1)[0]
            return sign * int(s2) if s2.isdigit() else default

        # Case B: only comma exists
        if "," in s:
            # Treat comma as thousand separator (common in US data exports).
            # If it looks like decimal (one comma and <=2 digits after), drop decimals.
            if s.count(",") == 1:
                left, right = s.split(",", 1)
                if right.isdigit() and len(right) in (1, 2) and left.replace(".", "").isdigit():
                    # "1234,5" / "1234,56" -> drop decimals
                    s2 = left.replace(".", "")
                    return sign * int(s2) if s2.isdigit() else default
            s2 = s.replace(",", "")
            return sign * int(s2) if s2.isdigit() else default

        # Case C: only dot exists
        if "." in s:
            # If it looks like thousand separator groups (e.g. 1.234.567), remove dots.
            parts = s.split(".")
            if all(p.isdigit() for p in parts) and len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
                s2 = "".join(parts)
                return sign * int(s2) if s2.isdigit() else default
            # Otherwise treat as decimal separator and drop decimals.
            left = s.split(".", 1)[0]
            return sign * int(left) if left.isdigit() else default

        # Case D: plain digits
        return sign * int(s) if s.isdigit() else default

    # Unknown type
    return default


def _to_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = _to_str(value, default="")
    s = s.strip()
    return s if s else None


def _non_empty_str(value: Any, *, type_name: str, field: str) -> str:
    s = _to_str(value, default="").strip()
    if not s:
        _fail(type_name, "must be a non-empty string", field=field)
    return s


# =============================================================================
# VALIDATORS (Public API)
# =============================================================================

def validate_payment_item(data: Mapping[str, Any]) -> PaymentItem:
    """
    Validasi + normalisasi PaymentItem.
    Wajib: item_code, item_price, item_name
    """
    type_name = "PaymentItem"
    m = _require_mapping(data, type_name)
    _require_keys(m, type_name, {"item_code", "item_price", "item_name"})

    item_code = _non_empty_str(m.get("item_code"), type_name=type_name, field="item_code")
    item_name = _non_empty_str(m.get("item_name"), type_name=type_name, field="item_name")

    item_price = _to_int(m.get("item_price"), default=-1)
    if item_price < 0:
        _fail(type_name, "must be a non-negative integer", field="item_price")

    tax = _to_int(m.get("tax"), default=0)
    if tax < 0:
        _fail(type_name, "must be a non-negative integer", field="tax")

    product_type = _to_str(m.get("product_type"), default="").strip()
    token_confirmation = _to_optional_str(m.get("token_confirmation"))

    return cast(PaymentItem, {
        "item_code": item_code,
        "product_type": product_type,
        "item_price": item_price,
        "item_name": item_name,
        "tax": tax,
        "token_confirmation": token_confirmation,
    })


def create_payment_item(
    code: str,
    price: _Intish,
    name: str,
    token: Optional[str] = None,
    p_type: str = "",
    tax: _Intish = 0,
) -> PaymentItem:
    """
    Factory helper untuk membuat PaymentItem yang sudah normalized & tervalidasi.
    (Jadi outputnya selalu clean)
    """
    payload: Dict[str, Any] = {
        "item_code": code,
        "item_price": price,
        "item_name": name,
        "product_type": p_type,
        "tax": tax,
        "token_confirmation": token,
    }
    return validate_payment_item(payload)


def validate_user_token(data: Mapping[str, Any]) -> UserToken:
    """Validasi & normalisasi struktur token."""
    type_name = "UserToken"
    m = _require_mapping(data, type_name)
    _require_keys(m, type_name, {"access_token", "refresh_token", "id_token", "token_type", "expires_in", "scope"})

    access_token = _non_empty_str(m.get("access_token"), type_name=type_name, field="access_token")
    refresh_token = _non_empty_str(m.get("refresh_token"), type_name=type_name, field="refresh_token")
    id_token = _non_empty_str(m.get("id_token"), type_name=type_name, field="id_token")
    token_type = _to_str(m.get("token_type"), default="Bearer").strip() or "Bearer"
    scope = _to_str(m.get("scope"), default="").strip()

    expires_in = _to_int(m.get("expires_in"), default=0)
    if expires_in <= 0:
        _fail(type_name, "must be a positive integer", field="expires_in")

    return cast(UserToken, {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "token_type": token_type,
        "expires_in": expires_in,
        "scope": scope,
    })


def validate_package_to_buy(data: Mapping[str, Any]) -> PackageToBuy:
    """Validasi payload pembelian paket."""
    type_name = "PackageToBuy"
    m = _require_mapping(data, type_name)
    _require_keys(m, type_name, {"family_code", "variant_name", "order"})

    family_code = _non_empty_str(m.get("family_code"), type_name=type_name, field="family_code")
    variant_name = _non_empty_str(m.get("variant_name"), type_name=type_name, field="variant_name")

    order = _to_int(m.get("order"), default=1)
    if order < 1:
        order = 1

    # is_enterprise optional -> default False
    is_enterprise = bool(m.get("is_enterprise", False))

    return cast(PackageToBuy, {
        "family_code": family_code,
        "variant_name": variant_name,
        "order": order,
        "is_enterprise": is_enterprise,
    })


def validate_user_profile(data: Mapping[str, Any]) -> UserProfile:
    """
    Validasi ringan + normalisasi untuk UserProfile (karena total=False).
    Tidak ada required keys; semua field jika ada akan dinormalisasi.
    """
    type_name = "UserProfile"
    m = _require_mapping(data, type_name)

    out: Dict[str, Any] = {}

    if "number" in m:
        out["number"] = _to_str(m.get("number"), default="").strip()
    if "subscriber_id" in m:
        out["subscriber_id"] = _to_str(m.get("subscriber_id"), default="").strip()
    if "subscription_type" in m:
        out["subscription_type"] = _to_str(m.get("subscription_type"), default="").strip()

    if "balance" in m:
        out["balance"] = _to_int(m.get("balance"), default=0)
    if "balance_expired_at" in m:
        out["balance_expired_at"] = _to_int(m.get("balance_expired_at"), default=0)

    if "point_info" in m:
        out["point_info"] = _to_str(m.get("point_info"), default="").strip()

    return cast(UserProfile, out)


# =============================================================================
# OPTIONAL QUICK SELF-TEST
# =============================================================================

if __name__ == "__main__":
    # Minimal sanity checks (tidak wajib dipakai)
    print("Sanity test: create_payment_item")
    pi = create_payment_item("ABC", "1.234,56", "Paket Data", token="tok", p_type="DATA", tax="10")
    print(pi)

    print("Sanity test: validate_user_token")
    ut = validate_user_token({
        "access_token": "a",
        "refresh_token": "r",
        "id_token": "i",
        "token_type": "Bearer",
        "expires_in": "3600",
        "scope": "openid profile",
    })
    print(ut)

    print("Sanity test: validate_package_to_buy")
    pb = validate_package_to_buy({"family_code": "FAM", "variant_name": "V1", "order": 0})
    print(pb)