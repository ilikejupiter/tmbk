from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

# Import dependencies internal
from app.client.ciam import get_otp, submit_otp
from app.menus.util import clear_screen, pause
from app.service.auth import AuthInstance

# =============================================================================
# LOGGER
# =============================================================================

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

WIDTH = 55


# =============================================================================
# TYPES / HELPERS
# =============================================================================

Json = Dict[str, Any]


def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    try:
        return str(val)
    except Exception:
        return default


def _safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            digits = "".join(ch for ch in val if ch.isdigit())
            return int(digits) if digits else default
        return default
    except Exception:
        return default


def _format_phone_display(phone_62: str) -> str:
    """62812xxxx -> 0812xxxx (sekadar untuk UI)."""
    s = re.sub(r"\D", "", phone_62 or "")
    if s.startswith("62"):
        return "0" + s[2:]
    return s


def validate_phone_number(phone_input: str) -> Optional[str]:
    """
    Membersihkan dan menormalisasi nomor telepon ke format 628xxx.
    Menerima variasi: 0812..., 62812..., +62 812..., 8xxx...
    """
    clean = re.sub(r"\D", "", phone_input or "").strip()

    # Normalisasi awalan
    if clean.startswith("08"):
        clean = "62" + clean[1:]
    elif clean.startswith("8"):
        clean = "62" + clean
    elif clean.startswith("6208"):
        # kasus user ngetik 6208...
        clean = "62" + clean[3:]
    elif clean.startswith("0062"):
        # 0062xxxx -> 62xxxx
        clean = "62" + clean[4:]

    # Validasi format akhir
    if not clean.startswith("628"):
        return None
    if len(clean) < 10 or len(clean) > 15:
        return None

    return clean


def _get_users_list() -> List[Json]:
    """
    AuthInstance.refresh_tokens di kode lama kelihatan dipakai sebagai data,
    tapi bisa saja berupa method / property / list.
    Fungsi ini bikin aksesnya aman & konsisten.
    """
    users = None
    try:
        users = getattr(AuthInstance, "refresh_tokens", None)
        if callable(users):
            users = users()  # type: ignore[misc]
    except Exception:
        users = None

    if isinstance(users, list):
        return [u for u in users if isinstance(u, dict)]
    return []


def _get_active_info(users: List[Json]) -> Tuple[Optional[Json], str]:
    active_user = None
    active_number = ""

    try:
        active_user = AuthInstance.get_active_user()
    except Exception:
        active_user = None

    if isinstance(active_user, dict):
        active_number = _safe_str(active_user.get("number"), "")
    else:
        # fallback: cari flag di list user kalau ada
        for u in users:
            if u.get("is_active") is True:
                active_user = u
                active_number = _safe_str(u.get("number"), "")
                break

    return active_user if isinstance(active_user, dict) else None, active_number


# =============================================================================
# CORE LOGIN
# =============================================================================

def login_prompt(api_key: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Menangani alur login lengkap:
    Input Nomor -> Request OTP -> Submit OTP.
    Return: (phone_number_628, refresh_token)
    """
    clear_screen()
    print("=" * 50)
    print("LOGIN SYSTEM - MYXL".center(50))
    print("=" * 50)

    try:
        raw = input("Masukkan Nomor XL (misal: 0812...): ").strip()
        phone_62 = validate_phone_number(raw)

        if not phone_62:
            print("❌ Format nomor tidak valid. Contoh valid: 0812xxxx / +62 812xxxx / 62812xxxx")
            return None, None

        # Request OTP
        print(f"\n🔄 Meminta kode OTP untuk {_format_phone_display(phone_62)} ...")
        try:
            subscriber_id = get_otp(phone_62)
            if not subscriber_id:
                print("❌ Gagal meminta OTP. Coba lagi nanti.")
                return None, None
        except Exception as e:
            logger.exception("Error requesting OTP")
            print("❌ Terjadi kesalahan koneksi saat meminta OTP.")
            print(f"   Detail: {_safe_str(e)}")
            return None, None

        print("✅ OTP berhasil dikirim via SMS.")
        print("-" * 50)

        # Submit OTP (Retry)
        max_retries = 3
        for attempt in range(max_retries):
            remaining = max_retries - attempt
            otp = input(f"Masukkan 6 digit OTP ({remaining} kesempatan): ").strip()

            if not (otp.isdigit() and len(otp) == 6):
                print("⚠️ OTP harus berupa 6 digit angka.")
                continue

            print("🔄 Memverifikasi...")

            try:
                tokens = submit_otp(api_key, "SMS", phone_62, otp)
            except Exception as e:
                logger.exception("Error submitting OTP")
                print("❌ Gagal verifikasi OTP karena error koneksi.")
                print(f"   Detail: {_safe_str(e)}")
                continue

            if isinstance(tokens, dict) and tokens.get("refresh_token"):
                print("\n✅ LOGIN BERHASIL!")
                return phone_62, _safe_str(tokens.get("refresh_token"))

            print("❌ Kode OTP salah atau token tidak valid.")

        print("\n⛔ Gagal login: kesempatan habis.")
        return None, None

    except KeyboardInterrupt:
        print("\n⚠️ Login dibatalkan.")
        return None, None
    except Exception as e:
        logger.exception("Critical Login Error")
        print("\n❌ Sistem error saat login.")
        print(f"   Detail: {_safe_str(e)}")
        return None, None


# =============================================================================
# ACCOUNT MENU
# =============================================================================

def show_account_menu() -> Optional[str]:
    """
    Menu Manajemen Akun Interaktif.
    Return nomor aktif (string) atau None.
    """
    # Initial Load
    try:
        AuthInstance.load_tokens()
    except Exception:
        logger.exception("AuthInstance.load_tokens failed")

    while True:
        clear_screen()

        users = _get_users_list()
        _, active_number = _get_active_info(users)

        print("=" * WIDTH)
        print("MANAJEMEN AKUN".center(WIDTH))
        print("=" * WIDTH)

        if not users:
            print("   [ ⚠️  BELUM ADA AKUN TERSIMPAN ]")
            print("   Silahkan tambah akun terlebih dahulu.")
        else:
            print(f"{'NO':<4} | {'NOMOR':<16} | {'STATUS':<10} | {'TIPE'}")
            print("-" * WIDTH)

            for idx, user in enumerate(users):
                u_num = _safe_str(user.get("number", ""))
                is_active = (u_num == active_number)

                marker = "🟢 AKTIF" if is_active else "⚪"
                sub_type = _safe_str(user.get("subscription_type", "PREPAID"))[:8] or "PREPAID"

                print(f"{idx + 1:<4} | {u_num:<16} | {marker:<10} | {sub_type}")

        print("=" * WIDTH)
        print("PERINTAH:")
        print(" [0]      Tambah Akun Baru")
        print(" [1-99]   Pilih/Ganti Akun (sesuai nomor urut)")
        print(" [del X]  Hapus Akun nomor urut X (contoh: del 1)")
        print(" [00]     Kembali ke Menu Utama")
        print("-" * WIDTH)

        choice = input("Pilihan >> ").strip().lower()

        # --- LOGIC HANDLER ---

        if choice == "00":
            return active_number if active_number else None

        if choice == "0":
            # Add New Account
            api_key = _safe_str(getattr(AuthInstance, "api_key", ""), "")
            res_number, res_token = login_prompt(api_key)

            if res_number and res_token:
                # Simpan biasanya minta int, tapi nomor bisa panjang -> tetap coba aman
                num_int = _safe_int(res_number)
                try:
                    if num_int is not None:
                        AuthInstance.add_refresh_token(num_int, res_token)
                        AuthInstance.set_active_user(num_int)
                    else:
                        # fallback jika implementasi menerima string
                        AuthInstance.add_refresh_token(res_number, res_token)  # type: ignore[arg-type]
                        AuthInstance.set_active_user(res_number)  # type: ignore[arg-type]
                    print(f"✅ Akun {_format_phone_display(res_number)} berhasil ditambahkan dan diaktifkan.")
                except Exception as e:
                    logger.exception("Failed to add/switch account")
                    print("❌ Gagal menyimpan akun.")
                    print(f"   Detail: {_safe_str(e)}")
                pause()
            else:
                pause()
            continue

        if choice.startswith("del"):
            # Delete Account
            try:
                parts = choice.split()
                if len(parts) != 2 or not parts[1].isdigit():
                    raise ValueError

                idx = int(parts[1]) - 1
                if idx < 0 or idx >= len(users):
                    print("❌ Nomor urut tidak valid.")
                    pause()
                    continue

                target_user = users[idx]
                target_num = _safe_str(target_user.get("number", ""), "")

                if not target_num:
                    print("❌ Data akun tidak valid.")
                    pause()
                    continue

                # Prevent deleting active user
                if target_num == active_number:
                    print("⚠️  TIDAK BISA MENGHAPUS AKUN YANG SEDANG AKTIF!")
                    print("    Silahkan ganti ke akun lain terlebih dahulu.")
                    pause()
                    continue

                confirm = input(f"❓ Hapus akun {target_num}? (y/n): ").strip().lower()
                if confirm == "y":
                    try:
                        # remove bisa terima int atau string
                        num_int = _safe_int(target_num)
                        if num_int is not None:
                            AuthInstance.remove_refresh_token(num_int)
                        else:
                            AuthInstance.remove_refresh_token(target_num)  # type: ignore[arg-type]
                        print("🗑️  Akun berhasil dihapus.")
                    except Exception as e:
                        logger.exception("Failed to remove account")
                        print("❌ Gagal menghapus akun.")
                        print(f"   Detail: {_safe_str(e)}")
                else:
                    print("Pembatalan.")

            except ValueError:
                print("❌ Format salah. Gunakan: del <nomor_urut>")
            pause()
            continue

        if choice.isdigit():
            # Switch Account
            idx = int(choice) - 1
            if 0 <= idx < len(users):
                target_user = users[idx]
                target_num = _safe_str(target_user.get("number", ""), "")

                if not target_num:
                    print("❌ Data akun tidak valid.")
                    pause()
                    continue

                if target_num == active_number:
                    print("ℹ️  Akun ini sudah aktif.")
                    pause()
                    continue

                try:
                    num_int = _safe_int(target_num)
                    success = AuthInstance.set_active_user(num_int if num_int is not None else target_num)  # type: ignore[arg-type]
                    if success:
                        print(f"✅ Berhasil beralih ke akun {target_num}")
                    else:
                        print("❌ Gagal beralih akun. Coba login ulang.")
                except Exception as e:
                    logger.exception("Failed to switch account")
                    print("❌ Error saat beralih akun.")
                    print(f"   Detail: {_safe_str(e)}")

            else:
                print("❌ Nomor urut tidak ditemukan.")
            pause()
            continue

        print("❌ Perintah tidak dikenali.")
        pause()


# =============================================================================
# LEGACY COMPATIBILITY
# =============================================================================

def show_login_menu() -> None:
    """Wrapper untuk kompatibilitas jika masih ada yang memanggil fungsi ini."""
    api_key = _safe_str(getattr(AuthInstance, "api_key", ""), "")
    login_prompt(api_key)