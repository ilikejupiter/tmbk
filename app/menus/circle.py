from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Import Internal Modules
from app.menus.package import get_packages_by_family, show_package_details
from app.menus.util import pause, clear_screen, format_quota_byte
from app.client.circle import (
    get_group_data,
    get_group_members,
    create_circle,
    validate_circle_member,
    invite_circle_member,
    remove_circle_member,
    accept_circle_invitation,
    spending_tracker,
    get_bonus_data,
)
from app.service.auth import AuthInstance
from app.client.encrypt import decrypt_circle_msisdn

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

WIDTH = 65

Json = Dict[str, Any]
Tokens = Mapping[str, Any]


# =============================================================================
# HELPERS
# =============================================================================

def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    try:
        return str(val)
    except Exception:
        return default


def _status_ok(res: Any) -> bool:
    return isinstance(res, dict) and _safe_str(res.get("status", "")).upper() == "SUCCESS"


def _get_dict(res: Any, *keys: str) -> Dict[str, Any]:
    cur: Any = res
    for k in keys:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(k)
    return cur if isinstance(cur, dict) else {}


def _get_list(res: Any, *keys: str) -> List[Any]:
    cur: Any = res
    for k in keys:
        if not isinstance(cur, dict):
            return []
        cur = cur.get(k)
    return cur if isinstance(cur, list) else []


def _format_date(ts: Any) -> str:
    """Helper format tanggal aman (supports seconds/millis)."""
    try:
        if ts is None or ts == "":
            return "N/A"
        if isinstance(ts, str):
            digits = "".join(ch for ch in ts if ch.isdigit())
            ts = int(digits) if digits else 0
        if isinstance(ts, float):
            ts = int(ts)
        if not isinstance(ts, int) or ts <= 0:
            return "N/A"
        if ts > 1_000_000_000_000:  # millis
            ts = ts // 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return "N/A"


def _normalize_msisdn(user_input: str) -> Optional[str]:
    """
    Normalisasi nomor ke format 628xxx.
    Menerima 08xxx / 8xxx / +62xxx / 62xxx / 0062xxx.
    """
    s = re.sub(r"\D", "", user_input or "").strip()
    if not s:
        return None

    if s.startswith("08"):
        s = "62" + s[1:]
    elif s.startswith("8"):
        s = "62" + s
    elif s.startswith("0062"):
        s = "62" + s[4:]
    elif s.startswith("6208"):
        s = "62" + s[3:]

    if not s.startswith("628"):
        return None
    if len(s) < 10 or len(s) > 15:
        return None
    return s


def _confirm(prompt: str) -> bool:
    return input(prompt).strip().lower() in {"y", "yes"}


def _decrypt_msisdn(api_key: str, encrypted: Any) -> str:
    """Helper dekripsi nomor dengan fallback aman."""
    try:
        enc = _safe_str(encrypted, "").strip()
        if not enc:
            return "<Empty>"
        plain = decrypt_circle_msisdn(api_key, enc)
        # beberapa implementasi bisa return None/"" -> disamarkan
        return _safe_str(plain, "<Hidden>") or "<Hidden>"
    except Exception:
        return "<Error>"


def _call_get_packages_by_family(family_code: str, is_enterprise: bool = False, text_search: str = "") -> None:
    """
    Wrapper aman buat kompatibilitas signature:
    - get_packages_by_family(family_code)
    - get_packages_by_family(family_code, is_enterprise)
    - get_packages_by_family(family_code, is_enterprise, text_search)
    """
    try:
        get_packages_by_family(family_code, is_enterprise, text_search)  # type: ignore[misc]
    except TypeError:
        try:
            get_packages_by_family(family_code, is_enterprise)  # type: ignore[misc]
        except TypeError:
            get_packages_by_family(family_code)  # type: ignore[misc]


def _mask_json_for_display(obj: Any, max_len: int = 2000) -> str:
    """
    Hindari print response terlalu besar / bocor data sensitif.
    """
    try:
        text = json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        text = _safe_str(obj, "")
    if len(text) > max_len:
        return text[:max_len] + "\n... (truncated)"
    return text


def _normalize_me_number() -> str:
    """
    Nomor user aktif dari AuthInstance untuk highlight di list member.
    """
    try:
        u = AuthInstance.get_active_user()
        n = _safe_str(u.get("number", "") if isinstance(u, dict) else "", "")
        return re.sub(r"\D", "", n)
    except Exception:
        return ""


# =============================================================================
# CORE MENUS
# =============================================================================

def show_circle_creation(api_key: str, tokens: dict) -> None:
    """Menu pembuatan Circle baru."""
    clear_screen()
    print("=" * WIDTH)
    print("🛠️  BUAT CIRCLE BARU".center(WIDTH))
    print("=" * WIDTH)

    try:
        parent_name = input("Nama Anda (Owner): ").strip()
        group_name = input("Nama Circle: ").strip()
        member_msisdn_raw = input("Nomor Anggota Pertama (contoh 0812/628..): ").strip()
        member_name = input("Nama Anggota Pertama (opsional): ").strip()

        member_msisdn = _normalize_msisdn(member_msisdn_raw)

        if not parent_name or not group_name or not member_msisdn:
            print("❌ Data tidak valid. Pastikan nama, nama circle, dan nomor anggota benar.")
            pause()
            return

        print("\n⏳ Membuat Circle...")
        res = create_circle(api_key, tokens, parent_name, group_name, member_msisdn, member_name)

        if _status_ok(res):
            print("✅ Circle berhasil dibuat!")
            print(_mask_json_for_display(res))
        else:
            msg = _safe_str(res.get("message", "Unknown error") if isinstance(res, dict) else "Unknown error")
            print(f"❌ Gagal membuat Circle: {msg}")

    except KeyboardInterrupt:
        print("\n⚠️ Dibatalin.")
    except Exception as e:
        logger.exception("Create circle error")
        print(f"❌ Error: {_safe_str(e)}")

    pause()


def show_bonus_list(api_key: str, tokens: dict, parent_subs_id: str, group_id: str) -> None:
    """Menu daftar bonus Circle."""
    while True:
        clear_screen()
        print("=" * WIDTH)
        print("🎁  CIRCLE BONUS".center(WIDTH))
        print("=" * WIDTH)
        print("⏳ Mengambil data bonus...", end="\r")

        try:
            res = get_bonus_data(api_key, tokens, parent_subs_id, group_id)
        except Exception as e:
            logger.exception("get_bonus_data failed")
            print(" " * WIDTH, end="\r")
            print(f"❌ Gagal mengambil data bonus: {_safe_str(e)}")
            pause()
            return

        if not _status_ok(res):
            print(" " * WIDTH, end="\r")
            msg = _safe_str(res.get("message", "Unknown error") if isinstance(res, dict) else "Unknown error")
            print(f"❌ Gagal mengambil data bonus: {msg}")
            pause()
            return

        bonuses = _get_list(res, "data", "bonuses")
        if not bonuses:
            print(" " * WIDTH, end="\r")
            print("📭 Tidak ada bonus tersedia.")
            pause()
            return

        print(" " * WIDTH, end="\r")

        selection_map: Dict[int, Dict[str, Any]] = {}

        for idx, bonus in enumerate(bonuses, 1):
            if not isinstance(bonus, dict):
                continue
            name = _safe_str(bonus.get("name", "Bonus"))[:30]
            b_type = _safe_str(bonus.get("bonus_type", "General"))[:12]
            selection_map[idx] = bonus
            print(f"{idx:<2}. {name:<32} | {b_type:<12}")

        print("-" * WIDTH)
        print("[No] Pilih Bonus")
        print("[00] Kembali")
        print("=" * WIDTH)

        choice = input("Pilihan >> ").strip()

        if choice == "00":
            return

        if not choice.isdigit():
            print("⚠️ Input salah.")
            pause()
            continue

        pick = int(choice)
        bonus = selection_map.get(pick)
        if not bonus:
            print("⚠️ Nomor tidak valid.")
            pause()
            continue

        act_type = _safe_str(bonus.get("action_type", "UNK")).upper()
        act_param = _safe_str(bonus.get("action_param", "")).strip()

        if not act_param:
            print("❌ Bonus ini tidak punya parameter aksi.")
            pause()
            continue

        if act_type == "PLP":
            # biasanya act_param adalah family_code
            _call_get_packages_by_family(act_param, False, "")
        elif act_type == "PDP":
            show_package_details(api_key, tokens, act_param, False)
        else:
            print(f"⚠️ Tipe aksi tidak didukung: {act_type}")
            pause()


def show_circle_info(api_key: str, tokens: dict) -> None:
    """
    Menu Utama Manajemen Circle.
    """
    my_number = _normalize_me_number()

    while True:
        clear_screen()
        print("=" * WIDTH)
        print("⭕  CIRCLE MANAGER".center(WIDTH))
        print("=" * WIDTH)
        print("⏳ Mengambil data circle...", end="\r")

        # 1) Fetch Group Data
        try:
            group_res = get_group_data(api_key, tokens)
        except Exception as e:
            logger.exception("get_group_data failed")
            print(" " * WIDTH, end="\r")
            print(f"\n❌ Gagal mengambil data Circle: {_safe_str(e)}")
            pause()
            return

        if not _status_ok(group_res):
            print(" " * WIDTH, end="\r")
            msg = _safe_str(group_res.get("message", "Unknown error") if isinstance(group_res, dict) else "Unknown error")
            print(f"\n❌ Gagal mengambil data Circle: {msg}")
            pause()
            return

        group_data = _get_dict(group_res, "data")
        group_id = _safe_str(group_data.get("group_id", "")).strip()

        # Case: No Circle
        if not group_id:
            print(" " * WIDTH, end="\r")
            print("\n   [ Anda belum tergabung dalam Circle ]")
            print("\n   1. Buat Circle Baru")
            print("   0. Kembali")

            ch = input("\n   Pilihan >> ").strip()
            if ch == "1":
                show_circle_creation(api_key, tokens)
                continue
            return

        # Case: Blocked
        if _safe_str(group_data.get("group_status", "")).upper() == "BLOCKED":
            print(" " * WIDTH, end="\r")
            print("\n⛔ Circle ini sedang DIBLOKIR.")
            pause()
            return

        # 2) Fetch Members (dan package info)
        try:
            members_res = get_group_members(api_key, tokens, group_id)
        except Exception as e:
            logger.exception("get_group_members failed")
            print(" " * WIDTH, end="\r")
            print(f"\n❌ Gagal mengambil daftar anggota: {_safe_str(e)}")
            pause()
            return

        if not _status_ok(members_res):
            print(" " * WIDTH, end="\r")
            msg = _safe_str(members_res.get("message", "Unknown error") if isinstance(members_res, dict) else "Unknown error")
            print(f"\n❌ Gagal mengambil daftar anggota: {msg}")
            pause()
            return

        mem_data = _get_dict(members_res, "data")
        members = mem_data.get("members", [])
        if not isinstance(members, list):
            members = []
        package_info = mem_data.get("package", {})
        if not isinstance(package_info, dict):
            package_info = {}

        # Cari Parent Info
        parent_info = next((m for m in members if isinstance(m, dict) and m.get("member_role") == "PARENT"), {}) or {}
        parent_subs_id = _safe_str(parent_info.get("subscriber_number", "")).strip()
        parent_msisdn = _decrypt_msisdn(api_key, parent_info.get("msisdn", ""))
        parent_member_id = _safe_str(parent_info.get("member_id", "")).strip()

        # Spending (jangan sampai crash kalau parent_subs_id kosong)
        spend_data: Dict[str, Any] = {}
        if parent_subs_id:
            try:
                spend_res = spending_tracker(api_key, tokens, parent_subs_id, group_id)
                if _status_ok(spend_res):
                    spend_data = _get_dict(spend_res, "data")
            except Exception:
                logger.debug("spending_tracker failed", exc_info=True)

        # Render Header
        print(" " * WIDTH, end="\r")

        g_name = _safe_str(group_data.get("group_name", "Unknown"))
        pkg_name = _safe_str(package_info.get("name", "No Package"))

        benefit = package_info.get("benefit", {})
        if not isinstance(benefit, dict):
            benefit = {}

        rem_q = format_quota_byte(benefit.get("remaining", 0))
        tot_q = format_quota_byte(benefit.get("allocation", 0))

        spend_curr = spend_data.get("spend", 0) or 0
        spend_tgt = spend_data.get("target", 0) or 0

        try:
            spend_curr_i = int(spend_curr)
        except Exception:
            spend_curr_i = 0
        try:
            spend_tgt_i = int(spend_tgt)
        except Exception:
            spend_tgt_i = 0

        print(f" Nama Circle : {g_name}")
        print(f" Owner       : {parent_msisdn}")
        print(f" Paket       : {pkg_name}")
        print(f" Sisa Kuota  : {rem_q} / {tot_q}")
        print(f" Spending    : Rp {spend_curr_i:,} / Rp {spend_tgt_i:,}")
        print("-" * WIDTH)

        # Render Members
        print(f"{'NO':<3} | {'NOMOR':<14} | {'ROLE':<14} | {'STATUS'}")
        print("-" * WIDTH)

        # Untuk highlight "You", bandingkan digit saja (karena hasil decrypt bisa 0812.. atau 628.. tergantung)
        def _digits(x: str) -> str:
            return re.sub(r"\D", "", x or "")

        for i, m in enumerate(members, 1):
            if not isinstance(m, dict):
                continue

            num = _decrypt_msisdn(api_key, m.get("msisdn", ""))
            role = "👑 OWNER" if m.get("member_role") == "PARENT" else "👤 MEMBER"
            status = _safe_str(m.get("status", "ACTIVE"))

            if my_number and _digits(num).endswith(my_number[-8:]):  # match longgar (8 digit terakhir)
                role += " (You)"

            print(f" {i:<2} | {num:<14} | {role:<14} | {status}")

        print("=" * WIDTH)
        print("COMMANDS:")
        print(" [1]      Undang Anggota (Invite)")
        print(" [2]      Lihat Bonus Circle")
        print(" [del X]  Hapus Anggota No. X")
        print(" [acc X]  Terima Undangan Anggota No. X")
        print(" [00]     Kembali")
        print("-" * WIDTH)

        choice = input("Pilihan >> ").strip().lower()

        if choice == "00":
            return
        if choice == "1":
            _handle_invite(api_key, tokens, group_id, parent_member_id)
            continue
        if choice == "2":
            show_bonus_list(api_key, tokens, parent_subs_id, group_id)
            continue
        if choice.startswith("del "):
            _handle_remove(api_key, tokens, members, group_id, parent_member_id, choice)
            continue
        if choice.startswith("acc "):
            _handle_accept(api_key, tokens, members, group_id, choice)
            continue

        print("⚠️ Perintah tidak valid.")
        pause()


# =============================================================================
# ACTION HANDLERS
# =============================================================================

def _handle_invite(api_key: str, tokens: dict, group_id: str, parent_id: str) -> None:
    target_raw = input("Nomor Tujuan (contoh 0812/628..): ").strip()
    target = _normalize_msisdn(target_raw)
    name = input("Nama Anggota (opsional): ").strip()

    if not target:
        print("❌ Nomor tidak valid.")
        pause()
        return

    if not group_id or not parent_id:
        print("❌ Data circle tidak lengkap (group/parent id).")
        pause()
        return

    print("⏳ Memvalidasi...")
    try:
        val = validate_circle_member(api_key, tokens, target)
    except Exception as e:
        logger.exception("validate_circle_member failed")
        print(f"❌ Gagal validasi: {_safe_str(e)}")
        pause()
        return

    # Cek eligibility (mengikuti logika original)
    response_code = _safe_str(_get_dict(val, "data").get("response_code", ""))
    if response_code != "200-2001":
        msg = _safe_str(_get_dict(val, "data").get("message", "Tidak memenuhi syarat"))
        print(f"❌ Gagal: {msg}")
        pause()
        return

    print("⏳ Mengirim undangan...")
    try:
        res = invite_circle_member(api_key, tokens, target, name, group_id, parent_id)
    except Exception as e:
        logger.exception("invite_circle_member failed")
        print(f"❌ Gagal mengirim undangan: {_safe_str(e)}")
        pause()
        return

    if _status_ok(res):
        print("✅ Undangan terkirim!")
    else:
        msg = _safe_str(res.get("message", "Unknown error") if isinstance(res, dict) else "Unknown error")
        print(f"❌ Gagal: {msg}")
    pause()


def _handle_remove(api_key: str, tokens: dict, members: List[Any], group_id: str, parent_id: str, cmd: str) -> None:
    try:
        parts = cmd.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise ValueError

        idx = int(parts[1]) - 1
        if not (0 <= idx < len(members)):
            raise ValueError

        target = members[idx]
        if not isinstance(target, dict):
            raise ValueError

        if _safe_str(target.get("member_role", "")).upper() == "PARENT":
            print("❌ Tidak bisa menghapus Owner.")
            pause()
            return

        # minimal circle: owner + 1 member (2 total). Kalau sudah 2 jangan bisa hapus lagi.
        if len([m for m in members if isinstance(m, dict)]) <= 2:
            print("❌ Minimal 2 anggota dalam Circle (Owner + 1 Member).")
            pause()
            return

        member_id = _safe_str(target.get("member_id", "")).strip()
        if not member_id or not group_id or not parent_id:
            print("❌ Data tidak lengkap untuk menghapus anggota.")
            pause()
            return

        num = _decrypt_msisdn(api_key, target.get("msisdn", ""))

        if not _confirm(f"❓ Hapus {num}? (y/n): "):
            print("Batal.")
            pause()
            return

        res = remove_circle_member(api_key, tokens, member_id, group_id, parent_id, False)
        if _status_ok(res):
            print("✅ Anggota dihapus.")
        else:
            msg = _safe_str(res.get("message", "Unknown error") if isinstance(res, dict) else "Unknown error")
            print(f"❌ Gagal: {msg}")
        pause()

    except ValueError:
        print("❌ Format salah. Gunakan: del <nomor_urut>")
        pause()
    except Exception as e:
        logger.exception("remove handler error")
        print(f"❌ Error: {_safe_str(e)}")
        pause()


def _handle_accept(api_key: str, tokens: dict, members: List[Any], group_id: str, cmd: str) -> None:
    try:
        parts = cmd.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise ValueError

        idx = int(parts[1]) - 1
        if not (0 <= idx < len(members)):
            raise ValueError

        target = members[idx]
        if not isinstance(target, dict):
            raise ValueError

        status = _safe_str(target.get("status", "")).upper()
        if status != "INVITED":
            print("⚠️ Member ini tidak dalam status INVITED.")
            pause()
            return

        member_id = _safe_str(target.get("member_id", "")).strip()
        if not member_id or not group_id:
            print("❌ Data tidak lengkap untuk menerima undangan.")
            pause()
            return

        if not _confirm("❓ Terima undangan ini? (y/n): "):
            print("Batal.")
            pause()
            return

        res = accept_circle_invitation(api_key, tokens, group_id, member_id)
        if _status_ok(res):
            print("✅ Undangan diterima.")
        else:
            msg = _safe_str(res.get("message", "Unknown error") if isinstance(res, dict) else "Unknown error")
            print(f"❌ Gagal: {msg}")
        pause()

    except ValueError:
        print("❌ Format salah. Gunakan: acc <nomor_urut>")
        pause()
    except Exception as e:
        logger.exception("accept handler error")
        print(f"❌ Error: {_safe_str(e)}")
        pause()