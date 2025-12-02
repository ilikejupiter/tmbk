# -*- coding: utf-8 -*-
"""
purchase.py - purchase & redeem flows (modernized, stable)
Python 3.9.18 compatible.

Key fixes:
- PaymentItem is a TypedDict -> must be a dict, not PaymentItem(...)
- More robust token refresh: uses AuthInstance.get_active_tokens() (auto-renew)
- Safer decoy loading + validation
- Cleaner retry & amount adjustment parsing
- Resilient pause import (app.menus.util OR app.util), non-TTY safe
"""

from __future__ import annotations

import re
import time
from random import randint
from typing import Any, Dict, List, Optional, Tuple

from app.client.engsel import get_family, get_package_details, get_package
from app.client.purchase.redeem import settlement_bounty, settlement_loyalty  # penting untuk redeem
from app.client.purchase.balance import settlement_balance
from app.service.auth import AuthInstance
from app.service.decoy import DecoyInstance

# PaymentItem is TypedDict -> at runtime it's a dict-like payload
from app.type_dict import PaymentItem

# =============================================================================
# Resilient pause import
# =============================================================================
def _fallback_pause(msg: str = "Tekan [Enter] untuk melanjutkan...") -> None:
    import sys
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            print("")
            input(msg)
    except Exception:
        pass

try:
    from app.menus.util import pause  # type: ignore
except Exception:
    try:
        from app.util import pause  # type: ignore
    except Exception:
        pause = _fallback_pause  # type: ignore

# =============================================================================
# KONFIGURASI AUTO REFRESH (Menu 7 - Pembelian Biasa)
# Gunakan monotonic agar stabil terhadap perubahan jam sistem
# =============================================================================
REFRESH_INTERVAL_SEC = 20   # refresh tokens setiap 20 detik (best-effort)
REFRESH_BATCH_COUNT = 5     # refresh tokens setiap 5 aksi (best-effort)

# =============================================================================
# Internal helpers
# =============================================================================

_RE_AMOUNT_TOTAL = re.compile(r"(?:Bizz-err\.Amount\.Total).*?=\s*(\d+)", re.IGNORECASE)


def _now_m() -> float:
    return time.monotonic()


def _safe_str(v: Any) -> str:
    try:
        return "" if v is None else str(v)
    except Exception:
        return ""


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if isinstance(v, bool):
            return int(v)
        return int(str(v).strip())
    except Exception:
        return default


def _get_tokens() -> Dict[str, Any]:
    """
    Use AuthInstance for safe, auto-refresh behavior.
    """
    tokens = AuthInstance.get_active_tokens() or {}
    return tokens if isinstance(tokens, dict) else {}


def _get_api_key() -> str:
    return _safe_str(getattr(AuthInstance, "api_key", "")).strip()


def _build_payment_item_from_detail(detail: Dict[str, Any], *, name_prefix_rand: bool = True) -> PaymentItem:
    """
    Convert package detail response -> PaymentItem dict (TypedDict-compatible).
    """
    pkg_opt = detail.get("package_option") or {}
    code = _safe_str(pkg_opt.get("package_option_code")).strip()
    price = _safe_int(pkg_opt.get("price"), 0)
    name = _safe_str(pkg_opt.get("name")).strip() or "Unknown Item"
    tc = detail.get("token_confirmation")

    display_name = f"{randint(1000, 9999)} {name}" if name_prefix_rand else name

    return {
        "item_code": code,
        "product_type": "",
        "item_price": price,
        "item_name": display_name,
        "tax": 0,
        "token_confirmation": tc if tc is not None else None,
    }


def _confirm(prompt: str) -> bool:
    try:
        ans = input(prompt).strip().lower()
        return ans == "y"
    except Exception:
        return False


def _load_decoy_detail(api_key: str, tokens: Dict[str, Any], decoy_type: str = "balance") -> Optional[Dict[str, Any]]:
    """
    Load decoy config via DecoyInstance then fetch package detail.
    Returns package detail dict or None.
    """
    decoy = DecoyInstance.get_decoy(decoy_type)
    if not isinstance(decoy, dict):
        return None

    option_code = _safe_str(decoy.get("option_code")).strip()
    if not option_code:
        return None

    detail = get_package(api_key, tokens, option_code)
    return detail if isinstance(detail, dict) and detail else None


def _maybe_refresh_session(
    *,
    use_decoy: bool,
    api_key: str,
    last_refresh_m: float,
    batch_counter: int,
    decoy_detail: Optional[Dict[str, Any]],
) -> Tuple[float, int, Dict[str, Any], Optional[Dict[str, Any]]]:
    """
    Best-effort refresh tokens + decoy detail periodically.
    Returns updated (last_refresh_m, batch_counter, tokens, decoy_detail).
    """
    tokens = _get_tokens()
    now = _now_m()
    if batch_counter >= REFRESH_BATCH_COUNT or (now - last_refresh_m) >= REFRESH_INTERVAL_SEC:
        print(f"\n🔄 Refreshing session & decoy token...")
        # tokens were refreshed via AuthInstance already (auto renew)
        if use_decoy:
            try:
                fresh = _load_decoy_detail(api_key, tokens, "balance")
                if fresh:
                    decoy_detail = fresh
                    print("   ✅ Decoy token refreshed!")
            except Exception as e:
                print(f"   ⚠️ Error refreshing decoy: {e}")

        last_refresh_m = _now_m()
        batch_counter = 0
        print("-" * 55)

    return last_refresh_m, batch_counter, tokens, decoy_detail


def _try_settlement_balance_with_adjustment(
    api_key: str,
    tokens: Dict[str, Any],
    payment_items: List[PaymentItem],
    payment_for: str,
    overwrite_amount: int,
    token_confirmation_idx: int,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Attempt settlement_balance, if backend complains about total amount mismatch,
    parse the correct amount and retry once.

    Returns (response_dict_or_none, error_message)
    """
    res = None
    error_msg = ""

    try:
        res = settlement_balance(
            api_key, tokens, payment_items, payment_for, False,
            overwrite_amount=overwrite_amount,
            token_confirmation_idx=token_confirmation_idx,
        )
    except Exception as e:
        return None, f"Exception while creating order: {e}"

    if not res:
        return None, "No response"

    if _safe_str(res.get("status")).strip().upper() == "SUCCESS":
        return res, ""

    error_msg = _safe_str(res.get("message")).strip()
    if "Bizz-err.Amount.Total" in error_msg:
        m = _RE_AMOUNT_TOTAL.search(error_msg)
        if m:
            valid_amount = _safe_int(m.group(1), 0)
            if valid_amount > 0:
                print(f"   ⚠️ Price adjustment from backend: {valid_amount}")
                try:
                    res2 = settlement_balance(
                        api_key, tokens, payment_items, "SHARE_PACKAGE", False,
                        overwrite_amount=valid_amount,
                        token_confirmation_idx=-1,
                    )
                    if res2 and _safe_str(res2.get("status")).strip().upper() == "SUCCESS":
                        return res2, ""
                    return res2, _safe_str(res2.get("message")).strip() if isinstance(res2, dict) else "Retry failed"
                except Exception as e:
                    return None, f"Exception on retry: {e}"

    return res, error_msg or "Failed"


# =============================================================================
# 1. PURCHASE BY FAMILY (MENU 7)
# =============================================================================
def purchase_by_family(
    family_code: str,
    use_decoy: bool,
    pause_on_success: bool = True,
    delay_seconds: int = 0,
    start_from_option: int = 1,
):
    api_key = _get_api_key()
    tokens: Dict[str, Any] = _get_tokens()

    if not api_key:
        print("❌ API key kosong. Set env API_KEY/XL_API_KEY/MYXL_API_KEY.")
        pause()
        return False

    if not tokens:
        print("❌ Session invalid. Login dulu.")
        pause()
        return False

    decoy_package_detail: Optional[Dict[str, Any]] = None

    # --- 1) INITIAL LOAD DECOY ---
    if use_decoy:
        print("⏳ Memuat data Decoy awal...")
        decoy_package_detail = _load_decoy_detail(api_key, tokens, "balance")
        if not decoy_package_detail:
            print("❌ Gagal memuat detail paket Decoy.")
            pause()
            return False

        try:
            balance_threshold = _safe_int(decoy_package_detail["package_option"]["price"], 0)
        except Exception:
            balance_threshold = 0

        if balance_threshold > 0:
            print(f"⚠️  Pastikan sisa pulsa KURANG DARI Rp{balance_threshold}!!!")
        if not _confirm("Yakin ingin melanjutkan? (y/n): "):
            print("Pembelian dibatalkan.")
            pause()
            return None

    # --- 2) LOAD FAMILY DATA ---
    family_data = get_family(api_key, tokens, family_code)
    if not family_data:
        print(f"❌ Failed to get family data for code: {family_code}.")
        pause()
        return None

    package_family = family_data.get("package_family") or {}
    family_name = _safe_str(package_family.get("name")).strip() or family_code
    variants = family_data.get("package_variants") or []

    print("-------------------------------------------------------")
    successful_purchases: List[str] = []

    # count packages for progress
    packages_count = 0
    for variant in variants:
        try:
            packages_count += len(variant.get("package_options") or [])
        except Exception:
            pass

    purchase_count = 0
    start_buying = start_from_option <= 1

    # stable refresh vars
    last_refresh_m = _now_m()
    batch_counter = 0

    for variant in variants:
        variant_name = _safe_str(variant.get("name")).strip() or "Variant"
        variant_code = _safe_str(variant.get("package_variant_code")).strip()
        options = variant.get("package_options") or []

        for option in options:
            option_order = _safe_int(option.get("order"), 0)
            if not start_buying and option_order == start_from_option:
                start_buying = True
            if not start_buying:
                print(f"Skipping option {option_order}. {_safe_str(option.get('name'))}")
                continue

            # refresh session periodically
            last_refresh_m, batch_counter, tokens, decoy_package_detail = _maybe_refresh_session(
                use_decoy=use_decoy,
                api_key=api_key,
                last_refresh_m=last_refresh_m,
                batch_counter=batch_counter,
                decoy_detail=decoy_package_detail,
            )

            option_name = _safe_str(option.get("name")).strip() or "Option"
            option_price = option.get("price")

            purchase_count += 1
            batch_counter += 1

            print(f"Purchase {purchase_count} of {packages_count}...")
            print(f"Target: {variant_name} - {option_order}. {option_name} - {option_price}")

            payment_items: List[PaymentItem] = []
            error_msg = ""

            try:
                # detail target
                target_package_detail = get_package_details(
                    api_key, tokens, family_code,
                    variant_code, option_order, None, None,
                )

                if not isinstance(target_package_detail, dict) or not target_package_detail:
                    print("   ❌ Gagal mengambil detail target package.")
                    continue

                payment_items.append(_build_payment_item_from_detail(target_package_detail))

                # detail decoy (optional)
                if use_decoy and decoy_package_detail:
                    payment_items.append(_build_payment_item_from_detail(decoy_package_detail))

                # overwrite amount
                overwrite_amount = _safe_int(target_package_detail.get("package_option", {}).get("price"), 0)
                if use_decoy and decoy_package_detail:
                    overwrite_amount += _safe_int(decoy_package_detail.get("package_option", {}).get("price"), 0)

                # settlement
                res, error_msg = _try_settlement_balance_with_adjustment(
                    api_key=api_key,
                    tokens=tokens,
                    payment_items=payment_items,
                    payment_for="頂",  # keep legacy token/label
                    overwrite_amount=overwrite_amount,
                    token_confirmation_idx=1 if use_decoy else 0,
                )

                if not error_msg:
                    successful_purchases.append(f"{variant_name}|{option_order}. {option_name} - {option_price}")
                    print("   ✅ Purchase successful!")
                    if pause_on_success:
                        pause()
                else:
                    print(f"   ❌ Failed: {error_msg}")

            except Exception as e:
                print(f"Exception occurred while processing: {e}")
                continue

            print("-------------------------------------------------------")

            should_delay = (error_msg == "") or ("Failed call ipaas purchase" in error_msg)
            if delay_seconds > 0 and should_delay:
                print(f"Waiting for {delay_seconds} seconds...")
                time.sleep(delay_seconds)

    print(f"Family: {family_name}\nSuccessful: {len(successful_purchases)}")
    if successful_purchases:
        print("-" * 55)
        print("Successful purchases:")
        for purchase in successful_purchases:
            print(f"- {purchase}")

    print("-" * 55)
    pause()
    return True


# =============================================================================
# 2. REDEEM LOOP (MENU 15)
# =============================================================================
def redeem_n_times(
    n: int,
    option_code: str,
    redeem_type: str = "BOUNTY",  # "BOUNTY" (Voucher) atau "LOYALTY" (Poin)
    delay_seconds: int = 0
):
    """
    Redeem berkali-kali dengan refresh token setiap putaran.
    Token redeem bersifat One-Time-Use, jadi detail paket harus di-fetch tiap loop.
    """
    api_key = _get_api_key()
    if not api_key:
        print("❌ API key kosong. Set env API_KEY/XL_API_KEY/MYXL_API_KEY.")
        pause()
        return False

    redeem_type_u = _safe_str(redeem_type).strip().upper() or "BOUNTY"
    if redeem_type_u not in ("BOUNTY", "LOYALTY"):
        redeem_type_u = "BOUNTY"

    print(f"\n🚀 Memulai Redeem Loop ({redeem_type_u}) sebanyak {n}x")
    print(f"📦 Target: {option_code}")
    print("-------------------------------------------------------")

    success_count = 0

    for i in range(int(n)):
        print(f"🔄 Redeem {i + 1} of {n}...")

        tokens = _get_tokens()
        if not tokens:
            print("❌ Session Invalid.")
            break

        try:
            pkg_detail = get_package(api_key, tokens, option_code)
            if not isinstance(pkg_detail, dict) or not pkg_detail:
                print("❌ Gagal mengambil detail paket/voucher.")
                time.sleep(1)
                continue

            pkg_opt = pkg_detail.get("package_option") or {}
            token_conf = pkg_detail.get("token_confirmation")
            ts_sign = pkg_detail.get("timestamp")
            price = _safe_int(pkg_opt.get("price"), 0)
            name = _safe_str(pkg_opt.get("name")).strip() or "Unknown Item"

            if not token_conf:
                print("❌ Token Confirmation habis/invalid.")
                time.sleep(1)
                continue

            print(f"   Item: {name} | Price: {price}")

            res = None
            if redeem_type_u == "BOUNTY":
                res = settlement_bounty(
                    api_key=api_key, tokens=tokens, token_confirmation=token_conf,
                    ts_to_sign=ts_sign, payment_target=option_code,
                    price=price, item_name=name
                )
            else:
                res = settlement_loyalty(
                    api_key=api_key, tokens=tokens, token_confirmation=token_conf,
                    ts_to_sign=ts_sign, payment_target=option_code,
                    price=price
                )

            if res and _safe_str(res.get("status")).strip().upper() == "SUCCESS":
                print("   ✅ REDEEM SUCCESS!")
                success_count += 1
            else:
                msg = _safe_str(res.get("message")) if isinstance(res, dict) else "No Response"
                print(f"   ❌ GAGAL: {msg}")

        except Exception as e:
            print(f"   ⚠️ Exception: {e}")

        print("-------------------------------------------------------")

        if delay_seconds > 0 and i < n - 1:
            print(f"⏳ Waiting {delay_seconds}s...")
            time.sleep(delay_seconds)

    print(f"\n🏁 Selesai! Berhasil: {success_count}/{n}")
    pause()
    return True


# =============================================================================
# 3. STANDARD LOOP FUNCTIONS (kept for backward compatibility)
# =============================================================================

def purchase_n_times(
    n: int,
    family_code: str,
    variant_code: str,
    option_order: int,
    use_decoy: bool,
    delay_seconds: int = 0,
    pause_on_success: bool = False,
    token_confirmation_idx: int = 0,
):
    api_key = _get_api_key()
    tokens: Dict[str, Any] = _get_tokens()

    if not api_key:
        print("❌ API key kosong.")
        pause()
        return False
    if not tokens:
        print("❌ Session invalid.")
        pause()
        return False

    decoy_package_detail: Optional[Dict[str, Any]] = None
    if use_decoy:
        decoy_package_detail = _load_decoy_detail(api_key, tokens, "balance")
        if not decoy_package_detail:
            print("Failed to load decoy package details.")
            pause()
            return False

        balance_threshold = _safe_int(decoy_package_detail.get("package_option", {}).get("price"), 0)
        if balance_threshold > 0:
            print(f"Pastikan sisa balance KURANG DARI Rp{balance_threshold}!!!")
        if not _confirm("Lanjut? (y/n): "):
            return None

    family_data = get_family(api_key, tokens, family_code)
    if not family_data:
        print("Failed to get family data.")
        pause()
        return None

    variants = family_data.get("package_variants") or []
    target_variant = next((v for v in variants if _safe_str(v.get("package_variant_code")) == variant_code), None)
    if not target_variant:
        return None

    options = target_variant.get("package_options") or []
    target_option = next((o for o in options if _safe_int(o.get("order"), -1) == int(option_order)), None)
    if not target_option:
        return None

    print("-------------------------------------------------------")
    successful_purchases: List[str] = []

    for i in range(int(n)):
        print(f"Purchase {i + 1} of {n}...")
        tokens = _get_tokens()
        if not tokens:
            print("❌ Session invalid.")
            break

        payment_items: List[PaymentItem] = []

        try:
            target_package_detail = get_package_details(
                api_key, tokens, family_code,
                _safe_str(target_variant.get("package_variant_code")),
                _safe_int(target_option.get("order"), 0),
                None, None,
            )
            if not isinstance(target_package_detail, dict) or not target_package_detail:
                print("Failed to fetch target detail.")
                continue

            payment_items.append(_build_payment_item_from_detail(target_package_detail))

            if use_decoy and decoy_package_detail:
                payment_items.append(_build_payment_item_from_detail(decoy_package_detail))

            overwrite_amount = _safe_int(target_package_detail.get("package_option", {}).get("price"), 0)
            if use_decoy and decoy_package_detail:
                overwrite_amount += _safe_int(decoy_package_detail.get("package_option", {}).get("price"), 0)

            res = settlement_balance(
                api_key, tokens, payment_items, "💰", False,
                overwrite_amount=overwrite_amount, token_confirmation_idx=token_confirmation_idx
            )

            if res and _safe_str(res.get("status")).strip().upper() == "SUCCESS":
                successful_purchases.append(f"Purchase {i + 1}")
                print("Purchase successful!")
                if pause_on_success:
                    pause()
            else:
                msg = _safe_str(res.get("message")) if isinstance(res, dict) else "No Response"
                print(f"Failed: {msg}")

        except Exception as e:
            print(f"Exception: {e}")

        print("-------------------------------------------------------")
        if delay_seconds > 0 and i < n - 1:
            time.sleep(delay_seconds)

    print(f"Total successful purchases {len(successful_purchases)}/{n}")
    pause()
    return True


def purchase_n_times_by_option_code(
    n: int,
    option_code: str,
    use_decoy: bool,
    delay_seconds: int = 0,
    pause_on_success: bool = False,
    token_confirmation_idx: int = 0,
):
    api_key = _get_api_key()
    tokens: Dict[str, Any] = _get_tokens()

    if not api_key:
        print("❌ API key kosong.")
        pause()
        return False
    if not tokens:
        print("❌ Session invalid.")
        pause()
        return False

    decoy_package_detail: Optional[Dict[str, Any]] = None
    if use_decoy:
        decoy_package_detail = _load_decoy_detail(api_key, tokens, "balance")
        if not decoy_package_detail:
            return False
        if not _confirm("Lanjut? (y/n): "):
            return None

    print("-------------------------------------------------------")
    successful_purchases: List[str] = []

    for i in range(int(n)):
        print(f"Purchase {i + 1} of {n}...")
        tokens = _get_tokens()
        if not tokens:
            print("❌ Session invalid.")
            break

        payment_items: List[PaymentItem] = []

        try:
            target_package_detail = get_package(api_key, tokens, option_code)
            if not isinstance(target_package_detail, dict) or not target_package_detail:
                print("Failed to fetch package detail.")
                continue

            payment_items.append(_build_payment_item_from_detail(target_package_detail))

            if use_decoy and decoy_package_detail:
                payment_items.append(_build_payment_item_from_detail(decoy_package_detail))

            overwrite_amount = _safe_int(target_package_detail.get("package_option", {}).get("price"), 0)
            if use_decoy and decoy_package_detail:
                overwrite_amount += _safe_int(decoy_package_detail.get("package_option", {}).get("price"), 0)

            res = settlement_balance(
                api_key, tokens, payment_items, "💰", False,
                overwrite_amount=overwrite_amount, token_confirmation_idx=token_confirmation_idx
            )

            if res and _safe_str(res.get("status")).strip().upper() == "SUCCESS":
                successful_purchases.append(f"Purchase {i + 1}")
                print("Purchase successful!")
                if pause_on_success:
                    pause()
            else:
                msg = _safe_str(res.get("message")) if isinstance(res, dict) else "No Response"
                print(f"Failed: {msg}")

        except Exception as e:
            print(f"Error: {e}")

        print("-------------------------------------------------------")
        if delay_seconds > 0 and i < n - 1:
            time.sleep(delay_seconds)

    print(f"Total successful purchases {len(successful_purchases)}/{n}")
    pause()
    return True