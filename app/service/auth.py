# -*- coding: utf-8 -*-
"""
Auth Singleton - modern & stable (Python 3.9.18)

Fokus stabilitas:
- Atomic read/write untuk refresh-tokens.json & active.number
- Thread-safe (RLock) + best-effort inter-process lock (lockfile)
- Auto-backup jika JSON corrupt
- Auto-refresh token (default 240 detik) pakai time.monotonic()
- API publik tetap sama: Auth, AuthInstance dan metode-metodenya

NOTE:
- Module ini tidak meng-setup logging global (biar tidak ganggu aplikasi).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from app.client.ciam import get_new_token
from app.client.engsel import get_profile
from app.util import ensure_api_key

logger = logging.getLogger(__name__)

_REFRESH_INTERVAL_SECONDS = int(os.environ.get("MYXL_TOKEN_REFRESH_SECONDS", "240") or "240")
_TOKEN_FILE_ENV = "MYXL_TOKEN_FILE"
_ACTIVE_FILE_ENV = "MYXL_ACTIVE_FILE"


# =============================================================================
# Low-level helpers (atomic IO + inter-process lock)
# =============================================================================

def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        s = str(value).strip()
        if not s:
            return None
        return int(s)
    except Exception:
        return None


@contextlib.contextmanager
def _interprocess_lock(lock_path: Path, *, timeout_s: float = 4.0, poll_s: float = 0.05):
    """
    Best-effort inter-process lock using a lock file.
    - Unix: fcntl.flock
    - Windows: msvcrt.locking
    If locking fails, we continue without hard failing (stability > deadlock).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = None
    locked = False
    start = time.monotonic()

    try:
        fh = lock_path.open("a+", encoding="utf-8")
        while (time.monotonic() - start) < timeout_s:
            try:
                if os.name == "nt":
                    import msvcrt  # type: ignore
                    fh.seek(0)
                    # lock 1 byte (best-effort)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl  # type: ignore
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except Exception:
                time.sleep(poll_s)

        if not locked:
            logger.debug("Inter-process lock not acquired (%s). Continuing without it.", lock_path)

        yield

    finally:
        if fh is not None:
            if locked:
                with contextlib.suppress(Exception):
                    if os.name == "nt":
                        import msvcrt  # type: ignore
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl  # type: ignore
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                fh.close()


def _atomic_write_text(path: Path, text: str) -> None:
    """
    Atomic write:
    write to .tmp then replace target (safe against partial write).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def _backup_corrupt_file(path: Path) -> None:
    """
    Backup file corrupt -> rename with timestamp.
    """
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = path.with_suffix(path.suffix + f".bak.{ts}")
        path.rename(bak)
    except Exception:
        # If rename fails, ignore to avoid breaking flow
        pass


def _read_json_list(path: Path) -> List[Any]:
    """
    Read JSON list from file with graceful fallback.
    """
    try:
        raw = _read_text(path)
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        logger.warning("Token file '%s' format bukan list. Reset ke [].", path)
        return []
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        logger.error("Token file '%s' JSON corrupt. Backup dan reset.", path)
        _backup_corrupt_file(path)
        return []
    except Exception as e:
        logger.error("Gagal membaca token file '%s': %s", path, e)
        return []


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, indent=4, ensure_ascii=False, sort_keys=True)


# =============================================================================
# Auth Singleton
# =============================================================================

class Auth:
    _instance_: "Auth" = None
    _initialized_ = False
    _lock = threading.RLock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance_:
            with cls._lock:
                if not cls._instance_:
                    cls._instance_ = super().__new__(cls)
        return cls._instance_

    def __init__(self):
        if self._initialized_:
            return
        with self._lock:
            if self._initialized_:
                return

            self.api_key = ensure_api_key()
            self.refresh_tokens: List[Dict[str, Any]] = []
            self.active_user: Optional[Dict[str, Any]] = None

            # Monotonic supaya tahan perubahan jam sistem
            self.last_refresh_time = time.monotonic()

            # File locations (tetap sama default)
            self.token_path = Path(os.environ.get(_TOKEN_FILE_ENV, "refresh-tokens.json"))
            self.active_user_path = Path(os.environ.get(_ACTIVE_FILE_ENV, "active.number"))

            # lockfile (untuk multi-process safety)
            self._token_lock_path = self.token_path.with_suffix(self.token_path.suffix + ".lock")
            self._active_lock_path = self.active_user_path.with_suffix(self.active_user_path.suffix + ".lock")

            self._pending_active_number: Optional[int] = None  # lazy select
            self._load_tokens()
            self._load_active_number()

            self._initialized_ = True

    # -------------------------------------------------------------------------
    # Load / Save
    # -------------------------------------------------------------------------

    def _load_tokens(self) -> None:
        with _interprocess_lock(self._token_lock_path):
            items = _read_json_list(self.token_path)

        cleaned: List[Dict[str, Any]] = []
        for t in items:
            if not isinstance(t, Mapping):
                continue
            n = _safe_int(t.get("number"))
            rt = t.get("refresh_token")
            if n is None or not isinstance(rt, str) or not rt.strip():
                continue

            cleaned.append({
                "number": int(n),
                "subscriber_id": str(t.get("subscriber_id") or "").strip(),
                "subscription_type": str(t.get("subscription_type") or "PREPAID").strip() or "PREPAID",
                "refresh_token": rt.strip(),
            })

        self.refresh_tokens = cleaned

        # Kalau ada data tapi semuanya invalid -> tulis ulang agar file bersih
        if items and not cleaned:
            self._save_tokens()

    def _save_tokens(self) -> None:
        with self._lock:
            payload = _json_dumps(self.refresh_tokens)
            with _interprocess_lock(self._token_lock_path):
                try:
                    _atomic_write_text(self.token_path, payload)
                except Exception as e:
                    logger.error("Gagal menulis token file '%s': %s", self.token_path, e)

    def _load_active_number(self) -> None:
        with _interprocess_lock(self._active_lock_path):
            try:
                raw = self.active_user_path.read_text(encoding="utf-8").strip()
                if raw:
                    self._pending_active_number = int(raw)
            except Exception:
                self._pending_active_number = None

    def _write_active_file(self, number: int) -> None:
        with _interprocess_lock(self._active_lock_path):
            with contextlib.suppress(Exception):
                _atomic_write_text(self.active_user_path, f"{int(number)}")

    def _clear_active_file(self) -> None:
        with _interprocess_lock(self._active_lock_path):
            with contextlib.suppress(Exception):
                if self.active_user_path.exists():
                    self.active_user_path.unlink()

    # -------------------------------------------------------------------------
    # Public API (dipertahankan)
    # -------------------------------------------------------------------------

    def load_tokens(self):
        """Back-compat: alias untuk _load_tokens()."""
        with self._lock:
            self._load_tokens()

    def add_refresh_token(self, number: int, refresh_token: str) -> bool:
        """
        Menambah/menimpa token user + set aktif.
        API tetap: return bool.
        """
        n = _safe_int(number)
        if n is None or not refresh_token or not str(refresh_token).strip():
            return False

        if not self.api_key:
            logger.error("API key kosong. Set env API_KEY/XL_API_KEY/MYXL_API_KEY.")
            return False

        with self._lock:
            try:
                # Ambil token pertama untuk dapat access_token/id_token
                temp_tokens = get_new_token(self.api_key, str(refresh_token).strip(), "")
                if not temp_tokens:
                    logger.error("Token awal tidak valid/expired.")
                    return False

                profile_data = get_profile(self.api_key, temp_tokens["access_token"], temp_tokens["id_token"])
                if not profile_data:
                    logger.error("Gagal mengambil profil user.")
                    return False

                prof = profile_data.get("profile", {}) if isinstance(profile_data, dict) else {}
                sub_id = str(prof.get("subscriber_id") or "").strip()
                sub_type = str(prof.get("subscription_type") or "PREPAID").strip() or "PREPAID"

                new_entry = {
                    "number": int(n),
                    "subscriber_id": sub_id,
                    "subscription_type": sub_type,
                    "refresh_token": str(temp_tokens.get("refresh_token") or refresh_token).strip(),
                }

                existing = next((rt for rt in self.refresh_tokens if rt.get("number") == int(n)), None)
                if existing:
                    existing.update(new_entry)
                else:
                    self.refresh_tokens.append(new_entry)

                self._save_tokens()
                self.set_active_user(int(n))
                return True

            except Exception as e:
                logger.error("Error adding token: %s", e)
                return False

    def remove_refresh_token(self, number: int) -> bool:
        n = _safe_int(number)
        if n is None:
            return False

        with self._lock:
            initial = len(self.refresh_tokens)
            self.refresh_tokens = [rt for rt in self.refresh_tokens if rt.get("number") != int(n)]

            if len(self.refresh_tokens) >= initial:
                return False

            self._save_tokens()

            if self.active_user and self.active_user.get("number") == int(n):
                self.active_user = None
                if self.refresh_tokens:
                    self.set_active_user(self.refresh_tokens[0]["number"])
                else:
                    self._clear_active_file()
            return True

    def set_active_user(self, number: int) -> bool:
        n = _safe_int(number)
        if n is None:
            return False

        if not self.api_key:
            logger.error("API key kosong. Set env API_KEY/XL_API_KEY/MYXL_API_KEY.")
            return False

        with self._lock:
            target = next((rt for rt in self.refresh_tokens if rt.get("number") == int(n)), None)
            if not target:
                logger.error("User %s tidak ditemukan.", n)
                return False

            try:
                tokens = get_new_token(
                    self.api_key,
                    str(target.get("refresh_token") or "").strip(),
                    str(target.get("subscriber_id") or "").strip(),
                )
                if not tokens:
                    logger.error("Gagal refresh token saat switch user.")
                    return False

                target["refresh_token"] = str(tokens.get("refresh_token") or target.get("refresh_token") or "").strip()
                self._save_tokens()

                self.active_user = {
                    "number": int(n),
                    "subscriber_id": str(target.get("subscriber_id") or "").strip(),
                    "subscription_type": str(target.get("subscription_type") or "PREPAID").strip() or "PREPAID",
                    "tokens": tokens,
                }
                self.last_refresh_time = time.monotonic()
                self._write_active_file(int(n))
                return True

            except Exception as e:
                logger.error("Gagal set active user: %s", e)
                return False

    def get_active_user(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            # Lazy pilih aktif dari file active.number
            if not self.active_user and self._pending_active_number is not None and self.refresh_tokens:
                if not self.set_active_user(self._pending_active_number):
                    # fallback user pertama
                    if self.refresh_tokens:
                        self.set_active_user(self.refresh_tokens[0]["number"])
                self._pending_active_number = None

            # Kalau belum ada aktif tapi ada token -> pilih pertama
            if not self.active_user and self.refresh_tokens:
                self.set_active_user(self.refresh_tokens[0]["number"])

            if not self.active_user:
                return None

            # Auto-refresh jika sudah melewati interval
            if (time.monotonic() - self.last_refresh_time) > _REFRESH_INTERVAL_SECONDS:
                logger.info("Token kedaluwarsa, memperbarui…")
                if not self._renew_active_token():
                    logger.warning("Gagal memperbarui token otomatis.")

            return self.active_user

    def get_active_tokens(self):
        u = self.get_active_user()
        return u["tokens"] if u else None

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    def _renew_active_token(self) -> bool:
        if not self.api_key:
            return False

        with self._lock:
            if not self.active_user:
                return False

            try:
                tokens_cur = self.active_user.get("tokens") or {}
                rt = tokens_cur.get("refresh_token")
                sub_id = self.active_user.get("subscriber_id", "")

                if not isinstance(rt, str) or not rt.strip():
                    logger.error("Renew token gagal: refresh_token kosong di active_user.")
                    return False

                tokens = get_new_token(self.api_key, rt.strip(), str(sub_id or "").strip())
                if not tokens:
                    return False

                self.active_user["tokens"] = tokens
                self.last_refresh_time = time.monotonic()

                # Update refresh token di list
                new_rt = str(tokens.get("refresh_token") or "").strip()
                if new_rt:
                    for u in self.refresh_tokens:
                        if u.get("number") == self.active_user.get("number"):
                            u["refresh_token"] = new_rt
                            break
                    self._save_tokens()

                return True

            except Exception as e:
                logger.error("Renew token error: %s", e)
                return False


# Singleton instance (API lama dipertahankan)
AuthInstance = Auth()