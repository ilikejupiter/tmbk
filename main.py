#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# file: main.py

"""
MYXL Terminal - Main Entrypoint (Modernized + Stabilized)
Compatible with Python 3.9.18
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from shutil import get_terminal_size
from typing import Callable, Dict, List, Optional, Tuple


# =============================================================================
# Optional dotenv (won't crash if python-dotenv isn't installed)
# =============================================================================


def try_load_dotenv(*, override: bool = False) -> bool:
    """
    Attempt to load .env without hard dependency.
    Returns True if loaded, else False.

    Env:
        MYXL_AUTO_LOAD_DOTENV=0  -> skip loading
    """
    auto = os.getenv("MYXL_AUTO_LOAD_DOTENV", "1").strip().lower()
    if auto in {"0", "false", "no"}:
        return False
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return False
    try:
        load_dotenv(override=override)
        return True
    except Exception:
        return False


try_load_dotenv()


# =============================================================================
# Import Internal Modules (project modules) - fail-fast with clear error
# =============================================================================

try:
    from app.service.git import check_for_updates
    from app.menus.util import clear_screen, pause
    from app.client.engsel import get_balance, get_tiering_info
    from app.client.famplan import validate_msisdn
    from app.menus.payment import show_transaction_history
    from app.service.auth import AuthInstance
    from app.menus.bookmark import show_bookmark_menu
    from app.menus.account import show_account_menu
    from app.menus.package import (
        fetch_my_packages,
        get_packages_by_family,
        show_package_details,
    )
    from app.menus.hot import show_hot_menu, show_hot_menu2
    from app.service.sentry import enter_sentry_mode
    from app.menus.purchase import purchase_by_family
    from app.menus.famplan import show_family_info
    from app.menus.circle import show_circle_info
    from app.menus.notification import show_notification_menu
    from app.menus.store.segments import show_store_segments_menu
    from app.menus.store.search import (
        show_family_list_menu,
        show_store_packages_menu,
    )
    from app.menus.store.redemables import show_redeemables_menu
    from app.client.registration import dukcapil
    from app.menus.custom_loop import show_custom_loop_menu
except ImportError as _imp_err:  # noqa: N816 (keep clear name)
    # Penting: kasih pesan jelas kalau script dijalankan tanpa struktur project benar.
    sys.stderr.write(
        "\n❌ Gagal mengimpor modul internal aplikasi.\n"
        f"   Detail: {repr(_imp_err)}\n"
        "   Pastikan:\n"
        "     - Anda menjalankan script dari root project (bukan dari folder lain)\n"
        "     - Package 'app' dan submodulnya sudah tersedia di PYTHONPATH\n"
        "   Contoh: `python -m app.main` atau `python main.py` dari root project.\n\n"
    )
    sys.exit(1)


# =============================================================================
# App Configuration
# =============================================================================

APP_NAME = "MYXL TERMINAL"
VERSION = "3.0.0"

DEFAULT_WIDTH = 60

# Prefer logs directory if possible
DEFAULT_LOG_PATH = os.getenv("APP_LOG_FILE", "")
if DEFAULT_LOG_PATH.strip():
    LOG_FILE = Path(DEFAULT_LOG_PATH).expanduser()
else:
    LOG_FILE = Path.cwd() / "logs" / "app.log"

LOG_MAX_BYTES = int(os.environ.get("APP_LOG_MAX_BYTES", "1000000"))  # 1MB
LOG_BACKUP_COUNT = int(os.environ.get("APP_LOG_BACKUP_COUNT", "5"))


# =============================================================================
# Logging Setup (stable + no duplicate handlers)
# =============================================================================


def setup_logging(level: str = "INFO") -> logging.Logger:
    log_level = getattr(logging, level.upper(), logging.INFO)

    logger = logging.getLogger("myxl")
    logger.setLevel(log_level)

    # Avoid adding handlers multiple times
    if getattr(logger, "_myxl_configured", False):
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler (rotating) - ensure directory exists
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            str(LOG_FILE),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        # Kalau file logging gagal (misal permission), tetap lanjut dengan console logging.
        pass

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger._myxl_configured = True  # type: ignore[attr-defined]
    return logger


logger = setup_logging(os.getenv("APP_LOG_LEVEL", "INFO"))


# =============================================================================
# Utilities (input, UI, dates)
# =============================================================================


def is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def term_width() -> int:
    try:
        cols = get_terminal_size().columns
        return max(cols, DEFAULT_WIDTH)
    except Exception:
        return DEFAULT_WIDTH


def safe_pause() -> None:
    """Pause only when interactive TTY."""
    if not is_tty():
        return
    try:
        pause()
    except Exception:
        # Jangan biarkan pause gagal merusak flow.
        pass


def ask_text(prompt: str, default: Optional[str] = None) -> str:
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError:
            return default or ""
        if not raw and default is not None:
            return default
        if raw:
            return raw
        print("⚠️  Input tidak boleh kosong.")


def ask_int(
    prompt: str,
    default: Optional[int] = None,
    allow_quit: bool = False,
) -> int:
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError:
            if default is not None:
                return default
            print("⚠️  Tidak ada input. Coba lagi.")
            continue

        if not raw and default is not None:
            return default

        if allow_quit and raw.lower() == "q":
            return -1

        try:
            return int(raw)
        except ValueError:
            print("❌ Input harus berupa angka.")


def ask_bool(prompt: str, default: Optional[bool] = None) -> bool:
    mapping_true = {"y", "yes", "1", "true", "t"}
    mapping_false = {"n", "no", "0", "false", "f"}
    while True:
        try:
            raw = input(prompt).strip().lower()
        except EOFError:
            return default if default is not None else False

        if not raw and default is not None:
            return default
        if raw in mapping_true:
            return True
        if raw in mapping_false:
            return False
        print("⚠️  Jawab dengan y/n.")


def format_expiry(ts_like: Optional[float]) -> str:
    if not ts_like:
        return "Unknown"
    try:
        ts = float(ts_like)
        if ts > 10**12:  # ms
            ts /= 1000.0
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%d %b %Y")
    except Exception:
        return "Unknown"


def rupiah(val) -> str:
    if isinstance(val, (int, float)):
        return f"Rp {val:,.0f}".replace(",", ".")
    return str(val)


def show_header(profile: dict, width: Optional[int] = None) -> None:
    width = width or term_width()
    clear = os.environ.get("NO_CLEAR", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }

    if clear and is_tty():
        try:
            clear_screen()
        except Exception:
            pass

    print("=" * width)
    print(f"{APP_NAME} v{VERSION}".center(width))
    print("=" * width)

    exp_date = format_expiry(profile.get("balance_expired_at"))
    number = profile.get("number", "-")
    subs = profile.get("subscription_type", "UNKNOWN")
    bal = rupiah(profile.get("balance", "N/A"))
    points = profile.get("point_info", "Points: - | Tier: -")

    print(f" 📱 {number} ({subs})".center(width))
    print(f" 💰 {bal} | Exp: {exp_date}".center(width))
    print(f" 🌟 {points}".center(width))
    print("=" * width)


# =============================================================================
# Menu System
# =============================================================================


@dataclass(frozen=True)
class MenuItem:
    keys: Tuple[str, ...]
    label: str
    handler: Callable[[dict], None]
    visible: bool = True


def _h_login(active_user: dict) -> None:
    selected = show_account_menu()
    if selected:
        AuthInstance.set_active_user(selected)


def _h_my_packages(active_user: dict) -> None:
    fetch_my_packages()


def _h_hot(active_user: dict) -> None:
    show_hot_menu()


def _h_hot2(active_user: dict) -> None:
    show_hot_menu2()


def _h_option_code(active_user: dict) -> None:
    code = ask_text("Masukkan Option Code: ")
    if code:
        tokens = active_user.get("tokens") or {}
        show_package_details(AuthInstance.api_key, tokens, code, False)


def _h_family_code(active_user: dict) -> None:
    code = ask_text("Masukkan Family Code: ")
    if code:
        get_packages_by_family(code)


def _h_buy_all_family(active_user: dict) -> None:
    fam_code = ask_text("Masukkan Family Code: ")
    if not fam_code:
        return
    start_opt = ask_int("Mulai dari urutan ke (default 1): ", 1)
    use_decoy = ask_bool("Gunakan Decoy/Pancingan? (y/n): ", False)
    pause_ok = ask_bool("Pause jika sukses? (y/n): ", False)
    delay = ask_int("Delay (detik): ", 0)
    purchase_by_family(fam_code, use_decoy, pause_ok, delay, start_opt)


def _h_history(active_user: dict) -> None:
    tokens = active_user.get("tokens") or {}
    show_transaction_history(AuthInstance.api_key, tokens)


def _h_family_org(active_user: dict) -> None:
    tokens = active_user.get("tokens") or {}
    show_family_info(AuthInstance.api_key, tokens)


def _h_circle_org(active_user: dict) -> None:
    tokens = active_user.get("tokens") or {}
    show_circle_info(AuthInstance.api_key, tokens)


def _ask_is_ent() -> bool:
    return ask_bool("Is Enterprise? (y/n): ", False)


def _h_store_segments(active_user: dict) -> None:
    show_store_segments_menu(_ask_is_ent())


def _h_store_family_list(active_user: dict) -> None:
    show_family_list_menu(
        active_user.get("subscription_type", "UNKNOWN"),
        _ask_is_ent(),
    )


def _h_store_packages(active_user: dict) -> None:
    show_store_packages_menu(
        active_user.get("subscription_type", "UNKNOWN"),
        _ask_is_ent(),
    )


def _h_redeemables(active_user: dict) -> None:
    show_redeemables_menu(_ask_is_ent())


def _h_custom_loop(active_user: dict) -> None:
    show_custom_loop_menu()


def _h_bookmark(active_user: dict) -> None:
    show_bookmark_menu()


def _h_dukcapil(active_user: dict) -> None:
    msisdn = ask_text("MSISDN (628...): ")
    nik = ask_text("NIK: ")
    kk = ask_text("KK: ")
    if msisdn and nik and kk:
        res = dukcapil(AuthInstance.api_key, msisdn, kk, nik)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        safe_pause()


def _h_validate_msisdn(active_user: dict) -> None:
    msisdn = ask_text("MSISDN Target: ")
    if msisdn:
        tokens = active_user.get("tokens") or {}
        res = validate_msisdn(AuthInstance.api_key, tokens, msisdn)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        safe_pause()


def _h_notification(active_user: dict) -> None:
    show_notification_menu()


def _h_sentry(active_user: dict) -> None:
    enter_sentry_mode()


def _h_exit(active_user: dict) -> None:
    print("👋 Sampai Jumpa!")
    raise SystemExit(0)


def build_menu() -> List[MenuItem]:
    return [
        MenuItem(("1",), "Login / Ganti Akun", _h_login),
        MenuItem(("2",), "Lihat Paket Saya", _h_my_packages),
        MenuItem(("3",), "Beli Paket 🔥 HOT 🔥", _h_hot),
        MenuItem(("4",), "Beli Paket 🔥 HOT-2 🔥", _h_hot2),
        MenuItem(("5",), "Beli Paket (Option Code)", _h_option_code),
        MenuItem(("6",), "Lihat Family (Family Code)", _h_family_code),
        MenuItem(("7",), "Beli Semua di Family (Loop)", _h_buy_all_family),
        MenuItem(("8",), "Riwayat Transaksi", _h_history),
        MenuItem(("9",), "Family Plan Organizer", _h_family_org),
        MenuItem(("10",), "Circle Organizer", _h_circle_org),
        MenuItem(("11",), "Store Segments", _h_store_segments),
        MenuItem(("12",), "Store Family List", _h_store_family_list),
        MenuItem(("13",), "Store Packages (Cari Paket)", _h_store_packages),
        MenuItem(("14",), "Redeemables (Voucher/Bonus)", _h_redeemables),
        MenuItem(("15",), "Custom Loop / Bomber (Menu Baru) 🔥", _h_custom_loop),
        MenuItem(("R", "r"), "Registrasi Kartu (Dukcapil)", _h_dukcapil),
        MenuItem(("N", "n"), "Notifikasi", _h_notification),
        MenuItem(("V", "v"), "Validasi MSISDN", _h_validate_msisdn),
        MenuItem(("S", "s"), "Sentry Mode (Monitoring)", _h_sentry),
        MenuItem(("00",), "Bookmark Paket", _h_bookmark),
        MenuItem(("99",), "Keluar", _h_exit),
    ]


def build_menu_mapping(items: List[MenuItem]) -> Dict[str, Callable[[dict], None]]:
    mapping: Dict[str, Callable[[dict], None]] = {}
    for it in items:
        for k in it.keys:
            mapping[k] = it.handler
    return mapping


def print_menu(items: List[MenuItem], width: Optional[int] = None) -> None:
    width = width or term_width()
    print("MENU UTAMA:")

    display = [(it.keys[0], it.label) for it in items if it.visible]
    half = (len(display) + 1) // 2

    for i in range(half):
        left_key, left_label = display[i]
        left_txt = f" [{left_key:>2}] {left_label:<30}"

        right_txt = ""
        j = i + half
        if j < len(display):
            right_key, right_label = display[j]
            right_txt = f"| [{right_key:>2}] {right_label}"

        print(f"{left_txt}{right_txt}")

    print("-" * width)


def run_menu_choice(
    choice: str,
    mapping: Dict[str, Callable[[dict], None]],
    active_user: dict,
) -> None:
    handler = mapping.get(choice)
    if not handler:
        print("⚠️ Pilihan tidak valid.")
        safe_pause()
        return

    try:
        handler(active_user)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n🛑 Dibatalkan pengguna.")
    except Exception as e:
        logger.exception("Error saat menjalankan menu '%s': %s", choice, e)
        print(f"\n❌ Terjadi kesalahan pada menu: {e}")
        safe_pause()


# =============================================================================
# Core Loop
# =============================================================================


def fetch_profile(active_user: dict) -> dict:
    balance_val = "N/A"
    expired_val: Optional[float] = None
    point_str = "Points: - | Tier: -"

    tokens = active_user.get("tokens") or {}
    id_token = tokens.get("id_token")

    # Balance
    try:
        if id_token:
            bal_data = get_balance(AuthInstance.api_key, id_token)
            if bal_data:
                balance_val = bal_data.get("remaining", 0)
                expired_val = bal_data.get("expired_at")
    except Exception as e:
        logger.warning("Gagal mengambil balance: %s", e)

    # Tiering PREPAID
    if active_user.get("subscription_type") == "PREPAID":
        try:
            tier_data = get_tiering_info(AuthInstance.api_key, tokens)
            if tier_data:
                point_str = (
                    f"Points: {tier_data.get('current_point', 0)} | "
                    f"Tier: {tier_data.get('tier', 0)}"
                )
        except Exception as e:
            logger.warning("Gagal mengambil tiering: %s", e)

    return {
        "number": active_user.get("number", "-"),
        "subscriber_id": active_user.get("subscriber_id", "-"),
        "subscription_type": active_user.get("subscription_type", "UNKNOWN"),
        "balance": balance_val,
        "balance_expired_at": expired_val,
        "point_info": point_str,
    }


def login_flow() -> Optional[dict]:
    clear = os.environ.get("NO_CLEAR", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }

    if clear and is_tty():
        try:
            clear_screen()
        except Exception:
            pass

    width = term_width()
    print("=" * width)
    print(f"{APP_NAME} - SILAHKAN LOGIN".center(width))
    print("=" * width)

    selected = show_account_menu()
    if selected:
        AuthInstance.set_active_user(selected)
        return selected

    print("Tidak ada user dipilih.")
    retry = ask_bool("Coba lagi? (y/n): ", True)
    if retry:
        return None
    raise SystemExit(0)


def main_loop() -> None:
    items = build_menu()
    mapping = build_menu_mapping(items)

    while True:
        try:
            active_user = AuthInstance.get_active_user()
            if not active_user:
                selected = login_flow()
                if selected is None:
                    continue

            active_user = AuthInstance.get_active_user() or {}
            profile = fetch_profile(active_user)

            show_header(profile)
            print_menu(items)

            choice = ask_text("Pilihan >> ")
            run_menu_choice(choice, mapping, active_user)

        except KeyboardInterrupt:
            print("\n\n🛑 Aplikasi dihentikan pengguna.")
            raise SystemExit(130)
        except SystemExit:
            raise
        except Exception as e:
            logger.critical("Critical Loop Error: %s", e)
            print(f"Critical Error: {e}")
            safe_pause()


# =============================================================================
# Signals & CLI
# =============================================================================


def _signal_handler(signum, frame) -> None:
    print(f"\n🛑 Sinyal {signum} diterima. Keluar…")
    raise SystemExit(0)


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="myxl-terminal",
        description="MYXL Terminal",
    )
    p.add_argument(
        "--skip-update",
        action="store_true",
        help="Lewati pemeriksaan pembaruan git",
    )
    p.add_argument(
        "--no-clear",
        action="store_true",
        help="Jangan clear layar (mode CI/log)",
    )
    p.add_argument(
        "--log-level",
        default=os.getenv("APP_LOG_LEVEL", "INFO"),
        help="Level log: DEBUG, INFO, WARNING, ERROR",
    )
    p.add_argument(
        "--version",
        action="store_true",
        help="Tampilkan versi lalu keluar",
    )
    return p.parse_args(argv)


def run(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or [])

    if args.version:
        print(f"{APP_NAME} v{VERSION}")
        return 0

    if args.no_clear:
        os.environ["NO_CLEAR"] = "1"

    global logger
    logger = setup_logging(args.log_level)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal_handler)
        except Exception:
            # Misal pada environment yang tidak mendukung signal tertentu.
            pass

    try:
        if not args.skip_update:
            print("🔍 Memeriksa pembaruan script...")
            try:
                if check_for_updates():
                    print("🆕 Update ditemukan. Silakan restart jika diperlukan.")
                    safe_pause()
            except Exception as e:
                logger.warning("Gagal memeriksa pembaruan: %s", e)

        main_loop()
        return 0
    except SystemExit as se:
        return int(getattr(se, "code", 0) or 0)
    except Exception as e:
        print(f"Fatal Boot Error: {e}")
        logger.exception("Fatal Boot Error: %s", e)
        return 1


def main() -> None:
    code = run(sys.argv[1:])
    sys.exit(code)


if __name__ == "__main__":
    main()