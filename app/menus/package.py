# -*- coding: utf-8 -*-
"""
package.py - Package Menu (modernized & hardened)
Python 3.9.18 compatible.

Highlights:
- No logging.basicConfig side-effects on import (library-friendly).
- TypedDict PaymentItem is a *dict* at runtime -> fixed all PaymentItem(...) misuse.
- Resilient util imports (clear_screen/pause/display_html/format_quota_byte).
- Safer dynamic pricing retry (Bizz-err.Amount.Total) using regex.
- Unsubscribe arg order fixed to match engsel.py wrapper signature.
- Reduced crash probability: centralized safe wrappers + consistent guards.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import datetime
from random import randint
from typing import Any, Dict, List, Optional, Tuple

from app.service.auth import AuthInstance
from app.client.engsel import (
    get_family,
    get_package,
    get_addons,
    get_package_details,
    send_api_request,
    unsubscribe,
)
from app.service.bookmark import BookmarkInstance
from app.client.purchase.redeem import settlement_bounty, settlement_loyalty, bounty_allotment
from app.client.purchase.qris import show_qris_payment
from app.client.purchase.ewallet import show_multipayment
from app.client.purchase.balance import settlement_balance
from app.menus.purchase import purchase_n_times_by_option_code
from app.service.decoy import DecoyInstance
from app.type_dict import PaymentItem

logger = logging.getLogger(__name__)

WIDTH = 60

_RE_AMOUNT_TOTAL = re.compile(r"(?:Bizz-err\.Amount\.Total).*?=\s*(\d+)", re.IGNORECASE)


# =============================================================================
# Resilient util imports
# =============================================================================

def _fallback_clear_screen() -> None:
    import sys
    try:
        if sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
    except Exception:
        pass


def _fallback_pause(msg: str = "Tekan [Enter] untuk melanjutkan...") -> None:
    import sys
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            print("")
            input(msg)
    except Exception:
        pass


def _fallback_display_html(html: str) -> str:
    # Minimal: strip-ish display; keep as-is if already plain.
    return str(html or "")


def _fallback_format_quota_byte(size: Any) -> str:
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


try:
    from app.menus.util import clear_screen, pause, display_html, format_quota_byte  # type: ignore
except Exception:
    try:
        from app.util import clear_screen, pause, display_html, format_quota_byte  # type: ignore
    except Exception:
        clear_screen = _fallback_clear_screen  # type: ignore
        pause = _fallback_pause  # type: ignore
        display_html = _fallback_display_html  # type: ignore
        format_quota_byte = _fallback_format_quota_byte  # type: ignore


# =============================================================================
# Small helpers
# =============================================================================

def _as_str(v: Any) -> str:
    try:
        return "" if v is None else str(v)
    except Exception:
        return ""


def _as_int(v: Any, default: int = 0) -> int:
    try:
        if isinstance(v, bool):
            return int(v)
        return int(str(v).strip())
    except Exception:
        return default


def _print_kv(key: str, value: Any) -> None:
    print(f"{key:<25}: {value}")


def _get_api_key_tokens() -> Tuple[str, Dict[str, Any]]:
    api_key = _as_str(getattr(AuthInstance, "api_key", "")).strip()
    tokens = AuthInstance.get_active_tokens() or {}
    if not isinstance(tokens, dict):
        tokens = {}
    return api_key, tokens


def _build_payment_item_from_package_detail(detail: Dict[str, Any], *, name_prefix_rand: bool = False) -> PaymentItem:
    pkg_opt = detail.get("package_option") or {}
    code = _as_str(pkg_opt.get("package_option_code")).strip()
    price = _as_int(pkg_opt.get("price"), 0)
    name = _as_str(pkg_opt.get("name")).strip() or "Item"

    if name_prefix_rand:
        name = f"{randint(1000, 9999)} {name}"

    # TypedDict => build plain dict (NOT PaymentItem(...))
    return {
        "item_code": code,
        "product_type": "",
        "item_price": price,
        "item_name": name,
        "tax": 0,
        "token_confirmation": detail.get("token_confirmation", ""),
    }


def _extract_valid_amount(msg: str) -> Optional[int]:
    if not msg:
        return None
    m = _RE_AMOUNT_TOTAL.search(msg)
    if not m:
        return None
    try:
        amt = int(m.group(1))
        return amt if amt > 0 else None
    except Exception:
        return None


# =============================================================================
# Decoy helpers
# =============================================================================

def _prepare_decoy_items(main_payment_items: List[PaymentItem], decoy_type: str = "balance") -> Tuple[Optional[List[PaymentItem]], int]:
    """
    Menyiapkan list item pembayaran + decoy item.
    Returns: (new_items, decoy_price)
    """
    try:
        api_key, tokens = _get_api_key_tokens()
        if not api_key or not tokens:
            print("❌ Session/API key invalid. Login dulu.")
            return None, 0

        decoy = DecoyInstance.get_decoy(decoy_type)
        if not isinstance(decoy, dict) or not decoy.get("option_code"):
            print("❌ Konfigurasi decoy tidak valid.")
            return None, 0

        decoy_pkg = get_package(api_key, tokens, _as_str(decoy["option_code"]).strip())
        if not isinstance(decoy_pkg, dict) or not decoy_pkg:
            print("❌ Gagal memuat paket decoy. Pastikan konfigurasi benar.")
            return None, 0

        pkg_opt = decoy_pkg.get("package_option") or {}
        decoy_item: PaymentItem = {
            "item_code": _as_str(pkg_opt.get("package_option_code")).strip(),
            "product_type": "",
            "item_price": _as_int(pkg_opt.get("price"), 0),
            "item_name": _as_str(pkg_opt.get("name") or "Decoy Item").strip() or "Decoy Item",
            "tax": 0,
            "token_confirmation": decoy_pkg.get("token_confirmation", ""),
        }

        new_items = list(main_payment_items)
        new_items.append(decoy_item)
        return new_items, _as_int(pkg_opt.get("price"), 0)

    except Exception as e:
        logger.error("Error preparing decoy: %s", e, exc_info=True)
        return None, 0


# =============================================================================
# Purchase result handler (dynamic pricing retry)
# =============================================================================

def _handle_purchase_response(
    res: Optional[Dict[str, Any]],
    api_key: str,
    tokens: Dict[str, Any],
    items: List[PaymentItem],
    payment_for: str,
    token_confirmation_idx: int = 0,
) -> None:
    if not res:
        print("❌ Tidak ada respon dari server.")
        pause()
        return

    status = _as_str(res.get("status")).strip().upper()
    if status == "SUCCESS":
        print("✅ Pembelian Berhasil!")
        pause()
        return

    msg = _as_str(res.get("message")).strip()
    if "Bizz-err.Amount.Total" in msg:
        valid_amt = _extract_valid_amount(msg)
        if valid_amt:
            print(f"🔄 Auto-adjust harga ke: Rp {valid_amt:,}")
            try:
                res_retry = settlement_balance(
                    api_key,
                    tokens,
                    items,
                    payment_for,
                    False,
                    overwrite_amount=valid_amt,
                    token_confirmation_idx=token_confirmation_idx,
                )
                if res_retry and _as_str(res_retry.get("status")).strip().upper() == "SUCCESS":
                    print("✅ Pembelian Berhasil (Setelah retry)!")
                else:
                    print(f"❌ Masih gagal: {_as_str((res_retry or {}).get('message'))}")
            except Exception as e:
                print(f"❌ Retry error: {e}")
        else:
            print(f"❌ Gagal parsing error amount: {msg}")
    else:
        print(f"❌ Pembelian Gagal: {msg}")
        if "data" in res:
            try:
                print(json.dumps(res["data"], indent=2, ensure_ascii=False))
            except Exception:
                pass

    pause()


def _handle_bomb_purchase(option_code: str) -> None:
    try:
        n = int(input("Jumlah pembelian: ").strip())
        use_decoy = input("Gunakan Decoy? (y/n): ").strip().lower() == "y"
        delay_s = input("Delay (detik, default 0): ").strip()
        delay = int(delay_s) if delay_s.isdigit() else 0
        # token_confirmation_idx=1 agar decoy jadi confirmation (umum untuk mode decoy)
        purchase_n_times_by_option_code(
            n,
            option_code,
            use_decoy,
            delay_seconds=delay,
            pause_on_success=False,
            token_confirmation_idx=1,
        )
    except ValueError:
        print("Input angka tidak valid.")
        pause()
    except Exception as e:
        print(f"Error: {e}")
        pause()


# =============================================================================
# Main Viewer / Purchase Menu
# =============================================================================

def show_package_details(api_key: str, tokens: Dict[str, Any], package_option_code: str, is_enterprise: bool, option_order: int = -1) -> bool:
    """
    Menampilkan detail paket + menu pembelian.
    Return True jika sukses melakukan aksi pembelian; False jika kembali.
    """
    try:
        clear_screen()
        print("⏳ Memuat detail paket...")

        package = get_package(api_key, tokens, package_option_code)
        if not isinstance(package, dict) or not package:
            print("❌ Gagal mengambil data paket (Mungkin paket Bonus/Legacy).")
            pause()
            return False

        pkg_opt = package.get("package_option") or {}
        pkg_fam = package.get("package_family") or {}
        pkg_var = package.get("package_detail_variant") or {}
        pkg_addon = package.get("package_addon") or {}

        price = _as_int(pkg_opt.get("price"), 0)
        validity = _as_str(pkg_opt.get("validity") or "N/A")
        option_name = _as_str(pkg_opt.get("name") or "N/A")
        family_name = _as_str(pkg_fam.get("name") or "N/A")
        variant_name = _as_str(pkg_var.get("name") or "N/A")

        family_code = _as_str(pkg_fam.get("package_family_code") or "N/A")
        parent_code = _as_str(pkg_addon.get("parent_code") or "N/A")

        full_title = f"{family_name} - {variant_name} - {option_name}"

        main_item = _build_payment_item_from_package_detail(
            {
                "package_option": {
                    "package_option_code": package_option_code,
                    "price": price,
                    "name": f"{variant_name} {option_name}".strip(),
                },
                "token_confirmation": package.get("token_confirmation", ""),
            }
        )
        payment_items: List[PaymentItem] = [main_item]
        payment_for = _as_str(pkg_fam.get("payment_for") or "BUY_PACKAGE") or "BUY_PACKAGE"

        clear_screen()
        print("=" * WIDTH)
        print(full_title.center(WIDTH))
        print("=" * WIDTH)

        _print_kv("Harga", f"Rp {price:,}")
        _print_kv("Masa Aktif", validity)
        _print_kv("Tipe Pembayaran", payment_for)
        _print_kv("Plan Type", _as_str(pkg_fam.get("plan_type") or "N/A"))
        print("-" * WIDTH)
        _print_kv("Family Code", family_code)
        _print_kv("Parent Code", parent_code)
        print("-" * WIDTH)

        # Benefits
        benefits = pkg_opt.get("benefits") or []
        if isinstance(benefits, list) and benefits:
            print("KEUNTUNGAN PAKET:")
            for b in benefits:
                if not isinstance(b, dict):
                    continue
                b_name = _as_str(b.get("name") or "Benefit")
                b_type = _as_str(b.get("data_type") or "OTHER").upper()
                b_total = b.get("total", 0)

                name_lower = b_name.lower()
                if b_type == "DATA" or "kuota" in name_lower or "internet" in name_lower:
                    info_str = format_quota_byte(b_total)
                elif b_type == "VOICE":
                    try:
                        info_str = f"{float(b_total) / 60:.1f} Menit"
                    except Exception:
                        info_str = f"{b_total}"
                elif b_type == "TEXT":
                    info_str = f"{b_total} SMS"
                else:
                    if isinstance(b_total, (int, float)) and b_total > 1048576:
                        info_str = format_quota_byte(b_total)
                    else:
                        info_str = f"{b_total}"

                unlimited_tag = " [UNLIMITED]" if b.get("is_unlimited") else ""
                print(f" • {b_name:<25} : {info_str}{unlimited_tag}")

        print("-" * WIDTH)

        # Addons availability hint
        try:
            addons = get_addons(api_key, tokens, package_option_code) or {}
            if isinstance(addons, dict) and (addons.get("bonuses") or addons.get("addons")):
                print(" (Tersedia Bonus/Addons tambahan)")
                print("-" * WIDTH)
        except Exception:
            pass

        # S&K
        tnc_raw = _as_str(pkg_opt.get("tnc") or "")
        tnc = display_html(tnc_raw)
        print("Syarat & Ketentuan:")
        print(tnc if tnc else "(Tidak ada deskripsi.)")
        print("=" * WIDTH)

        while True:
            print("\nMETODE PEMBELIAN:")
            print(" [1] Pulsa (Normal)")
            print(" [2] E-Wallet (DANA, OVO, Shopee, GoPay)")
            print(" [3] QRIS (Scan)")
            print("-" * 20 + " ADVANCED / TRICK " + "-" * 20)
            print(" [4] Pulsa + Decoy (Bypass Limit)")
            print(" [5] Pulsa + Decoy V2 (Ghost Mode)")
            print(" [6] QRIS + Decoy (Custom Amount)")
            print(" [7] QRIS + Decoy V2 (Rp 0)")
            print(" [8] Bom Pembelian (N kali)")

            if option_order != -1:
                print(" [B] Bookmark Paket Ini")

            if payment_for == "REDEEM_VOUCHER":
                print(" [R] Redeem Voucher")
                print(" [S] Kirim Bonus (Gift)")
                print(" [L] Beli dengan Poin")

            print(" [0] Kembali")

            choice = input("\nPilihan >> ").strip().upper()

            try:
                if choice == "0":
                    return False

                if choice == "B" and option_order != -1:
                    ok = BookmarkInstance.add_bookmark(
                        family_code=_as_str(pkg_fam.get("package_family_code") or ""),
                        family_name=family_name,
                        is_enterprise=is_enterprise,
                        variant_name=variant_name,
                        option_name=option_name,
                        order=option_order,
                    )
                    print("✅ Bookmark tersimpan!" if ok else "⚠️ Sudah ada di bookmark.")
                    pause()
                    continue

                if choice == "1":
                    settlement_balance(api_key, tokens, payment_items, payment_for, True)
                    pause()
                    return True
                if choice == "2":
                    show_multipayment(api_key, tokens, payment_items, payment_for, True)
                    pause()
                    return True
                if choice == "3":
                    show_qris_payment(api_key, tokens, payment_items, payment_for, True)
                    pause()
                    return True

                # --- DECOY START ---
                if choice == "4":
                    items, decoy_price = _prepare_decoy_items(payment_items, "balance")
                    if not items:
                        continue
                    total_overwrite = price + decoy_price
                    res = settlement_balance(api_key, tokens, items, payment_for, False, overwrite_amount=total_overwrite)
                    _handle_purchase_response(res, api_key, tokens, items, payment_for)
                    return True

                if choice == "5":  # Ghost Mode
                    items, decoy_price = _prepare_decoy_items(payment_items, "balance")
                    if not items:
                        print("❌ Gagal menyiapkan Item Decoy.")
                        pause()
                        continue

                    total_overwrite = price + decoy_price
                    print("👻 Executing Ghost Mode (Decoy V2)...")

                    res = settlement_balance(
                        api_key,
                        tokens,
                        items,
                        "🤫",
                        False,
                        overwrite_amount=total_overwrite,
                        token_confirmation_idx=1,  # decoy confirmation
                    )
                    if res:
                        _handle_purchase_response(res, api_key, tokens, items, "🤫", token_confirmation_idx=1)
                    else:
                        print("❌ Transaksi Gagal (Return None dari Settlement).")
                        pause()
                    return True

                if choice == "6":
                    items, decoy_price = _prepare_decoy_items(payment_items, "qris")
                    if not items:
                        continue
                    print(f"Harga Asli: {price} | Decoy: {decoy_price}")
                    show_qris_payment(api_key, tokens, items, "SHARE_PACKAGE", True, token_confirmation_idx=1)
                    pause()
                    return True

                if choice == "7":
                    items, _ = _prepare_decoy_items(payment_items, "qris0")
                    if not items:
                        continue
                    show_qris_payment(api_key, tokens, items, "SHARE_PACKAGE", True, token_confirmation_idx=1)
                    pause()
                    return True
                # --- DECOY END ---

                if choice == "8":
                    _handle_bomb_purchase(package_option_code)
                    continue

                # Redeem / Gift / Loyalty
                if choice == "R" and payment_for == "REDEEM_VOUCHER":
                    settlement_bounty(
                        api_key=api_key,
                        tokens=tokens,
                        token_confirmation=package.get("token_confirmation"),
                        ts_to_sign=package.get("timestamp"),
                        payment_target=package_option_code,
                        price=price,
                        item_name=variant_name,
                    )
                    pause()
                    return True

                if choice == "S" and payment_for == "REDEEM_VOUCHER":
                    dest = input("Nomor Tujuan (62...): ").strip()
                    if dest:
                        bounty_allotment(
                            api_key=api_key,
                            tokens=tokens,
                            ts_to_sign=package.get("timestamp"),
                            destination_msisdn=dest,
                            item_name=option_name,
                            item_code=package_option_code,
                            token_confirmation=package.get("token_confirmation"),
                        )
                    pause()
                    return True

                if choice == "L" and payment_for == "REDEEM_VOUCHER":
                    settlement_loyalty(
                        api_key=api_key,
                        tokens=tokens,
                        token_confirmation=package.get("token_confirmation"),
                        ts_to_sign=package.get("timestamp"),
                        payment_target=package_option_code,
                        price=price,
                    )
                    pause()
                    return True

                print("⚠️ Pilihan tidak valid.")
                pause()

            except Exception as e:
                print("\n" + "!" * 50)
                print("💥 ERROR TERJADI SAAT EKSEKUSI MENU 💥")
                print(f"Pesan Error: {str(e)}")
                print("-" * 50)
                traceback.print_exc()
                print("!" * 50)
                print("Sistem tidak akan keluar. Silahkan coba lagi atau kembali.")
                pause()

    except Exception as main_e:
        print(f"Fatal Error di Package Menu: {main_e}")
        traceback.print_exc()
        pause()
        return False


# =============================================================================
# Browse packages by family
# =============================================================================

def get_packages_by_family(family_code: str, is_enterprise: Optional[bool] = None, migration_type: Optional[str] = None) -> None:
    api_key, tokens = _get_api_key_tokens()
    if not tokens:
        print("❌ Session expired.")
        pause()
        return

    data = get_family(api_key, tokens, family_code, is_enterprise, migration_type)
    if not data:
        print("❌ Paket family tidak ditemukan / kosong.")
        pause()
        return

    fam_info = data.get("package_family") or {}
    variants = data.get("package_variants") or []
    price_currency = "Rp"
    if _as_str(fam_info.get("rc_bonus_type")).strip().upper() == "MYREWARDS":
        price_currency = "Poin"

    while True:
        clear_screen()
        print("=" * WIDTH)
        print(f"FAMILY: {_as_str(fam_info.get('name') or 'Unknown')}".center(WIDTH))
        print(f"Code: {family_code}".center(WIDTH))
        print("=" * WIDTH)

        flattened_opts: List[Dict[str, Any]] = []
        opt_counter = 1

        for var in variants:
            if not isinstance(var, dict):
                continue
            print(f"\n[ {_as_str(var.get('name') or 'Variant')} ]")
            print(f" Code: {_as_str(var.get('package_variant_code') or '-')}")
            for opt in var.get("package_options") or []:
                if not isinstance(opt, dict):
                    continue
                curr_num = opt_counter
                price_tag = f"{price_currency} {_as_int(opt.get('price'), 0):,}"
                print(f"  {curr_num:2}. {_as_str(opt.get('name') or 'Option'):<35} {price_tag:>15}")
                flattened_opts.append(
                    {
                        "num": curr_num,
                        "code": _as_str(opt.get("package_option_code") or ""),
                        "order": _as_int(opt.get("order"), -1),
                    }
                )
                opt_counter += 1

        print("\n" + "-" * WIDTH)
        print("[0] Kembali")
        choice = input("Pilih Paket >> ").strip()

        if choice == "0":
            return

        if choice.isdigit():
            sel_num = int(choice)
            selected = next((x for x in flattened_opts if x["num"] == sel_num), None)
            if selected and selected.get("code"):
                show_package_details(api_key, tokens, selected["code"], bool(is_enterprise), option_order=selected.get("order", -1))
            else:
                print("⚠️ Nomor paket tidak ada.")
                pause()
        else:
            print("⚠️ Input harus angka.")
            pause()


# =============================================================================
# My packages / quota-details viewer + re-buy + unsubscribe
# =============================================================================

def fetch_my_packages() -> None:
    """
    Menampilkan paket aktif:
    - Smart expiry detection
    - Detail pull (family_code + real_option_code) best-effort
    - Unsubscribe flow (arg order fixed)
    """
    api_key, tokens = _get_api_key_tokens()
    if not tokens:
        return

    print("⏳ Mengambil list paket aktif...")
    res = send_api_request(api_key, "api/v8/packages/quota-details", {"is_enterprise": False, "lang": "en"}, tokens.get("id_token"), "POST")
    if _as_str(res.get("status")).strip().upper() != "SUCCESS":
        print("❌ Gagal mengambil data.")
        pause()
        return

    quotas = _safe_list(res.get("data", {}).get("quotas", []))

    while True:
        clear_screen()
        print("=" * WIDTH)
        print("PAKET SAYA".center(WIDTH))
        print("=" * WIDTH)

        mapped_pkgs: List[Dict[str, Any]] = []
        if not quotas:
            print("  [Tidak ada paket aktif]")

        for idx, q in enumerate(quotas, 1):
            if not isinstance(q, dict):
                continue

            exp_str = _as_str(q.get("expiry_date")).strip()

            # fallback from expired_at
            if not exp_str or exp_str == "N/A":
                ts = q.get("expired_at")
                if isinstance(ts, (int, float)):
                    final_ts = float(ts)
                    if final_ts > 100_000_000_000:
                        final_ts /= 1000.0
                    if final_ts < 1_000_000:
                        exp_str = "Unlimited / Seumur Hidup"
                    else:
                        try:
                            dt = datetime.fromtimestamp(final_ts)
                            exp_str = "Unlimited / Unknown" if dt.year < 2020 else dt.strftime("%d-%m-%Y %H:%M")
                        except Exception:
                            exp_str = "Invalid Date"
                else:
                    exp_str = "Unlimited / Unknown"

            if "1970" in exp_str:
                exp_str = "Unlimited / Unknown"

            pkg_name = _as_str(q.get("name") or "Unknown Package")
            quota_code = _as_str(q.get("quota_code") or "-")

            # Best-effort mapping to family_code & real_option_code
            family_code = "N/A"
            real_option_code = quota_code
            try:
                pkg_detail = get_package(api_key, tokens, quota_code)
                if isinstance(pkg_detail, dict) and pkg_detail:
                    family_code = _as_str((pkg_detail.get("package_family") or {}).get("package_family_code") or "N/A")
                    real_option_code = _as_str((pkg_detail.get("package_option") or {}).get("package_option_code") or quota_code)
                else:
                    family_code = "Failed to fetch (Bonus/Legacy)"
            except Exception:
                family_code = "Error"

            print(f"{idx}. {pkg_name}")
            print(f"   Fam Code : {family_code}")
            print(f"   ID/Code  : {quota_code}")
            print(f"   Exp      : {exp_str}")

            benefits = q.get("benefits") or []
            if not isinstance(benefits, list) or not benefits:
                print("   • (Tidak ada detail kuota)")
            else:
                for b in benefits:
                    if not isinstance(b, dict):
                        continue
                    b_type = _as_str(b.get("data_type")).upper()
                    b_name = _as_str(b.get("name") or "Quota")
                    b_rem = b.get("remaining", 0)
                    b_tot = b.get("total", 0)

                    if b_type == "DATA":
                        print(f"   • {b_name:<25}: {format_quota_byte(b_rem)} / {format_quota_byte(b_tot)}")
                    elif b_type == "VOICE":
                        print(f"   • {b_name:<25}: {_as_int(b_rem)//60} / {_as_int(b_tot)//60} Min")
                    elif b_type == "TEXT":
                        print(f"   • {b_name:<25}: {_as_int(b_rem)} / {_as_int(b_tot)} SMS")
                    else:
                        # Heuristic
                        try:
                            if _as_int(b_tot) > 10000:
                                print(f"   • {b_name:<25}: {format_quota_byte(b_rem)}")
                            else:
                                print(f"   • {b_name:<25}: {_as_str(b_rem)}")
                        except Exception:
                            print(f"   • {b_name:<25}: {_as_str(b_rem)}")

            mapped_pkgs.append({"quota": q, "real_option_code": real_option_code})
            print("-" * WIDTH)

        print("[Nomor] Lihat Detail & Beli Lagi")
        print("[del No] Unsubscribe Paket (Contoh: del 1)")
        print("[0] Kembali")

        choice = input(">> ").strip()
        if choice == "0":
            return

        if choice.startswith("del ") and len(choice.split()) == 2:
            try:
                idx = int(choice.split()[1]) - 1
                if 0 <= idx < len(mapped_pkgs):
                    pkg = mapped_pkgs[idx]["quota"]
                    conf = input(f"Yakin STOP paket {_as_str(pkg.get('name'))}? (y/n): ").strip().lower()
                    if conf == "y":
                        quota_code = _as_str(pkg.get("quota_code"))
                        # FIX: order should be (quota_code, product_domain, product_subscription_type)
                        product_domain = _as_str(pkg.get("product_domain") or "PACKAGES")
                        product_subtype = _as_str(pkg.get("product_subscription_type") or "PREPAID")
                        ok = unsubscribe(api_key, tokens, quota_code, product_domain, product_subtype)
                        print("✅ Berhenti berlangganan berhasil." if ok else "❌ Gagal.")
                        pause()
                        # refresh list after unsubscribe
                        res2 = send_api_request(api_key, "api/v8/packages/quota-details", {"is_enterprise": False, "lang": "en"}, tokens.get("id_token"), "POST")
                        if _as_str(res2.get("status")).strip().upper() == "SUCCESS":
                            quotas = _safe_list(res2.get("data", {}).get("quotas", []))
                        continue
            except Exception as e:
                print(f"Error: {e}")
                pause()
                continue

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(mapped_pkgs):
                show_package_details(api_key, tokens, mapped_pkgs[idx]["real_option_code"], False)
            else:
                print("⚠️ Nomor tidak ada.")
                pause()
            continue

        print("⚠️ Input tidak valid.")
        pause()


def _safe_list(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []