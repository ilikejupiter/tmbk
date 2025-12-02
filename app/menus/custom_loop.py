from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Import Dependencies (internal project)
from app.client.engsel import get_family, get_package
from app.client.purchase.redeem import settlement_bounty
from app.service.auth import AuthInstance
from app.menus.util import clear_screen, pause


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

WIDTH = 60
Json = Dict[str, Any]


# =============================================================================
# Helpers
# =============================================================================

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
        if isinstance(val, float):
            return int(val)
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return default
            # allow "1,000" etc
            digits = "".join(ch for ch in s if ch.isdigit())
            return int(digits) if digits else default
    except Exception:
        return default
    return default


def _status_success(res: Any) -> bool:
    return isinstance(res, dict) and _safe_str(res.get("status"), "").upper() == "SUCCESS"


def _get_tokens_or_quit() -> Optional[dict]:
    """
    Attempt to get active tokens safely.
    Keep minimal retry (no bypassing server protection; just handles transient local state).
    """
    try:
        t = AuthInstance.get_active_tokens()
        if t:
            return t
        # one more attempt in case AuthInstance refreshes internally
        t = AuthInstance.get_active_tokens()
        return t or None
    except Exception:
        logger.exception("Failed to load active tokens")
        return None


def _parse_targets_input(choice: str, flattened: List["TargetOption"]) -> List["TargetOption"]:
    choice = (choice or "").strip().lower()
    if not choice:
        return []

    if choice in {"0", "00", "back", "exit", "q"}:
        return []

    if choice == "all":
        return list(flattened)

    # Allow "1,3, 7"
    indices: List[int] = []
    for part in choice.split(","):
        part = part.strip()
        if part.isdigit():
            indices.append(int(part))

    chosen: List[TargetOption] = []
    ids = {t.id for t in flattened}
    for idx in indices:
        if idx in ids:
            chosen.append(next(t for t in flattened if t.id == idx))
    return chosen


def _sleep_seconds(seconds: int) -> None:
    if seconds <= 0:
        return
    time.sleep(seconds)


# =============================================================================
# Data Structures
# =============================================================================

@dataclass(frozen=True)
class TargetOption:
    id: int
    code: str
    price: int
    name: str


# =============================================================================
# Main Menu
# =============================================================================

def show_custom_loop_menu() -> None:
    """
    SAFE Redeem Runner:
    - Select package options from a family code
    - Execute redeem with strong guards:
        * max_cycles: how many rounds over selected items
        * max_success: stop after N successes
        * delay_seconds: delay between operations to reduce load
        * dry_run: verify package detail/token without redeeming

    NOTE:
    This version intentionally avoids "infinite" looping or bypassing rate limits.
    """
    api_key = _safe_str(getattr(AuthInstance, "api_key", ""), "")
    if not api_key:
        print("❌ API key tidak tersedia.")
        pause()
        return

    clear_screen()
    print("=" * WIDTH)
    print("🔁 SAFE REDEEM RUNNER (LIMITED)".center(WIDTH))
    print("=" * WIDTH)

    family_code = input("Masukkan Family Code: ").strip()
    if not family_code:
        return

    tokens = _get_tokens_or_quit()
    if not tokens:
        print("❌ Sesi habis. Login ulang.")
        pause()
        return

    print("⏳ Mengambil daftar paket...", end="\r")
    try:
        family_data = get_family(api_key, tokens, family_code)
    except Exception as e:
        logger.exception("get_family failed")
        print(" " * WIDTH, end="\r")
        print(f"❌ Gagal mengambil data family: {_safe_str(e)}")
        pause()
        return

    if not isinstance(family_data, dict):
        print(" " * WIDTH, end="\r")
        print("❌ Response family tidak valid.")
        pause()
        return

    variants = family_data.get("package_variants", [])
    if not isinstance(variants, list) or not variants:
        print(" " * WIDTH, end="\r")
        print("❌ Tidak ada varian paket dalam family.")
        pause()
        return

    flattened: List[TargetOption] = []
    counter = 1

    clear_screen()
    print("=" * WIDTH)
    print(f"FAMILY: {family_code}".center(WIDTH))
    print("=" * WIDTH)
    print(f"{'NO':<4} | {'NAMA PAKET':<35} | {'HARGA'}")
    print("-" * WIDTH)

    for var in variants:
        if not isinstance(var, dict):
            continue
        options = var.get("package_options", [])
        if not isinstance(options, list):
            continue

        for opt in options:
            if not isinstance(opt, dict):
                continue

            code = _safe_str(opt.get("package_option_code"), "").strip()
            if not code:
                continue

            name = _safe_str(opt.get("name"), "Unknown").replace("\n", " ").strip()[:35]
            price = _safe_int(opt.get("price", 0), 0) or 0

            flattened.append(TargetOption(id=counter, code=code, price=price, name=name))
            print(f" {counter:<3} | {name:<35} | Rp {price:,}")
            counter += 1

    print("=" * WIDTH)
    print("INSTRUKSI:")
    print(" - Input: 1,3,7  (pilih beberapa)")
    print(" - Input: all    (pilih semua)")
    print(" - Input: 00     (kembali)")
    print("-" * WIDTH)

    choice = input("Pilihan >> ").strip().lower()
    if choice in {"00", "0", "back", "exit", "q"}:
        return

    targets = _parse_targets_input(choice, flattened)
    if not targets:
        print("❌ Tidak ada paket yang dipilih.")
        pause()
        return

    # --- Configuration (SAFE LIMITS) ---
    print("\n--- PENGATURAN (SAFE LIMITS) ---")
    delay_seconds = _safe_int(input("Delay antar item (detik) [Saran: 2-5]: ").strip(), 3)
    max_cycles = _safe_int(input("Maksimal putaran (cycle) [Saran: 1-3]: ").strip(), 1)
    max_success = _safe_int(input("Stop setelah berapa sukses? [Saran: 1-10]: ").strip(), 3)

    dry_run_in = input("Dry-run? (cek token saja, tidak redeem) [y/N]: ").strip().lower()
    dry_run = dry_run_in in {"y", "yes"}

    stop_on_fail_in = input("Berhenti jika ada gagal/error? [y/N]: ").strip().lower()
    stop_on_fail = stop_on_fail_in in {"y", "yes"}

    delay_seconds = int(delay_seconds or 0)
    max_cycles = max(1, int(max_cycles or 1))
    max_success = max(1, int(max_success or 1))

    clear_screen()
    print("=" * WIDTH)
    print("🚀 MENJALANKAN".center(WIDTH))
    print("=" * WIDTH)
    print(f"Mode      : {'DRY-RUN' if dry_run else 'EXECUTE'}")
    print(f"Targets   : {len(targets)} item")
    print(f"Max cycles: {max_cycles}")
    print(f"Max sukses: {max_success}")
    print(f"Delay     : {delay_seconds}s")
    print(f"Stop fail : {'Yes' if stop_on_fail else 'No'}")
    print("=" * WIDTH)

    total_success = 0
    total_fail = 0

    try:
        for cycle in range(1, max_cycles + 1):
            print(f"\n🔄 Cycle {cycle}/{max_cycles} — {datetime.now().strftime('%H:%M:%S')}")
            print("-" * WIDTH)

            current_tokens = _get_tokens_or_quit()
            if not current_tokens:
                print("❌ Token invalid. Berhenti.")
                break

            for t in targets:
                if total_success >= max_success:
                    print(f"\n✅ Target sukses tercapai ({total_success}/{max_success}). Stop.")
                    raise StopIteration

                print(f"🎁 Item: {t.name}")
                try:
                    # Always re-fetch package detail (token & timestamp can change)
                    pkg_detail = get_package(api_key, current_tokens, t.code)
                    if not isinstance(pkg_detail, dict):
                        print("   ⚠️ Detail paket tidak valid (skip).")
                        total_fail += 1
                        if stop_on_fail:
                            raise StopIteration
                        continue

                    token_conf = pkg_detail.get("token_confirmation")
                    ts_to_sign = pkg_detail.get("timestamp")

                    if not token_conf:
                        print("   ⚠️ Token konfirmasi kosong / tidak tersedia (skip).")
                        total_fail += 1
                        if stop_on_fail:
                            raise StopIteration
                        continue

                    if dry_run:
                        print("   ✅ DRY-RUN OK (token tersedia).")
                        _sleep_seconds(delay_seconds)
                        continue

                    res = settlement_bounty(
                        api_key=api_key,
                        tokens=current_tokens,
                        token_confirmation=token_conf,
                        ts_to_sign=ts_to_sign,
                        payment_target=t.code,
                        price=t.price,
                        item_name=t.name,
                    )

                    if _status_success(res):
                        print("   ✅ SUKSES!")
                        total_success += 1
                    else:
                        msg = _safe_str(res.get("message"), "Unknown Error") if isinstance(res, dict) else "No Response"
                        print(f"   ❌ GAGAL: {msg}")
                        total_fail += 1
                        if stop_on_fail:
                            raise StopIteration

                except StopIteration:
                    raise
                except Exception as e:
                    print(f"   ❌ ERROR: {_safe_str(e)}")
                    logger.exception("Redeem item failed")
                    total_fail += 1
                    if stop_on_fail:
                        raise StopIteration

                _sleep_seconds(delay_seconds)

            # Optional delay between cycles (reuse delay_seconds, no extra anti-spam logic)
            if cycle < max_cycles and delay_seconds > 0:
                print(f"\n⏳ Jeda antar cycle {delay_seconds}s...")
                _sleep_seconds(delay_seconds)

    except StopIteration:
        pass
    except KeyboardInterrupt:
        print("\n🛑 Dihentikan user (KeyboardInterrupt).")
    except Exception as e:
        print(f"\n❌ CRITICAL ERROR: {_safe_str(e)}")
        traceback.print_exc()
    finally:
        print("\n" + "=" * WIDTH)
        print("RINGKASAN".center(WIDTH))
        print("=" * WIDTH)
        print(f"✅ Sukses : {total_success}")
        print(f"❌ Gagal  : {total_fail}")
        print("=" * WIDTH)
        pause()