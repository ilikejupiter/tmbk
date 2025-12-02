import logging
import inspect
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.menus.util import clear_screen, pause
from app.client.engsel import get_notification_detail, dashboard_segments
from app.service.auth import AuthInstance

logger = logging.getLogger(__name__)

WIDTH = 60
DEFAULT_LIMIT = 50          # default seperti sebelumnya
MAX_LIMIT = 500             # supaya bisa lihat > 50 (kalau API mendukung)
DEFAULT_PAGE_SIZE = 20      # paging biar nyaman dibaca


@dataclass
class NotificationItem:
    notification_id: str
    is_read: bool
    brief_message: str
    full_message: str
    timestamp: Any
    image_url: str

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "NotificationItem":
        return cls(
            notification_id=str(d.get("notification_id") or ""),
            is_read=bool(d.get("is_read", False)),
            brief_message=str(d.get("brief_message") or "Pesan Sistem"),
            full_message=str(d.get("full_message") or ""),
            timestamp=d.get("timestamp"),
            image_url=str(d.get("image_url") or ""),
        )


def _format_timestamp(ts: Any) -> str:
    """Ubah timestamp (int/float/str) jadi tanggal mudah dibaca."""
    try:
        if ts is None or ts == "":
            return "-"
        timestamp = float(ts)
        # deteksi ms
        if timestamp > 1_000_000_000_000:
            timestamp /= 1000.0
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime("%d %b %H:%M")
    except Exception:
        return str(ts)


def _safe_input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return "00"


def _safe_get_tokens() -> Optional[dict]:
    try:
        return AuthInstance.get_active_tokens()
    except Exception as e:
        logger.error("get_active_tokens error: %s", e)
        return None


def _call_dashboard_segments(api_key: str, tokens: dict, limit: int) -> Optional[Dict[str, Any]]:
    """
    Panggil dashboard_segments dengan cara yang kompatibel lintas versi:
    - Jika fungsi mendukung parameter limit (atau **kwargs), kita kirim limit.
    - Jika tidak, fallback pemanggilan tanpa limit.
    """
    try:
        sig = inspect.signature(dashboard_segments)
        params = sig.parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

        kwargs: Dict[str, Any] = {}
        # beberapa kemungkinan nama parameter limit yang "umum"
        if "limit" in params or has_var_kw:
            kwargs["limit"] = limit
        elif "max_results" in params:
            kwargs["max_results"] = limit
        elif "page_size" in params or has_var_kw:
            kwargs["page_size"] = limit

        # hanya kirim kwargs kalau benar-benar ada
        if kwargs:
            return dashboard_segments(api_key, tokens, **kwargs)
        return dashboard_segments(api_key, tokens)

    except TypeError:
        # fallback paling aman
        try:
            return dashboard_segments(api_key, tokens)
        except Exception as e:
            logger.error("dashboard_segments failed after fallback: %s", e)
            return None
    except Exception as e:
        logger.error("dashboard_segments failed: %s", e)
        return None


def _extract_notifications(res: Dict[str, Any]) -> List[NotificationItem]:
    """Ambil list notifikasi dengan safe-navigation."""
    data = (res or {}).get("data", {})
    notif = (data or {}).get("notification", {})
    raw_list = (notif or {}).get("data", []) or []
    out: List[NotificationItem] = []
    for item in raw_list:
        if isinstance(item, dict):
            ni = NotificationItem.from_dict(item)
            if ni.notification_id:  # drop item rusak tanpa id
                out.append(ni)
    return out


def _fetch_notification_detail(api_key: str, tokens: dict, notif_id: str) -> Dict[str, Any]:
    """Fetch detail notifikasi. Dipakai juga sebagai 'mark as read' (kalau API side-effect)."""
    try:
        res = get_notification_detail(api_key, tokens, notif_id)
        return res if isinstance(res, dict) else {}
    except Exception as e:
        logger.error("get_notification_detail error (%s): %s", notif_id, e)
        return {}


def _mark_as_read(api_key: str, tokens: dict, notif_id: str) -> bool:
    """
    Helper menandai notifikasi terbaca.
    Catatan: pada skrip lama, get_notification_detail diasumsikan sekaligus mark-read.
    """
    res = _fetch_notification_detail(api_key, tokens, notif_id)
    # super defensif: beberapa API pakai status, sebagian tidak.
    status = str(res.get("status") or "").upper()
    return status == "SUCCESS" or status == "OK" or bool(res)


def _render_page(items: List[NotificationItem], page: int, page_size: int) -> Tuple[Dict[int, NotificationItem], int, int]:
    total = len(items)
    if total == 0:
        return {}, 0, 0

    max_page = (total - 1) // page_size
    page = max(0, min(page, max_page))
    start = page * page_size
    end = min(start + page_size, total)

    selection_map: Dict[int, NotificationItem] = {}
    for idx, notif in enumerate(items[start:end], start=1):
        icon = "✅" if notif.is_read else "📩"
        ts_str = _format_timestamp(notif.timestamp)
        brief = (notif.brief_message or "Pesan Sistem").strip()
        if len(brief) > 52:
            brief = brief[:52] + "…"
        selection_map[idx] = notif
        print(f"{idx:<2} {icon} [{ts_str}] {brief}")

    return selection_map, page, max_page


def show_notification_menu() -> None:
    """
    Pusat Notifikasi:
    - Limit bisa > 50 (kalau API mendukung)
    - Paging di sisi UI supaya nyaman dibaca
    - Saat buka detail, fetch detail via API (lebih akurat daripada dari list)
    """
    limit = DEFAULT_LIMIT
    page_size = DEFAULT_PAGE_SIZE
    page = 0

    while True:
        api_key = getattr(AuthInstance, "api_key", None)
        tokens = _safe_get_tokens()

        if not api_key or not tokens:
            print("❌ Sesi berakhir. Silahkan login kembali.")
            pause()
            return

        clear_screen()
        print("=" * WIDTH)
        print("📩  PUSAT NOTIFIKASI".center(WIDTH))
        print("=" * WIDTH)
        print(f"⏳ Mengambil pesan... (limit={limit})".ljust(WIDTH), end="\r")

        res = _call_dashboard_segments(api_key, tokens, limit=limit)
        if not res or not isinstance(res, dict):
            print(" " * WIDTH, end="\r")
            print("\n❌ Gagal mengambil data notifikasi.")
            pause()
            return

        items = _extract_notifications(res)
        if not items:
            print(" " * WIDTH, end="\r")
            print("\n📭 Tidak ada notifikasi.")
            pause()
            return

        # stats
        unread = [x for x in items if not x.is_read]
        total = len(items)

        # render
        clear_screen()
        print("=" * WIDTH)
        print("📩  PUSAT NOTIFIKASI".center(WIDTH))
        print("=" * WIDTH)

        selection_map, page, max_page = _render_page(items, page=page, page_size=page_size)

        print("-" * WIDTH)
        print(f"Total dimuat: {total} | Belum dibaca: {len(unread)} | Page: {page+1}/{max_page+1}")
        print("=" * WIDTH)

        print("COMMANDS:")
        print(" [No]     Baca detail (contoh: 1)")
        print(" [N]      Next page")
        print(" [P]      Prev page")
        print(" [L]      Ubah limit (mis: 100/200)  ✅ bisa > 50")
        print(" [S]      Ubah page size (mis: 20/30)")
        print(" [R]      Tandai semua yang dimuat sudah dibaca (Mark All Read)")
        print(" [00]     Kembali")
        print("-" * WIDTH)

        choice = _safe_input("Pilihan >> ").strip().upper()

        if choice == "00":
            return

        if choice == "N":
            if page < max_page:
                page += 1
            continue

        if choice == "P":
            if page > 0:
                page -= 1
            continue

        if choice == "L":
            raw = _safe_input("Masukkan limit (contoh 100) >> ")
            try:
                new_limit = int(raw)
                if new_limit < 1:
                    raise ValueError
                limit = min(new_limit, MAX_LIMIT)
                page = 0
                if new_limit > MAX_LIMIT:
                    print(f"⚠️ Limit dibatasi sampai {MAX_LIMIT}.")
                    pause()
            except Exception:
                print("⚠️ Limit harus angka positif.")
                pause()
            continue

        if choice == "S":
            raw = _safe_input("Masukkan page size (contoh 20) >> ")
            try:
                new_size = int(raw)
                if new_size < 5 or new_size > 100:
                    print("⚠️ Saran page size 5..100.")
                    pause()
                else:
                    page_size = new_size
                    page = 0
            except Exception:
                print("⚠️ Page size harus angka.")
                pause()
            continue

        if choice == "R":
            if not unread:
                print("\n✅ Semua pesan sudah terbaca.")
                pause()
                continue

            print(f"\n🔄 Memproses {len(unread)} pesan...")
            ok = 0
            for n in unread:
                if n.notification_id and _mark_as_read(api_key, tokens, n.notification_id):
                    ok += 1
                    print(f"   ✓ {n.notification_id[:8]}... ok")
                else:
                    print(f"   ✗ {n.notification_id[:8]}... gagal")
            print(f"\nSelesai! {ok}/{len(unread)} pesan ditandai.")
            pause()
            continue

        if choice.isdigit():
            idx = int(choice)
            notif = selection_map.get(idx)
            if not notif:
                print("⚠️ Nomor tidak valid.")
                pause()
                continue

            nid = notif.notification_id
            # fetch detail agar full_message lebih akurat + sekaligus mark as read jika API begitu
            detail = _fetch_notification_detail(api_key, tokens, nid)

            # fallback ke data list kalau detail minim
            brief = str(detail.get("brief_message") or notif.brief_message or "Pesan Sistem")
            full_msg = str(detail.get("full_message") or notif.full_message or "")
            ts = detail.get("timestamp", notif.timestamp)
            img_url = str(detail.get("image_url") or notif.image_url or "")

            clear_screen()
            print("=" * WIDTH)
            print(f"DETAIL PESAN #{idx}".center(WIDTH))
            print("=" * WIDTH)
            print(f"📅 Waktu : {_format_timestamp(ts)}")
            print(f"📌 Judul : {brief}")
            print("-" * WIDTH)
            print(full_msg.strip() or "(Tidak ada isi detail)")
            print()

            if img_url:
                print(f"[Gambar]: {img_url}")

            print("=" * WIDTH)
            pause("Tekan Enter untuk kembali...")
            continue

        print("⚠️ Perintah tidak dikenal.")
        pause()