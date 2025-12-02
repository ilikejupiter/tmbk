"""
hot.py — Menu paket HOT/promo.

Target: Python 3.9.18
Fokus perbaikan:
- I/O lebih rapi dan tahan error
- Loader JSON aman + path hot_data lebih robust
- Validasi konfigurasi & guard clause biar tidak gampang crash
- Formatting benefit lebih rapi
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.client.engsel import get_family, get_package_details
from app.menus.package import show_package_details
from app.service.auth import AuthInstance
from app.menus.util import clear_screen, pause, format_quota_byte
from app.client.purchase.ewallet import show_multipayment
from app.client.purchase.qris import show_qris_payment
from app.client.purchase.balance import settlement_balance
from app.type_dict import PaymentItem

WIDTH: int = 60

__all__ = ["show_hot_menu", "show_hot_menu2"]


# -------------------------
# Utilities
# -------------------------
def _data_dir_candidates() -> List[Path]:
    """
    Cari folder hot_data dari beberapa kandidat lokasi:
    1) CWD/hot_data (umum saat app dijalankan dari root project)
    2) Lokasi file ini/../hot_data (kalau hot.py ada di subfolder)
    3) Lokasi file ini/hot_data (kalau hot_data satu folder)
    """
    here = Path(__file__).resolve().parent
    return [
        Path.cwd() / "hot_data",
        here.parent / "hot_data",
        here / "hot_data",
    ]


def _resolve_data_file(filename: str) -> Path:
    """
    Mengembalikan path terbaik untuk file data hot.
    Jika tidak ditemukan, fallback ke CWD/hot_data/filename (biar error message jelas).
    """
    for d in _data_dir_candidates():
        candidate = d / filename
        if candidate.exists():
            return candidate
    return Path.cwd() / "hot_data" / filename


def _load_json_safe(filepath: os.PathLike | str) -> List[Dict[str, Any]]:
    """Helper untuk memuat file JSON dengan aman. Selalu mengembalikan list."""
    path = Path(filepath)
    if not path.exists():
        print(f"❌ File data tidak ditemukan: {path}")
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"❌ File rusak/bukan JSON valid: {path}")
        return []
    except Exception as e:
        print(f"❌ Error membaca file: {e}")
        return []

    if isinstance(data, list):
        # pastikan elemennya dict (kalau ada yang bukan, tetap biarkan tapi aman)
        return [x for x in data if isinstance(x, dict)]
    print(f"❌ Format JSON tidak sesuai (harus list of object): {path}")
    return []


def _input_choice(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        # treat as "back" in CLI
        return "00"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        if isinstance(value, (int,)):
            return value
        if isinstance(value, float):
            return int(value)
        s = str(value).strip()
        # handle "10.000", "10,000", "10000"
        s = s.replace(".", "").replace(",", "")
        return int(s)
    except Exception:
        return default


def _fmt_benefit(b: Dict[str, Any]) -> str:
    name = str(b.get("name", "Benefit"))
    b_type = str(b.get("data_type", "OTHER")).upper()
    total = b.get("total", 0)
    unlimited = bool(b.get("is_unlimited"))

    info = ""
    if b_type == "DATA":
        info = format_quota_byte(_to_int(total, 0))
    elif b_type == "VOICE":
        # biasanya dalam detik
        seconds = _to_int(total, 0)
        info = f"{seconds / 60:.1f} Menit"
    elif b_type == "TEXT":
        info = f"{_to_int(total, 0)} SMS"
    else:
        info = str(total)

    tag = " [UNLIMITED]" if unlimited else ""
    return f" • {name:<25} : {info}{tag}"


def _safe_get_tokens() -> Any:
    """
    AuthInstance.get_active_tokens() kadang bisa None/invalid.
    Kita jaga agar fungsi ini tidak bikin crash menu.
    """
    try:
        return AuthInstance.get_active_tokens()
    except Exception as e:
        print(f"❌ Gagal mengambil token aktif: {e}")
        return None


# -------------------------
# HOT Menu V1 (simple)
# -------------------------
def show_hot_menu() -> None:
    """Menu untuk paket Hot (Versi 1)."""
    api_key = getattr(AuthInstance, "api_key", None)
    tokens = _safe_get_tokens()

    while True:
        clear_screen()
        print("=" * WIDTH)
        print("🔥 PAKET HOT & PROMO 🔥".center(WIDTH))
        print("=" * WIDTH)

        hot_packages = _load_json_safe(_resolve_data_file("hot.json"))
        if not hot_packages:
            print("  [Data paket kosong / tidak bisa dibaca]")
            pause()
            return

        for idx, p in enumerate(hot_packages, start=1):
            fam = p.get("family_name", "?")
            opt = p.get("option_name", "?")
            print(f"{idx}. {fam} - {opt}")

        print("-" * WIDTH)
        print("[00] Kembali")

        choice = _input_choice("Pilih Paket >> ")
        if choice in {"00", "0"}:
            return

        if not choice.isdigit():
            print("⚠️ Input harus angka.")
            pause()
            continue

        idx = int(choice) - 1
        if not (0 <= idx < len(hot_packages)):
            print("⚠️ Nomor tidak valid.")
            pause()
            continue

        _process_hot_package(api_key, tokens, hot_packages[idx])


def _process_hot_package(api_key: Any, tokens: Any, selected_bm: Dict[str, Any]) -> None:
    """Memproses pemilihan paket dari menu Hot 1."""
    clear_screen()
    print("⏳ Mengambil detail paket...")

    family_code = selected_bm.get("family_code")
    is_enterprise = bool(selected_bm.get("is_enterprise", False))
    if not family_code:
        print("❌ Konfigurasi rusak: 'family_code' tidak ada.")
        pause()
        return

    try:
        family_data = get_family(api_key, tokens, family_code, is_enterprise)
    except Exception as e:
        print(f"❌ Gagal mengambil data family: {e}")
        pause()
        return

    if not family_data:
        print("❌ Gagal mengambil data family (response kosong).")
        pause()
        return

    target_variant = selected_bm.get("variant_name")
    target_order = selected_bm.get("order")

    found_code: Optional[str] = None
    variants = family_data.get("package_variants") or []

    # Cari option code berdasarkan nama varian & order
    for variant in variants:
        if target_variant and variant.get("name") != target_variant:
            continue

        options = variant.get("package_options") or []
        for opt in options:
            # kalau order tidak diset di config, ambil yang pertama yang punya code
            if target_order is not None and opt.get("order") != target_order:
                continue
            code = opt.get("package_option_code")
            if code:
                found_code = code
                break
        if found_code:
            break

    if found_code:
        show_package_details(api_key, tokens, found_code, is_enterprise)
        return

    # Fallback debug info agar gampang benahi hot.json
    print(f"❌ Paket spesifik tidak ditemukan dalam family {family_code}.")
    print(f"   Target Variant: {target_variant!r} | Target Order: {target_order!r}")
    print("   Available:")
    for v in variants:
        vname = v.get("name", "?")
        opts = v.get("package_options") or []
        orders = [o.get("order") for o in opts]
        print(f"   - {vname}: orders={orders}")
    pause()


# -------------------------
# HOT Menu V2 (bundling/custom)
# -------------------------
def show_hot_menu2() -> None:
    """Menu untuk paket Hot V2 (Advanced Config)."""
    api_key = getattr(AuthInstance, "api_key", None)
    tokens = _safe_get_tokens()

    while True:
        clear_screen()
        print("=" * WIDTH)
        print("🔥 PAKET HOT V2 (BUNDLING/CUSTOM) 🔥".center(WIDTH))
        print("=" * WIDTH)

        hot_packages = _load_json_safe(_resolve_data_file("hot2.json"))
        if not hot_packages:
            print("  [Data paket kosong / tidak bisa dibaca]")
            pause()
            return

        for idx, p in enumerate(hot_packages, start=1):
            name = p.get("name", "Unnamed")
            price = p.get("price", "N/A")
            print(f"{idx}. {name}")
            print(f"   🏷️  {price}")
            print("-" * WIDTH)

        print("[00] Kembali")

        choice = _input_choice("Pilih Paket >> ")
        if choice in {"00", "0"}:
            return

        if not choice.isdigit():
            print("⚠️ Input harus angka.")
            pause()
            continue

        idx = int(choice) - 1
        if not (0 <= idx < len(hot_packages)):
            print("⚠️ Nomor tidak valid.")
            pause()
            continue

        _process_hot2_package(api_key, tokens, hot_packages[idx])


def _process_hot2_package(api_key: Any, tokens: Any, package_config: Dict[str, Any]) -> None:
    """Memproses logika pembelian kompleks Hot V2."""
    packages_list = package_config.get("packages", [])
    if not isinstance(packages_list, list) or not packages_list:
        print("⚠️ Konfigurasi paket ini kosong.")
        pause()
        return

    print("⏳ Menyiapkan item pembayaran...")
    payment_items: List[Any] = []

    main_detail: Optional[Dict[str, Any]] = None

    # info teknis untuk display
    first = packages_list[0] if isinstance(packages_list[0], dict) else {}
    fam_code_display = first.get("family_code", "?")

    try:
        for pkg_cfg in packages_list:
            if not isinstance(pkg_cfg, dict):
                raise ValueError("Konfigurasi packages harus list of object/dict")

            # Guard: minimal keys
            family_code = pkg_cfg.get("family_code")
            variant_code = pkg_cfg.get("variant_code")
            order = pkg_cfg.get("order")
            is_enterprise = bool(pkg_cfg.get("is_enterprise", False))
            migration_type = pkg_cfg.get("migration_type")

            if not family_code or variant_code is None or order is None:
                raise ValueError(
                    f"Konfigurasi packages incomplete: {pkg_cfg} "
                    "(wajib: family_code, variant_code, order)"
                )

            detail = get_package_details(
                api_key,
                tokens,
                family_code,
                variant_code,
                order,
                is_enterprise,
                migration_type,
            )

            if not detail:
                raise RuntimeError(f"Gagal ambil detail: {family_code}")

            if main_detail is None:
                main_detail = detail

            opt = (detail or {}).get("package_option") or {}
            payment_items.append(
                PaymentItem(
                    item_code=opt.get("package_option_code"),
                    product_type="",
                    item_price=opt.get("price"),
                    item_name=opt.get("name"),
                    tax=0,
                    token_confirmation=(detail or {}).get("token_confirmation"),
                )
            )

    except Exception as e:
        print(f"❌ Error saat persiapan: {e}")
        pause()
        return

    if not main_detail:
        print("❌ Tidak ada detail paket yang bisa ditampilkan.")
        pause()
        return

    # Tampilkan Info Paket
    clear_screen()
    print("=" * WIDTH)
    print(f"📦 {package_config.get('name', 'Paket')}".center(WIDTH))
    print("=" * WIDTH)
    print(f"📝 Deskripsi:\n{package_config.get('detail', '-')}")
    print("-" * WIDTH)

    pkg_opt = (main_detail.get("package_option") or {})

    # Info teknis
    option_code = pkg_opt.get("package_option_code", "-")
    validity = pkg_opt.get("validity", "-")

    # Total harga estimasi dari item (lebih akurat daripada string config kalau tersedia)
    calc_total = 0
    for it in payment_items:
        # PaymentItem bisa dict-like atau object; yang aman: coba akses attribute, fallback key
        price = getattr(it, "item_price", None)
        if price is None and isinstance(it, dict):
            price = it.get("item_price")
        calc_total += _to_int(price, 0)

    cfg_price = package_config.get("price", "N/A")

    print(f"Family Code : {fam_code_display}")
    print(f"Option Code : {option_code}")
    print(f"Masa Aktif  : {validity}")
    if calc_total > 0:
        print(f"Total Harga : {calc_total} (Estimasi dari item)")
    else:
        print(f"Total Harga : {cfg_price} (Estimasi)")

    print("\nBenefits Lengkap:")
    benefits = pkg_opt.get("benefits") or []
    if isinstance(benefits, list) and benefits:
        for b in benefits:
            if isinstance(b, dict):
                print(_fmt_benefit(b))
    else:
        print(" (Tidak ada data benefits)")

    print("=" * WIDTH)

    # Konfigurasi Pembayaran
    payment_for = package_config.get("payment_for", "BUY_PACKAGE")
    overwrite_amt = package_config.get("overwrite_amount", -1)
    ask_overwrite = bool(package_config.get("ask_overwrite", False))

    token_confirmation_idx = _to_int(package_config.get("token_confirmation_idx", 0), 0)
    amount_idx = _to_int(package_config.get("amount_idx", -1), -1)

    def _do_balance() -> None:
        # Safety check untuk pulsa
        if overwrite_amt == -1 and not ask_overwrite:
            print("⚠️ PERINGATAN: Pastikan pulsa cukup!")
            if _input_choice("Lanjut? (y/n): ").lower() != "y":
                return

        settlement_balance(
            api_key,
            tokens,
            payment_items,
            payment_for,
            ask_overwrite,
            overwrite_amount=overwrite_amt,
            token_confirmation_idx=token_confirmation_idx,
            amount_idx=amount_idx,
        )
        pause()

    def _do_ewallet() -> None:
        show_multipayment(
            api_key,
            tokens,
            payment_items,
            payment_for,
            ask_overwrite,
            overwrite_amount=overwrite_amt,
            token_confirmation_idx=token_confirmation_idx,
            amount_idx=amount_idx,
        )
        pause()

    def _do_qris() -> None:
        show_qris_payment(
            api_key,
            tokens,
            payment_items,
            payment_for,
            ask_overwrite,
            overwrite_amount=overwrite_amt,
            token_confirmation_idx=token_confirmation_idx,
            amount_idx=amount_idx,
        )
        pause()

    while True:
        print("\nMETODE PEMBAYARAN:")
        print(" [1] Pulsa Utama")
        print(" [2] E-Wallet")
        print(" [3] QRIS")
        print(" [0] Batal")

        method = _input_choice(">> ")
        if method in {"0", "00"}:
            return
        if method == "1":
            _do_balance()
            return
        if method == "2":
            _do_ewallet()
            return
        if method == "3":
            _do_qris()
            return

        print("⚠️ Pilihan salah.")