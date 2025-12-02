from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, List

# Import Internal Modules
from app.client.store.segments import get_segments
from app.menus.util import clear_screen, pause
from app.service.auth import AuthInstance
from app.menus.package import show_package_details

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

WIDTH = 65

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


def _fmt_rp(value: Any) -> str:
    try:
        if value is None:
            return "N/A"
        if isinstance(value, str):
            v = value.strip()
            if not v:
                return "N/A"
            digits = "".join(ch for ch in v if ch.isdigit())
            if digits:
                value = int(digits)
            else:
                return v
        if isinstance(value, float):
            value = int(value)
        if isinstance(value, int):
            return f"Rp{value:,}"
        return _safe_str(value, "N/A")
    except Exception:
        return "N/A"


def _handle_action(api_key: str, tokens: Tokens, item: Json, is_enterprise: bool) -> None:
    """
    Menangani logika navigasi berdasarkan tipe aksi pada banner.
    """
    action_type = _safe_str(item.get("action_type", "UNKNOWN")).upper()
    action_param = _safe_str(item.get("action_param", "")).strip()
    title = _safe_str(item.get("title", "Unknown Item"))

    print(f"\n🔄 Memproses: {title}...")

    try:
        if action_type == "PDP":
            if action_param:
                show_package_details(api_key, dict(tokens), action_param, is_enterprise)
            else:
                print("❌ Parameter paket (Option Code) kosong.")
                pause()

        elif action_type == "WEBVIEW":
            print(f"ℹ️  Item ini membuka link web: {action_param or '-'}")
            print("   Fitur browser belum tersedia di CLI.")
            pause()

        else:
            print(f"⚠️  Tipe aksi tidak didukung: {action_type}")
            print(f"   Param: {action_param or '-'}")
            pause()

    except Exception as e:
        logger.exception("Error handling action %s", action_type)
        print(f"❌ Terjadi kesalahan: {e}")
        pause()


def _extract_store_segments(res: Optional[Json]) -> List[Json]:
    """
    Normalisasi struktur segments:
    - res["data"]["store_segments"]
    - res["data"]["segments"]
    - res["data"] list
    - res["store_segments"] list
    """
    if not isinstance(res, dict):
        return []

    data = res.get("data")
    if isinstance(data, dict):
        segs = data.get("store_segments") or data.get("segments") or data.get("results") or data.get("list")
        if isinstance(segs, list):
            return [x for x in segs if isinstance(x, dict)]
        return []

    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    segs2 = res.get("store_segments")
    if isinstance(segs2, list):
        return [x for x in segs2 if isinstance(x, dict)]

    return []


def _extract_banners(segment: Json) -> List[Json]:
    for k in ("banners", "items", "results", "list"):
        v = segment.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def show_store_segments_menu(is_enterprise: bool = False) -> None:
    """
    Menampilkan menu Store Segments (Banner Promo) dengan UI modern.
    """
    while True:
        api_key = getattr(AuthInstance, "api_key", "") or ""
        tokens = AuthInstance.get_active_tokens() or {}

        if not tokens:
            print("❌ Sesi berakhir. Silahkan login kembali.")
            pause()
            return

        clear_screen()
        print("=" * WIDTH)
        print("🛍️  XL STORE - SEGMENTS & PROMO".center(WIDTH))
        print("=" * WIDTH)
        print("⏳ Mengambil data promo...", end="\r")

        try:
            res = get_segments(api_key, tokens, is_enterprise)
        except Exception:
            logger.exception("Fetch segments failed")
            res = None

        segments = _extract_store_segments(res)

        print(" " * WIDTH, end="\r")
        if not segments:
            print("📭 Tidak ada promo tersedia saat ini / gagal mengambil data.")
            pause()
            return

        selection_map: Dict[str, Json] = {}

        for i, segment in enumerate(segments):
            seg_title = _safe_str(segment.get("title") or segment.get("name") or "Promo Lainnya")
            banners = _extract_banners(segment)
            if not banners:
                continue

            seg_letter = chr(65 + i)
            print(f"\n[{seg_letter}] {seg_title.upper()}")
            print("-" * WIDTH)

            for j, banner in enumerate(banners, start=1):
                title = _clip(banner.get("title") or banner.get("name") or "No Name", 30)
                fam_name = _clip(banner.get("family_name") or banner.get("familyName") or "", 15)

                price = banner.get("discounted_price", banner.get("price", banner.get("original_price", None)))
                validity = _safe_str(banner.get("validity") or banner.get("validity_text") or "-", "-")

                action_type = _safe_str(banner.get("action_type") or banner.get("actionType") or "PDP").upper()
                action_param = _safe_str(banner.get("action_param") or banner.get("actionParam") or "", "").strip()

                key = f"{seg_letter}{j}".lower()
                selection_map[key] = {
                    "title": f"{fam_name} - {title}".strip(" -"),
                    "action_type": action_type,
                    "action_param": action_param,
                }

                price_str = _fmt_rp(price)
                row_prefix = f" {seg_letter}{j}"
                print(f"{row_prefix:<4} {title:<32} | {price_str:<12} | {validity}")

        if not selection_map:
            print("\n📭 Tidak ada banner promo yang bisa ditampilkan.")
            pause()
            return

        print("\n" + "=" * WIDTH)
        print("[Kode]  Lihat Detail Promo (Contoh: A1)")
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