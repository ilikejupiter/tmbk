from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

# Import Internal Modules
from app.client.store.search import get_family_list, get_store_packages
from app.menus.package import get_packages_by_family, show_package_details
from app.menus.util import clear_screen, pause
from app.service.auth import AuthInstance

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

WIDTH = 65

Json = Dict[str, Any]
Tokens = Mapping[str, Any]


def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    try:
        s = str(val)
        return s
    except Exception:
        return default


def _clip(text: Any, n: int) -> str:
    s = _safe_str(text, "Unknown")
    s = s.replace("\n", " ").strip()
    return s[:n]


def _fmt_price(value: Any) -> str:
    """
    Format harga seaman mungkin.
    Input bisa int/float/str/dict.
    """
    try:
        if isinstance(value, dict):
            value = value.get("amount", value.get("value", value.get("price", 0)))

        if value is None:
            return "N/A"

        if isinstance(value, str):
            # coba ambil angka dari string
            cleaned = "".join(ch for ch in value if ch.isdigit())
            if cleaned:
                value = int(cleaned)
            else:
                return value.strip() or "N/A"

        if isinstance(value, float):
            value = int(value)

        if isinstance(value, int):
            return f"{value:,}"

        return _safe_str(value, "N/A")
    except Exception:
        return "N/A"


def _call_get_packages_by_family(family_code: str, is_enterprise: bool, text_search: str = "") -> None:
    """
    Wrapper agar tetap aman walau signature get_packages_by_family beda (2 arg vs 3 arg).
    """
    try:
        # Banyak project pakai (family_code, is_enterprise, text_search)
        get_packages_by_family(family_code, is_enterprise, text_search)  # type: ignore[arg-type]
    except TypeError:
        # Fallback signature lama (family_code, is_enterprise)
        get_packages_by_family(family_code, is_enterprise)  # type: ignore[arg-type]


def _extract_family_list(res: Optional[Json]) -> List[Json]:
    """
    Normalisasi response family list:
    - res["data"]["results"] (umum)
    - res["data"] list
    - res["results"] list
    """
    if not isinstance(res, dict):
        return []

    data = res.get("data")
    if isinstance(data, dict):
        results = data.get("results") or data.get("items") or data.get("list")
        if isinstance(results, list):
            return [x for x in results if isinstance(x, dict)]
        # kadang data langsung bentuk obj tunggal
        return []

    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    results2 = res.get("results")
    if isinstance(results2, list):
        return [x for x in results2 if isinstance(x, dict)]

    return []


def _extract_package_list(res: Optional[Json]) -> List[Json]:
    """
    Normalisasi response paket store:
    - res["data"]["results_price_only"]
    - res["data"]["packages"]
    - res["data"]["results"]
    - res["data"] list
    - res["data"] dict -> cari list di key umum
    """
    if not isinstance(res, dict):
        return []

    raw = res.get("data")
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]

    if isinstance(raw, dict):
        for k in ("results_price_only", "packages", "results", "items", "list", "data"):
            v = raw.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]

        # fallback: BFS cari list dict pertama
        queue: List[Any] = list(raw.values())
        visited = 0
        while queue and visited < 5000:
            visited += 1
            cur = queue.pop(0)
            if isinstance(cur, list):
                dicts = [x for x in cur if isinstance(x, dict)]
                if dicts:
                    return dicts
            elif isinstance(cur, dict):
                queue.extend(cur.values())

    return []


def _handle_action(api_key: str, tokens: Tokens, item: Json, is_enterprise: bool) -> None:
    """Menangani logika navigasi berdasarkan tipe aksi paket."""
    action_type = _safe_str(item.get("action_type", "UNKNOWN")).upper()
    action_param = _safe_str(item.get("action_param", "")).strip()
    title = _safe_str(item.get("title", "Unknown Item"))

    print(f"\n🔄 Memproses: {title}...")

    try:
        if action_type == "PDP":
            if action_param:
                show_package_details(api_key, dict(tokens), action_param, is_enterprise)
            else:
                print("❌ Parameter paket kosong.")
                pause()

        elif action_type == "PLP":
            if action_param:
                _call_get_packages_by_family(action_param, is_enterprise, "")
            else:
                print("❌ Parameter family kosong.")
                pause()

        else:
            print(f"⚠️  Tipe aksi tidak didukung: {action_type}")
            print(f"   Param: {action_param or '-'}")
            pause()

    except Exception as e:
        logger.exception("Error handling action %s", action_type)
        print(f"❌ Terjadi kesalahan: {e}")
        pause()


def show_family_list_menu(subs_type: str = "PREPAID", is_enterprise: bool = False) -> None:
    """
    Menampilkan daftar Kategori Family (Group Paket).
    """
    while True:
        api_key = getattr(AuthInstance, "api_key", "") or ""
        tokens = AuthInstance.get_active_tokens() or {}

        if not tokens:
            print("❌ Sesi berakhir.")
            pause()
            return

        clear_screen()
        print("=" * WIDTH)
        print("📂  XL STORE - FAMILY LIST".center(WIDTH))
        print("=" * WIDTH)
        print("⏳ Mengambil daftar kategori...", end="\r")

        try:
            res = get_family_list(api_key, tokens, subs_type, is_enterprise)
        except Exception as e:
            logger.exception("Fetch family failed")
            res = None

        family_list = _extract_family_list(res)

        print(" " * WIDTH, end="\r")
        if not family_list:
            print("📭 Tidak ada kategori ditemukan / gagal mengambil data.")
            pause()
            return

        print(f"{'NO':<4} | {'NAMA KATEGORI':<35} | {'KODE FAMILY'}")
        print("-" * WIDTH)

        for i, fam in enumerate(family_list, start=1):
            name = _clip(fam.get("label") or fam.get("name") or fam.get("title"), 35)
            code = _safe_str(fam.get("id") or fam.get("code") or fam.get("family_code"), "-")
            print(f" {i:<3} | {name:<35} | {code}")

        print("-" * WIDTH)
        print("[No]  Pilih Kategori")
        print("[00]  Kembali")
        print("=" * WIDTH)

        choice = input("Pilihan >> ").strip()

        if choice == "00":
            return

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(family_list):
                selected = family_list[idx]
                fam_id = _safe_str(selected.get("id") or selected.get("code") or selected.get("family_code"), "").strip()
                if fam_id:
                    _call_get_packages_by_family(fam_id, is_enterprise, "")
                else:
                    print("⚠️ ID Family tidak valid.")
                    pause()
            else:
                print("⚠️ Nomor tidak ada.")
                pause()
        else:
            print("⚠️ Input tidak valid.")
            pause()


def show_store_packages_menu(subs_type: str = "PREPAID", is_enterprise: bool = False) -> None:
    """
    Menampilkan daftar Paket Rekomendasi Store.
    """
    while True:
        api_key = getattr(AuthInstance, "api_key", "") or ""
        tokens = AuthInstance.get_active_tokens() or {}

        if not tokens:
            print("❌ Sesi berakhir.")
            pause()
            return

        clear_screen()
        print("=" * WIDTH)
        print("📦  XL STORE - RECOMMENDED PACKAGES".center(WIDTH))
        print("=" * WIDTH)
        print("⏳ Mengambil paket rekomendasi...", end="\r")

        try:
            res = get_store_packages(api_key, tokens, subs_type, is_enterprise)
        except Exception as e:
            logger.exception("Fetch store packages failed")
            res = None

        pkg_list = _extract_package_list(res)

        print(" " * WIDTH, end="\r")
        if not pkg_list:
            print("📭 Tidak ada paket ditemukan / gagal mengambil data.")
            pause()
            return

        print(f"{'NO':<4} | {'NAMA PAKET':<30} | {'HARGA':<12} | {'MASA AKTIF'}")
        print("-" * WIDTH)

        selection_map: Dict[int, Json] = {}

        for i, pkg in enumerate(pkg_list, start=1):
            title = pkg.get("title") or pkg.get("name") or pkg.get("package_name") or "Unknown"
            title = _clip(title, 30)

            orig_price = pkg.get("original_price", pkg.get("price", 0))
            disc_price = pkg.get("discounted_price", pkg.get("discount_price", 0))

            final_price = disc_price if isinstance(disc_price, (int, float)) and disc_price > 0 else orig_price
            price_str = _fmt_price(final_price)

            validity = _safe_str(pkg.get("validity") or pkg.get("validity_text") or pkg.get("validity_days") or "-", "-")

            action_type = _safe_str(pkg.get("action_type") or "PDP").upper()
            action_param = _safe_str(
                pkg.get("action_param")
                or pkg.get("package_code")
                or pkg.get("package_variant_code")
                or pkg.get("variant_code")
                or pkg.get("code")
                or pkg.get("id"),
                "",
            ).strip()

            selection_map[i] = {
                "title": title,
                "action_type": action_type,
                "action_param": action_param,
            }

            print(f" {i:<3} | {title:<30} | {price_str:<12} | {validity}")

        print("-" * WIDTH)
        print("[No]  Lihat Detail Paket")
        print("[00]  Kembali")
        print("=" * WIDTH)

        choice = input("Pilihan >> ").strip()

        if choice == "00":
            return

        if choice.isdigit():
            idx = int(choice)
            if idx in selection_map:
                _handle_action(api_key, tokens, selection_map[idx], is_enterprise)
            else:
                print("⚠️ Nomor tidak ada.")
                pause()
        else:
            print("⚠️ Input tidak valid.")
            pause()