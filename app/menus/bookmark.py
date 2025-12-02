from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

# Import Dependencies
from app.client.engsel import get_family
from app.menus.package import show_package_details
from app.menus.util import clear_screen, pause
from app.service.auth import AuthInstance
from app.service.bookmark import BookmarkInstance

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

WIDTH = 60
Json = Dict[str, Any]


def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    try:
        return str(val)
    except Exception:
        return default


def _clip(text: Any, n: int) -> str:
    s = _safe_str(text, "Unknown").replace("\n", " ").strip()
    return s[:n]


def _normalize_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(val, (int, float)):
        return bool(val)
    return default


def _normalize_int(val: Any, default: int = 0) -> int:
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


def _safe_bookmarks(raw: Any) -> List[Json]:
    """
    Pastikan list bookmark selalu berupa list[dict], tahan jika storage korup.
    """
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def _find_option_code_in_family(family_data: Any, variant_name: str, order: int) -> Optional[str]:
    """
    Cari package_option_code dalam family_data berdasarkan:
    - variant_name (case-insensitive, trimmed)
    - order pada package_options

    Fallback:
    - kalau order tidak ketemu, coba ambil option pertama yang punya code.
    """
    try:
        if not isinstance(family_data, dict):
            return None

        variants = family_data.get("package_variants")
        if not isinstance(variants, list):
            return None

        target_name = (variant_name or "").strip().lower()
        if not target_name:
            return None

        for variant in variants:
            if not isinstance(variant, dict):
                continue

            vname = _safe_str(variant.get("name", "")).strip().lower()
            if vname != target_name:
                continue

            options = variant.get("package_options", [])
            if not isinstance(options, list):
                return None

            # 1) match by order
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                if opt.get("order") == order:
                    code = _safe_str(opt.get("package_option_code"), "").strip()
                    return code or None

            # 2) fallback: ambil code pertama yang ada (biar bookmark tidak langsung useless)
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                code = _safe_str(opt.get("package_option_code"), "").strip()
                if code:
                    return code

    except Exception:
        logger.debug("Error parsing family data for option code.", exc_info=True)

    return None


def _confirm(prompt: str) -> bool:
    ans = input(prompt).strip().lower()
    return ans in {"y", "yes"}


def show_bookmark_menu() -> None:
    """
    Menampilkan menu bookmark dengan UI modern dan command-line style inputs.
    """
    api_key = _safe_str(getattr(AuthInstance, "api_key", ""), "")
    tokens = AuthInstance.get_active_tokens() or {}

    if not tokens:
        print("⚠️  Sesi tidak valid. Silahkan login kembali.")
        pause()
        return

    while True:
        clear_screen()
        bookmarks = _safe_bookmarks(BookmarkInstance.get_bookmarks())

        print("=" * WIDTH)
        print("🔖  BOOKMARK / PAKET TERSIMPAN".center(WIDTH))
        print("=" * WIDTH)

        if not bookmarks:
            print("\n   [ 📭 Tidak ada bookmark tersimpan ]\n")
            print("   Tips: Anda bisa menyimpan paket favorit saat")
            print("   menjelajahi menu paket beli.")
            print("-" * WIDTH)
            print("[00] Kembali")
            choice = input("\nPilihan >> ").strip()
            if choice == "00":
                return
            continue

        print(f"{'NO':<4} | {'FAMILY / KATEGORI':<20} | {'NAMA PAKET'}")
        print("-" * WIDTH)

        for idx, bm in enumerate(bookmarks):
            fam = _clip(bm.get("family_name", "Unknown"), 18)
            var = _clip(bm.get("variant_name", "-"), 18)
            opt = _clip(bm.get("option_name", "-"), 18)
            pkg_name = _clip(f"{var} {opt}".strip(), 30)
            print(f"{idx + 1:<4} | {fam:<20} | {pkg_name}")

        print("-" * WIDTH)
        print("COMMANDS:")
        print(" [No]     Pilih nomor untuk beli/lihat detail")
        print(" [del No] Hapus bookmark (contoh: del 1)")
        print(" [00]     Kembali ke Menu Utama")
        print("=" * WIDTH)

        choice = input("Pilihan >> ").strip().lower()

        # Back
        if choice in {"00", "back", "exit", "q"}:
            return

        # Delete
        if choice.startswith(("del ", "rm ")):
            parts = choice.split()
            if len(parts) != 2 or not parts[1].isdigit():
                print("❌ Format salah. Gunakan: del <nomor>")
                pause()
                continue

            idx = int(parts[1]) - 1
            if not (0 <= idx < len(bookmarks)):
                print("❌ Nomor tidak valid.")
                pause()
                continue

            target = bookmarks[idx]
            name = _safe_str(target.get("variant_name") or target.get("option_name") or "bookmark").strip()
            if _confirm(f"❓ Hapus bookmark '{name}'? (y/n): "):
                try:
                    BookmarkInstance.remove_bookmark(
                        target["family_code"],
                        _normalize_bool(target.get("is_enterprise"), False),
                        target["variant_name"],
                        _normalize_int(target.get("order"), 0),
                    )
                    print("🗑️  Bookmark berhasil dihapus.")
                except Exception as e:
                    logger.exception("Failed removing bookmark")
                    print(f"❌ Gagal menghapus bookmark: {_safe_str(e)}")
            else:
                print("Batal.")
            pause()
            continue

        # Select bookmark number
        if choice.isdigit():
            idx = int(choice) - 1
            if not (0 <= idx < len(bookmarks)):
                print("❌ Nomor tidak ada dalam daftar.")
                pause()
                continue

            selected = bookmarks[idx]

            family_code = _safe_str(selected.get("family_code"), "").strip()
            variant_name = _safe_str(selected.get("variant_name"), "").strip()
            is_enterprise = _normalize_bool(selected.get("is_enterprise"), False)
            order = _normalize_int(selected.get("order"), 0)

            if not family_code or not variant_name:
                print("❌ Bookmark rusak (data tidak lengkap). Disarankan hapus bookmark ini.")
                pause()
                continue

            print(f"\n🔄 Mengambil detail paket terbaru untuk '{variant_name}'...")

            try:
                family_data = get_family(api_key, tokens, family_code, is_enterprise)
            except Exception as e:
                logger.exception("Failed fetching family data")
                family_data = None
                print(f"❌ Gagal mengambil data paket dari server: {_safe_str(e)}")
                pause()
                continue

            if not family_data:
                print("❌ Gagal mengambil data paket dari server.")
                print("   Paket mungkin sudah tidak tersedia atau koneksi bermasalah.")
                pause()
                continue

            option_code = _find_option_code_in_family(family_data, variant_name, order)

            if option_code:
                try:
                    show_package_details(api_key, tokens, option_code, is_enterprise)
                except Exception as e:
                    logger.exception("Failed opening package details")
                    print(f"❌ Gagal membuka detail paket: {_safe_str(e)}")
                    pause()
            else:
                print("\n⚠️  PAKET TIDAK DITEMUKAN / KADALUARSA")
                print("   Paket ini tampaknya sudah dihapus atau strukturnya berubah.")
                if _confirm("   Hapus bookmark ini sekarang? (y/n): "):
                    try:
                        BookmarkInstance.remove_bookmark(family_code, is_enterprise, variant_name, order)
                        print("🗑️  Bookmark dihapus.")
                    except Exception as e:
                        logger.exception("Failed removing expired bookmark")
                        print(f"❌ Gagal menghapus bookmark: {_safe_str(e)}")
                pause()

            continue

        print("❌ Perintah tidak dikenali.")
        pause()