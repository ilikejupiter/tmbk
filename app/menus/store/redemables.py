from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, List

# Import Internal Modules
from app.client.store.redeemables import get_redeemables
from app.service.auth import AuthInstance
from app.menus.util import clear_screen, pause
from app.menus.package import show_package_details, get_packages_by_family

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

WIDTH = 60

Json = Dict[str, Any]
Tokens = Mapping[str, Any]


def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    try:
        return str(val)
    except Exception:
        return default


def _clip(text: Any, n: int) -> str:
    s = _safe_str(text, "No Name").replace("\n", " ").strip()
    return s[:n]


def _format_expiry(timestamp: Any) -> str:
    """Helper untuk memformat tanggal kadaluarsa dengan aman."""
    try:
        if not timestamp:
            return "Selamanya"

        ts = timestamp
        if isinstance(ts, str):
            ts = ts.strip()
            if not ts:
                return "Selamanya"
            # coba parse angka
            digits = "".join(ch for ch in ts if ch.isdigit())
            ts = int(digits) if digits else 0

        if isinstance(ts, float):
            ts = int(ts)

        if not isinstance(ts, int) or ts <= 0:
            return "Unknown Date"

        # Handle millis vs seconds
        if ts > 1_000_000_000_000:
            ts = int(ts / 1000)

        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%d %b %Y")
    except Exception:
        return "Unknown Date"


def _call_get_packages_by_family(family_code: str, is_enterprise: bool, text_search: str = "") -> None:
    try:
        get_packages_by_family(family_code, is_enterprise, text_search)  # type: ignore[arg-type]
    except TypeError:
        get_packages_by_family(family_code, is_enterprise)  # type: ignore[arg-type]


def _handle_action(api_key: str, tokens: Tokens, pkg: Json, is_enterprise: bool) -> None:
    """Menangani logika navigasi berdasarkan action_type."""
    action_type = _safe_str(pkg.get("action_type") or pkg.get("actionType") or "").upper()
    action_param = _safe_str(pkg.get("action_param") or pkg.get("actionParam") or "").strip()
    name = _safe_str(pkg.get("name") or pkg.get("title") or "Unknown")

    print(f"\n🔄 Memproses: {name}...")

    try:
        if action_type == "PLP":
            if action_param:
                _call_get_packages_by_family(action_param, is_enterprise, "")
            else:
                print("❌ Parameter family kosong.")
                pause()

        elif action_type == "PDP":
            if action_param:
                show_package_details(api_key, dict(tokens), action_param, is_enterprise)
            else:
                print("❌ Parameter paket kosong.")
                pause()

        elif action_type == "WEBVIEW":
            print(f"ℹ️  Item ini adalah link web: {action_param or '-'}")
            print("   Fitur browser belum tersedia di CLI.")
            pause()

        else:
            print(f"⚠️  Tipe aksi tidak dikenal: {action_type or 'UNKNOWN'}")
            print(f"   Param: {action_param or '-'}")
            pause()

    except Exception as e:
        logger.exception("Error handling action %s", action_type)
        print(f"❌ Terjadi kesalahan saat membuka item: {e}")
        pause()


def _extract_categories(res: Optional[Json]) -> List[Json]:
    """
    Tahan perubahan struktur:
    - res["data"]["categories"] (umum)
    - res["data"] list
    - res["categories"] list
    """
    if not isinstance(res, dict):
        return []

    data = res.get("data")
    if isinstance(data, dict):
        cats = data.get("categories") or data.get("category") or data.get("results")
        if isinstance(cats, list):
            return [x for x in cats if isinstance(x, dict)]
        return []

    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    cats2 = res.get("categories")
    if isinstance(cats2, list):
        return [x for x in cats2 if isinstance(x, dict)]

    return []


def _extract_items(category: Json) -> List[Json]:
    """
    Item bisa ada di key berbeda:
    - redeemables
    - packages
    - items
    """
    for k in ("redeemables", "packages", "items", "results", "list"):
        v = category.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def show_redeemables_menu(is_enterprise: bool = False) -> None:
    """
    Menampilkan menu Redeemables (Voucher/Bonus) dengan UI modern.
    """
    while True:
        api_key = getattr(AuthInstance, "api_key", "") or ""
        tokens = AuthInstance.get_active_tokens() or {}

        if not tokens:
            print("❌ Sesi kadaluarsa. Silahkan login kembali.")
            pause()
            return

        clear_screen()
        print("=" * WIDTH)
        print("🎁  XL STORE - REDEEMABLES & PROMO".center(WIDTH))
        print("=" * WIDTH)
        print("⏳ Sedang mengambil data promo terbaru...", end="\r")

        try:
            res = get_redeemables(api_key, tokens, is_enterprise)
        except Exception:
            logger.exception("Fetch redeemables failed")
            res = None

        categories = _extract_categories(res)

        print(" " * WIDTH, end="\r")
        if not categories:
            print("\n📭 Tidak ada kategori promo ditemukan / gagal mengambil data.")
            pause()
            return

        selection_map: Dict[str, Json] = {}

        for i, category in enumerate(categories):
            cat_name = _safe_str(
                category.get("category_name") or category.get("name") or category.get("label") or "Unknown Category"
            )
            items = _extract_items(category)
            if not items:
                continue

            cat_letter = chr(65 + i)  # A, B, C...
            print(f"\n[{cat_letter}] {cat_name.upper()}")
            print("-" * WIDTH)

            for j, item in enumerate(items, start=1):
                key = f"{cat_letter}{j}".lower()
                selection_map[key] = item

                name = _clip(item.get("name") or item.get("title") or "No Name", 40)
                valid_until = item.get("valid_until", item.get("validUntil", item.get("expired_at", 0)))
                valid_date = _format_expiry(valid_until)
                act_type = _safe_str(item.get("action_type") or item.get("actionType") or "UNK").upper()

                icon = "📦" if act_type == "PDP" else "📂" if act_type == "PLP" else "🔗" if act_type == "WEBVIEW" else "❔"

                print(f" {cat_letter}{j:<2} {icon} {name:<35}")
                print(f"        Exp: {valid_date} | Tipe: {act_type}")

        if not selection_map:
            print("\n📭 Tidak ada promo yang bisa ditampilkan.")
            pause()
            return

        print("\n" + "=" * WIDTH)
        print("[Kode]  Pilih Promo (Contoh: A1)")
        print("[00]    Kembali ke Menu Utama")
        print("-" * WIDTH)

        choice = input("Pilihan >> ").strip().lower()

        if choice == "00":
            return

        if choice in selection_map:
            _handle_action(api_key, tokens, selection_map[choice], is_enterprise)
        else:
            print("⚠️  Kode pilihan tidak valid.")
            pause()