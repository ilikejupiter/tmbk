from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Callable

from app.client.engsel import get_transaction_history
from app.menus.util import clear_screen


WIB = timezone(timedelta(hours=7))


def _normalize_timestamp(ts: Any) -> Optional[int]:
    """Accept seconds or milliseconds epoch; return seconds as int."""
    try:
        ts_int = int(ts)
    except Exception:
        return None

    # Heuristic: milliseconds are usually 13 digits
    if ts_int > 10_000_000_000:  # ~year 2286 in seconds; anything bigger likely ms
        ts_int = ts_int // 1000
    if ts_int <= 0:
        return None
    return ts_int


def _format_wib(ts: Any) -> str:
    ts_norm = _normalize_timestamp(ts)
    if ts_norm is None:
        return "Unknown"
    dt = datetime.fromtimestamp(ts_norm, tz=timezone.utc).astimezone(WIB)
    # Avoid locale dependency (bulan Indonesia) => format numerik stabil
    return dt.strftime("%d-%m-%Y | %H:%M WIB")


def _safe_get(d: Dict[str, Any], key: str, default: str = "-") -> str:
    val = d.get(key, default)
    return default if val is None else str(val)


def _call_get_transaction_history(
    api_key: str,
    tokens: Dict[str, Any],
    limit: int,
    offset: int,
    cursor: Optional[str],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Optional[str]]:
    """
    Try multiple calling conventions to remain backward-compatible with
    unknown get_transaction_history signatures.
    Returns: (raw_data, list_items, next_cursor)
    """
    # Try common patterns:
    candidates: List[Callable[[], Dict[str, Any]]] = [
        lambda: get_transaction_history(api_key, tokens, limit=limit, offset=offset),
        lambda: get_transaction_history(api_key, tokens, limit=limit, page=(offset // limit) + 1),
        lambda: get_transaction_history(api_key, tokens, page=(offset // limit) + 1, page_size=limit),
        lambda: get_transaction_history(api_key, tokens, cursor=cursor, limit=limit) if cursor else (_raise_typeerror()),
        lambda: get_transaction_history(api_key, tokens),
    ]

    last_err: Optional[Exception] = None
    for fn in candidates:
        try:
            data = fn()
            items = data.get("list", []) or []
            next_cursor = data.get("next_cursor") or data.get("nextCursor") or data.get("cursor_next")
            # If the API doesn't support paging, emulate it locally
            if fn.__name__ == "<lambda>" and data is not None and "list" in data and len(items) > 0:
                pass
            return data, items, next_cursor
        except TypeError as e:
            last_err = e
            continue
        except Exception as e:
            # Non-signature error (network/auth/etc) -> raise
            raise e
    if last_err:
        raise last_err
    return {}, [], None


def _raise_typeerror() -> Dict[str, Any]:
    raise TypeError("cursor mode not available")


def show_transaction_history(api_key: str, tokens: Dict[str, Any]) -> None:
    """
    UI:
      n  : next page
      p  : prev page
      s  : set page size (misal 100)
      r  : refresh
      00 : kembali
    """
    page_size = 100  # >50 by default
    page = 1
    cursor: Optional[str] = None
    cursor_stack: List[Optional[str]] = [None]  # history of cursors per page (best effort)

    while True:
        clear_screen()
        print("-------------------------------------------------------")
        print("Riwayat Transaksi")
        print(f"Page: {page} | Page Size: {page_size}")
        print("-------------------------------------------------------")

        try:
            offset = (page - 1) * page_size
            data, history, next_cursor = _call_get_transaction_history(
                api_key=api_key,
                tokens=tokens,
                limit=page_size,
                offset=offset,
                cursor=cursor,
            )

            # If API returned full list only, slice locally
            total_list = data.get("list", history) or []
            if isinstance(total_list, list) and len(total_list) > page_size:
                history = total_list[offset : offset + page_size]
                next_cursor = None  # local-slice mode; cursor unknown

        except Exception as e:
            print(f"Gagal mengambil riwayat transaksi: {e}")
            history = []
            next_cursor = None

        if not history:
            print("Tidak ada riwayat transaksi (atau halaman kosong).")
        else:
            for idx, trx in enumerate(history, start=1 + (page - 1) * page_size):
                title = _safe_get(trx, "title")
                price = _safe_get(trx, "price")
                ts = trx.get("timestamp", 0)

                print(f"{idx}. {title} - {price}")
                print(f"   Tanggal: {_format_wib(ts)}")
                print(f"   Metode Pembayaran: {_safe_get(trx, 'payment_method_label')}")
                print(f"   Status Transaksi: {_safe_get(trx, 'status')}")
                print(f"   Status Pembayaran: {_safe_get(trx, 'payment_status')}")
                print("-------------------------------------------------------")

        print("n. Next page | p. Prev page | s. Set page size | r. Refresh")
        print("00. Kembali ke Menu Utama")
        choice = input("Pilih opsi: ").strip().lower()

        if choice == "00":
            return
        if choice == "r":
            continue
        if choice == "s":
            raw = input("Masukkan page size (mis. 100/200): ").strip()
            try:
                new_size = int(raw)
                if new_size < 1:
                    raise ValueError
                page_size = new_size
                page = 1
                cursor = None
                cursor_stack = [None]
            except Exception:
                print("Page size tidak valid.")
                input("Enter untuk lanjut...")
            continue
        if choice == "n":
            # best-effort cursor paging:
            if next_cursor:
                cursor = next_cursor
                cursor_stack.append(cursor)
                page += 1
            else:
                page += 1
            continue
        if choice == "p":
            if page > 1:
                page -= 1
                # best-effort cursor back:
                if len(cursor_stack) >= page:
                    cursor = cursor_stack[page - 1]
                    cursor_stack = cursor_stack[:page]
                else:
                    cursor = None
            continue

        print("Opsi tidak valid. Silakan coba lagi.")
        input("Enter untuk lanjut...")