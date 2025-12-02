# -*- coding: utf-8 -*-
"""
Sentry Mode - real-time quota monitor (Python 3.9.18)

Improvements:
- Stable scheduling using time.monotonic() (avoids drift)
- Graceful stop: ENTER (TTY), Ctrl+C, SIGTERM/SIGINT
- JSONL logs include both success and error records, flushed every write
- Non-TTY friendly output (no hanging input, minimal noisy prints)
- Env-configurable interval/timeout/output:
    MYXL_SENTRY_DIR        default: sentry_logs
    MYXL_SENTRY_INTERVAL   default: 1.0 (seconds)
    MYXL_SENTRY_TIMEOUT    default: 15  (seconds)
    MYXL_SENTRY_PRINT_EVERY default: 10 (only for non-tty)
- Public API preserved: enter_sentry_mode()
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.client.engsel import send_api_request
from app.service.auth import AuthInstance

# ---------------------------------------------------------------------------
# Optional util imports (fallback to keep module resilient)
# ---------------------------------------------------------------------------

def _fallback_clear_screen() -> None:
    try:
        if sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
    except Exception:
        pass

def _fallback_pause(msg: str = "Tekan [Enter] untuk melanjutkan...") -> None:
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            print("")
            input(msg)
    except Exception:
        pass

try:
    # original import path (as in your file)
    from app.menus.util import clear_screen, pause  # type: ignore
except Exception:
    try:
        # alternate path (common layout)
        from app.util import clear_screen, pause  # type: ignore
    except Exception:
        clear_screen = _fallback_clear_screen
        pause = _fallback_pause


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return float(v.strip())
    except Exception:
        return default

def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return int(v.strip())
    except Exception:
        return default

def _isatty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False

def _now_iso() -> str:
    return datetime.now().isoformat()

def _safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Sentry Monitor
# ---------------------------------------------------------------------------

class SentryMonitor:
    def __init__(self):
        self.stop_event = threading.Event()

        self.interval_s = max(0.2, _env_float("MYXL_SENTRY_INTERVAL", 1.0))
        self.timeout_s = max(3, _env_int("MYXL_SENTRY_TIMEOUT", 15))
        self.print_every = max(1, _env_int("MYXL_SENTRY_PRINT_EVERY", 10))

        self.log_dir = Path(os.environ.get("MYXL_SENTRY_DIR", "sentry_logs"))
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # filesystem might be read-only; we'll error later when opening file
            pass

        self._tty = _isatty()
        self._progress_lock = threading.Lock()

        self._install_signal_handlers()

    # ------------------------------- stop controls --------------------------

    def _install_signal_handlers(self) -> None:
        # Make SIGTERM/SIGINT stop the loop cleanly.
        def _handler(signum, frame):  # noqa: ARG001
            self.stop_event.set()

        try:
            signal.signal(signal.SIGINT, _handler)
        except Exception:
            pass
        try:
            signal.signal(signal.SIGTERM, _handler)
        except Exception:
            pass

    def _input_listener(self) -> None:
        # Only meaningful in TTY; in non-TTY we do nothing.
        if not self._tty:
            return
        try:
            input()
        except Exception:
            # EOF / weird streams -> just stop
            pass
        finally:
            self.stop_event.set()

    # ------------------------------- display --------------------------------

    def _print_header(self, number: Any, log_path: Path) -> None:
        clear_screen()
        print("=" * 60)
        print("👁️  SENTRY MODE - REALTIME MONITORING".center(60))
        print("=" * 60)
        print(f" Target   : {number}")
        print(f" Log File : {log_path}")
        print(f" Interval : {self.interval_s:g} detik | Timeout: {self.timeout_s}s")
        print("-" * 60)
        if self._tty:
            print(" [ INFO ] Tekan [ENTER] untuk berhenti, atau Ctrl+C.")
        else:
            print(" [ INFO ] Non-TTY mode: hentikan dengan Ctrl+C / SIGTERM.")
        print("=" * 60)

    def _progress(self, line: str, *, force_newline: bool = False) -> None:
        with self._progress_lock:
            try:
                if self._tty and not force_newline:
                    sys.stdout.write("\r" + line[:120].ljust(120))
                    sys.stdout.flush()
                else:
                    print(line)
            except Exception:
                # never crash due to output
                pass

    # ------------------------------- core -----------------------------------

    def _fetch_quota(self, api_key: str, tokens: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        Returns:
          (ok, payload)
        payload: on success -> {"quotas": [...], "raw_status": "..."}
                 on error   -> {"error": "...", "raw": <optional>}
        """
        path_api = "api/v8/packages/quota-details"
        payload = {"is_enterprise": False, "lang": "en", "family_member_id": ""}

        id_token = tokens.get("id_token")
        if not isinstance(id_token, str) or not id_token.strip():
            return False, {"error": "id_token missing/invalid"}

        try:
            res = send_api_request(api_key, path_api, payload, id_token, "POST", timeout=self.timeout_s)
        except Exception as e:
            return False, {"error": f"request_exception: {type(e).__name__}: {e}"}

        if isinstance(res, dict) and res.get("status") == "SUCCESS":
            data = res.get("data") or {}
            quotas = data.get("quotas", [])
            if not isinstance(quotas, list):
                quotas = []
            return True, {"quotas": quotas, "raw_status": "SUCCESS"}

        # keep some info for debugging, but don’t bloat logs too much
        raw_status = res.get("status") if isinstance(res, dict) else None
        return False, {"error": "api_status_not_success", "raw_status": raw_status}

    def run(self) -> None:
        user = AuthInstance.get_active_user()
        if not user:
            print("❌ Harap login terlebih dahulu.")
            pause()
            return

        api_key = getattr(AuthInstance, "api_key", "") or ""
        if not api_key:
            print("❌ API key kosong. Set env API_KEY/XL_API_KEY/MYXL_API_KEY.")
            pause()
            return

        # Build log file name
        ts_label = datetime.now().strftime("%Y%m%d_%H%M%S")
        number = user.get("number")
        log_path = self.log_dir / f"sentry_{number}_{ts_label}.jsonl"

        self._print_header(number, log_path)

        # Start ENTER listener for TTY
        t = threading.Thread(target=self._input_listener, daemon=True)
        t.start()

        counter = 0
        ok_count = 0
        err_count = 0

        # Stable schedule
        next_tick = time.monotonic()

        try:
            # line-buffered file (still flush each record to be safe on crashes)
            with log_path.open("a", encoding="utf-8") as f:
                while not self.stop_event.is_set():
                    # wait until next tick without drift
                    now_m = time.monotonic()
                    delay = next_tick - now_m
                    if delay > 0:
                        self.stop_event.wait(delay)
                        if self.stop_event.is_set():
                            break

                    next_tick = max(next_tick + self.interval_s, time.monotonic() + 0.001)

                    counter += 1
                    now_hms = datetime.now().strftime("%H:%M:%S")

                    # Refresh active tokens periodically (also allows auto-renew from Auth)
                    # (Cost is small; makes sentry more robust in long runs.)
                    active = AuthInstance.get_active_user() or user
                    tokens = active.get("tokens", {}) if isinstance(active, dict) else {}
                    if not isinstance(tokens, dict):
                        tokens = {}

                    ok, payload = self._fetch_quota(api_key, tokens)

                    if ok:
                        ok_count += 1
                        record = {
                            "ts": _now_iso(),
                            "ok": True,
                            "number": number,
                            "quotas": payload.get("quotas", []),
                        }
                    else:
                        err_count += 1
                        record = {
                            "ts": _now_iso(),
                            "ok": False,
                            "number": number,
                            "error": payload.get("error", "unknown_error"),
                            "raw_status": payload.get("raw_status"),
                        }

                    # Always write JSONL
                    f.write(_safe_json(record) + "\n")
                    f.flush()

                    # Progress output
                    if self._tty:
                        self._progress(f"⏳ [{now_hms}] Fetch #{counter} | OK: {ok_count} | Errors: {err_count}")
                    else:
                        # reduce noise in non-tty logs
                        if counter == 1 or (counter % self.print_every == 0) or (not ok):
                            self._progress(
                                f"[{now_hms}] fetch={counter} ok={ok_count} err={err_count}"
                                + ("" if ok else f" last_error={record.get('error')!s}"),
                                force_newline=True,
                            )

        except KeyboardInterrupt:
            self.stop_event.set()
            print("\n\n🛑 Monitoring dihentikan (Ctrl+C).")
        except Exception as e:
            self.stop_event.set()
            print(f"\n\n❌ Critical Error: {type(e).__name__}: {e}")
        finally:
            # finalize output
            if self._tty:
                # ensure we don't leave a hanging \r line
                print("")
            print("\n✅ Monitoring Selesai.")
            print(f"   Total Fetch: {counter} | OK: {ok_count} | Errors: {err_count}")
            print(f"   File: {log_path}")
            pause()


def enter_sentry_mode() -> None:
    monitor = SentryMonitor()
    monitor.run()