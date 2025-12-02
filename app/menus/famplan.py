from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# Import Dependencies
from app.menus.util import pause, clear_screen, format_quota_byte
from app.client.famplan import (
    get_family_data,
    change_member,
    remove_member,
    set_quota_limit,
    validate_msisdn,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

WIDTH = 65
Json = Dict[str, Any]


# =============================================================================
# Helpers (safe parsing)
# =============================================================================

def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    try:
        return str(val)
    except Exception:
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        if isinstance(val, int):
            return val
        if isinstance(val, float):
            return int(val)
        if isinstance(val, str):
            digits = "".join(ch for ch in val if ch.isdigit())
            return int(digits) if digits else default
    except Exception:
        pass
    return default


def _get_dict(obj: Any, *keys: str) -> Json:
    cur: Any = obj
    for k in keys:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(k)
    return cur if isinstance(cur, dict) else {}


def _get_list(obj: Any, *keys: str) -> List[Any]:
    cur: Any = obj
    for k in keys:
        if not isinstance(cur, dict):
            return []
        cur = cur.get(k)
    return cur if isinstance(cur, list) else []


def _status_is_success(res: Any) -> bool:
    if not isinstance(res, dict):
        return False
    return _safe_str(res.get("status", "")).strip().upper() == "SUCCESS"


def _format_date(timestamp: Any) -> str:
    """Format tanggal aman (support seconds/millis/string)."""
    try:
        if not timestamp:
            return "-"
        ts = timestamp
        if isinstance(ts, str):
            ts = _safe_int(ts, 0)
        if isinstance(ts, float):
            ts = int(ts)
        if not isinstance(ts, int) or ts <= 0:
            return "-"
        # millis -> seconds
        if ts > 1_000_000_000_000:
            ts //= 1000
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%d %b %Y")
    except Exception:
        return "-"


def _slot_status(member: Json) -> str:
    return "🟢 TERISI" if _safe_str(member.get("msisdn", "")).strip() else "⚪ KOSONG"


def _confirm(prompt: str) -> bool:
    return input(prompt).strip().lower() in {"y", "yes"}


def _normalize_msisdn_62(raw: str) -> str:
    """
    Normalisasi ringan ke format '628xxxx' jika user ngasih '08xxxx' atau '+62xxxx'.
    Tidak terlalu keras agar tetap sesuai validate_msisdn() di backend.
    """
    s = "".join(ch for ch in (raw or "").strip() if ch.isdigit())
    if s.startswith("08"):
        s = "62" + s[1:]
    elif s.startswith("8"):
        s = "62" + s
    elif s.startswith("0062"):
        s = "62" + s[4:]
    return s


# =============================================================================
# Actions
# =============================================================================

def _handle_change_member(api_key: str, tokens: dict, members: List[Json]) -> None:
    """Tambah anggota ke slot kosong (sesuai perilaku original)."""
    try:
        if not members:
            print("❌ Tidak ada slot member.")
            return

        slot_raw = input("\nMasukkan Nomor Slot: ").strip()
        slot_idx = _safe_int(slot_raw, -1)

        if slot_idx < 1 or slot_idx > len(members):
            print("❌ Nomor slot tidak valid.")
            return

        member = members[slot_idx - 1]
        if _safe_str(member.get("msisdn", "")).strip():
            print("⚠️  Slot ini sudah terisi. Hapus anggota dulu jika ingin mengganti.")
            return

        target_msisdn = _normalize_msisdn_62(input("Masukkan Nomor Baru (contoh 0812/628...): ").strip())
        parent_alias = input("Alias Anda (Parent): ").strip() or "Admin"
        child_alias = input("Alias Anggota Baru: ").strip() or "Member"

        if not target_msisdn:
            print("❌ Nomor tidak boleh kosong.")
            return

        # Validasi MSISDN
        print("⏳ Memvalidasi nomor...")
        val_res = validate_msisdn(api_key, tokens, target_msisdn)

        if not isinstance(val_res, dict):
            print("❌ Gagal validasi: response tidak valid.")
            return

        if _safe_str(val_res.get("status", "")).strip().lower() != "success":
            print(f"❌ Nomor tidak valid: {_safe_str(val_res.get('message', 'Unknown error'))}")
            return

        role = _safe_str(_get_dict(val_res, "data").get("family_plan_role", "")).strip()
        if role and role != "NO_ROLE":
            print(f"⚠️  Nomor ini sudah terdaftar di paket keluarga lain (Role: {role}).")
            return

        slot_id = _safe_str(member.get("slot_id", "")).strip()
        family_member_id = _safe_str(member.get("family_member_id", "")).strip()
        if not slot_id or not family_member_id:
            print("❌ Data slot tidak lengkap (slot_id/family_member_id kosong).")
            return

        if not _confirm(f"❓ Tambahkan {target_msisdn} ke Slot {slot_idx}? (y/n): "):
            return

        print("⏳ Memproses penambahan...")
        res = change_member(
            api_key,
            tokens,
            parent_alias,
            child_alias,
            slot_id,
            family_member_id,
            target_msisdn,
        )

        if _status_is_success(res):
            print("✅ Berhasil menambahkan anggota!")
        else:
            msg = _safe_str(res.get("message", "Unknown error") if isinstance(res, dict) else "Unknown error")
            print(f"❌ Gagal: {msg}")

    except Exception as e:
        logger.exception("Change member error")
        print(f"❌ Terjadi kesalahan: {_safe_str(e)}")


def _handle_remove_member(api_key: str, tokens: dict, members: List[Json]) -> None:
    """Hapus anggota dari slot yang terisi."""
    try:
        if not members:
            print("❌ Tidak ada slot member.")
            return

        slot_raw = input("\nMasukkan Nomor Slot yang akan DIHAPUS: ").strip()
        slot_idx = _safe_int(slot_raw, -1)

        if slot_idx < 1 or slot_idx > len(members):
            print("❌ Nomor slot tidak valid.")
            return

        member = members[slot_idx - 1]
        msisdn = _safe_str(member.get("msisdn", "")).strip()
        if not msisdn:
            print("⚠️  Slot ini sudah kosong.")
            return

        family_member_id = _safe_str(member.get("family_member_id", "")).strip()
        if not family_member_id:
            print("❌ Data member tidak lengkap (family_member_id kosong).")
            return

        if not _confirm(f"❓ Yakin HAPUS {msisdn} dari Slot {slot_idx}? (y/n): "):
            return

        print("⏳ Memproses penghapusan...")
        res = remove_member(api_key, tokens, family_member_id)

        if _status_is_success(res):
            print("✅ Anggota berhasil dihapus.")
        else:
            msg = _safe_str(res.get("message", "Unknown error") if isinstance(res, dict) else "Unknown error")
            print(f"❌ Gagal: {msg}")

    except Exception as e:
        logger.exception("Remove member error")
        print(f"❌ Error: {_safe_str(e)}")


def _handle_set_limit(api_key: str, tokens: dict, members: List[Json]) -> None:
    """Atur batas kuota (limit) untuk anggota terisi."""
    try:
        if not members:
            print("❌ Tidak ada slot member.")
            return

        slot_raw = input("\nMasukkan Nomor Slot: ").strip()
        slot_idx = _safe_int(slot_raw, -1)

        if slot_idx < 1 or slot_idx > len(members):
            print("❌ Nomor slot tidak valid.")
            return

        member = members[slot_idx - 1]
        msisdn = _safe_str(member.get("msisdn", "")).strip()
        if not msisdn:
            print("⚠️  Slot kosong, tidak bisa atur limit.")
            return

        limit_raw = input("Masukkan Batas Kuota (MB): ").strip()
        limit_mb = _safe_int(limit_raw, -1)
        if limit_mb <= 0:
            print("❌ Limit harus angka > 0.")
            return

        limit_bytes = limit_mb * 1024 * 1024
        usage = member.get("usage") if isinstance(member.get("usage"), dict) else {}
        current_alloc = _safe_int(usage.get("quota_allocated", 0), 0)

        family_member_id = _safe_str(member.get("family_member_id", "")).strip()
        if not family_member_id:
            print("❌ Data member tidak lengkap (family_member_id kosong).")
            return

        print(f"⏳ Mengubah limit dari {format_quota_byte(current_alloc)} ke {format_quota_byte(limit_bytes)}...")

        res = set_quota_limit(api_key, tokens, current_alloc, limit_bytes, family_member_id)

        if _status_is_success(res):
            print("✅ Limit kuota berhasil diubah.")
        else:
            msg = _safe_str(res.get("message", "Unknown error") if isinstance(res, dict) else "Unknown error")
            print(f"❌ Gagal: {msg}")

    except Exception as e:
        logger.exception("Set limit error")
        print(f"❌ Error: {_safe_str(e)}")


# =============================================================================
# Main Menu
# =============================================================================

def show_family_info(api_key: str, tokens: dict) -> None:
    """
    Menu Manajemen Family Plan / Akrab.
    """
    while True:
        clear_screen()
        print("=" * WIDTH)
        print("👨‍👩‍👧‍👦  FAMILY PLAN MANAGER".center(WIDTH))
        print("=" * WIDTH)
        print("⏳ Mengambil data paket keluarga...", end="\r")

        try:
            res = get_family_data(api_key, tokens)
        except Exception as e:
            print(" " * WIDTH, end="\r")
            logger.exception("get_family_data failed")
            print(f"❌ Gagal mengambil data family plan: {_safe_str(e)}")
            pause()
            return

        data = _get_dict(res, "data")
        member_info = _get_dict(data, "member_info")
        if not member_info:
            print(" " * WIDTH, end="\r")
            print("❌ Gagal mengambil data family plan.")
            print("   Pastikan Anda sudah berlangganan paket Akrab.")
            pause()
            return

        plan_type = _safe_str(member_info.get("plan_type", "")).strip()
        if not plan_type:
            print(" " * WIDTH, end="\r")
            print("🚫 Anda bukan pengelola (Organizer) paket keluarga.")
            pause()
            return

        # Header info
        parent = _safe_str(member_info.get("parent_msisdn", "-"))
        total_q = format_quota_byte(member_info.get("total_quota", 0))
        rem_q = format_quota_byte(member_info.get("remaining_quota", 0))
        exp_date = _format_date(member_info.get("end_date", 0))

        members_any = member_info.get("members", [])
        members: List[Json] = [m for m in members_any if isinstance(m, dict)] if isinstance(members_any, list) else []

        print(" " * WIDTH, end="\r")
        print(f" 📦 Paket   : {plan_type}")
        print(f" 👑 Parent  : {parent}")
        print(f" 📊 Kuota   : {rem_q} / {total_q}")
        print(f" 📅 Expired : {exp_date}")
        print("-" * WIDTH)

        # Members table
        print(f"{'NO':<3} | {'NOMOR':<14} | {'STATUS':<10} | {'PEMAKAIAN':<20}")
        print("-" * WIDTH)

        for i, m in enumerate(members, start=1):
            num = _safe_str(m.get("msisdn", "")).strip() or "KOSONG"
            alias = _safe_str(m.get("alias", "-"))[:10]
            status_icon = "✅" if _safe_str(m.get("msisdn", "")).strip() else "⚪"

            usage = m.get("usage") if isinstance(m.get("usage"), dict) else {}
            used = format_quota_byte(usage.get("quota_used", 0))
            alloc = format_quota_byte(usage.get("quota_allocated", 0))
            print(f" {i:<2} | {num:<14} | {status_icon} {alias:<7} | {used} / {alloc}")

        print("-" * WIDTH)
        print("PERINTAH:")
        print(" [1] Tambah Anggota Baru (slot kosong)")
        print(" [2] Hapus Anggota")
        print(" [3] Atur Batas Kuota (Limit)")
        print(" [0] Kembali")
        print("=" * WIDTH)

        choice = input("Pilihan >> ").strip()

        if choice == "0":
            return
        elif choice == "1":
            _handle_change_member(api_key, tokens, members)
            pause()
        elif choice == "2":
            _handle_remove_member(api_key, tokens, members)
            pause()
        elif choice == "3":
            _handle_set_limit(api_key, tokens, members)
            pause()
        else:
            print("⚠️  Pilihan tidak valid.")
            pause()