# -*- coding: utf-8 -*-
"""
Decoy Manager - modern, stable, context-aware (Python 3.9.18)

Perbaikan utama:
- Thread-safe (RLock) untuk cache + context switch
- Cache TTL pakai time.monotonic() (lebih stabil dari time.time())
- Validasi config JSON lebih ketat + error log lebih jelas
- Prefix "prio-" / "default-" ditentukan secara konsisten (case-insensitive)
- Output ke terminal (print) dijadikan opsional (default: off) via env
- API publik tetap: DecoyPackage.get_decoy(decoy_type) + DecoyInstance
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.client.engsel import get_package_details
from app.service.auth import AuthInstance

logger = logging.getLogger(__name__)


class DecoyPackage:
    _instance_: "DecoyPackage" = None
    _initialized_ = False

    DECOY_DIR = Path("decoy_data")
    PRIO_TYPES = {"PRIORITAS", "PRIOHYBRID", "GO"}  # gunakan set untuk lookup cepat
    CACHE_TTL_SECONDS = 300  # 5 menit

    # decoy types yang valid (kunci file -> "decoy-<prefix><type>.json")
    VALID_TYPES = {"balance", "qris", "qris0"}

    # env: kalau benar, akan print warning yang tadinya ada di versi lama
    PRINT_WARN_ENV = "MYXL_DECOY_PRINT_WARN"

    def __new__(cls, *args, **kwargs):
        if not cls._instance_:
            cls._instance_ = super().__new__(cls)
        return cls._instance_

    def __init__(self):
        if self._initialized_:
            return

        self._lock = threading.RLock()

        self.current_sub_id: Optional[str] = None
        self.file_prefix: str = "default-"

        # cache: key -> {"expires_at": float(monotonic), "data": {...}}
        self.cache: Dict[str, Dict[str, Any]] = {}

        try:
            self.DECOY_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Kalau filesystem read-only/permission issue, jangan crash.
            logger.debug("Could not create decoy directory '%s'.", self.DECOY_DIR, exc_info=True)

        self._initialized_ = True

    # ------------------------------------------------------------------ ctx --
    @staticmethod
    def _is_prio_type(sub_type: Any) -> bool:
        s = str(sub_type or "").strip().upper()
        return s in DecoyPackage.PRIO_TYPES

    def _refresh_context(self) -> None:
        """
        Update context berdasarkan user aktif:
        - prefix "prio-" jika subscription_type termasuk PRIO_TYPES
        - clear cache saat subscriber berubah atau prefix berubah
        """
        user = AuthInstance.get_active_user()
        if not user:
            return

        sub_id = user.get("subscriber_id")
        sub_type = user.get("subscription_type", "")

        # Normalisasi sub_id agar stabil untuk perbandingan
        sub_id_norm = str(sub_id).strip() if sub_id is not None else None
        new_prefix = "prio-" if self._is_prio_type(sub_type) else "default-"

        with self._lock:
            if sub_id_norm != self.current_sub_id or new_prefix != self.file_prefix:
                self.current_sub_id = sub_id_norm
                self.file_prefix = new_prefix
                self.cache.clear()
                logger.info("Decoy context switched to: %s (%s)", self.file_prefix, str(sub_type))

    # --------------------------------------------------------------- config --
    def _config_path(self, decoy_type: str) -> Path:
        # contoh: decoy-default-qris.json / decoy-prio-balance.json
        file_name = f"decoy-{self.file_prefix}{decoy_type}.json"
        return self.DECOY_DIR / file_name

    @staticmethod
    def _safe_read_json(path: Path) -> Optional[Dict[str, Any]]:
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.error("Decoy config must be a JSON object/dict: %s", path)
                return None
            return data
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            logger.error("Decoy config JSON invalid/corrupt: %s", path, exc_info=True)
            return None
        except Exception:
            logger.error("Failed reading decoy config: %s", path, exc_info=True)
            return None

    @staticmethod
    def _validate_config(cfg: Dict[str, Any], full_key: str) -> Optional[Dict[str, Any]]:
        """
        Wajib: family_code, variant_code, order
        Optional: is_enterprise(bool), migration_type(str), price(int-ish)
        """
        missing = [k for k in ("family_code", "variant_code", "order") if k not in cfg]
        if missing:
            logger.error("Config decoy '%s' missing keys: %s", full_key, missing)
            return None

        family_code = str(cfg.get("family_code") or "").strip()
        variant_code = str(cfg.get("variant_code") or "").strip()

        # order harus int >= 1
        try:
            order = int(cfg.get("order"))
        except Exception:
            logger.error("Config decoy '%s' field 'order' must be int.", full_key)
            return None
        if order < 1:
            order = 1

        is_enterprise = bool(cfg.get("is_enterprise", False))
        migration_type = str(cfg.get("migration_type", "NONE") or "NONE").strip() or "NONE"

        # price optional; jika invalid -> 0
        try:
            price = int(cfg.get("price", 0))
        except Exception:
            price = 0
        if price < 0:
            price = 0

        if not family_code or not variant_code:
            logger.error("Config decoy '%s' family_code/variant_code cannot be empty.", full_key)
            return None

        return {
            "family_code": family_code,
            "variant_code": variant_code,
            "order": order,
            "is_enterprise": is_enterprise,
            "migration_type": migration_type,
            "price": price,
        }

    def _maybe_print_warn(self, msg: str) -> None:
        # default OFF (lebih stabil untuk library/CI)
        v = (Path.cwd(),)  # dummy refer, no-op
        _ = v  # silence linters
        val = str((__import__("os").environ.get(self.PRINT_WARN_ENV, "") or "")).strip().lower()
        if val in ("1", "true", "yes", "on"):
            try:
                print(msg)
            except Exception:
                pass

    # --------------------------------------------------------------- public --
    def get_decoy(self, decoy_type: str) -> Optional[Dict[str, Any]]:
        """
        Return dict:
          {
            "option_code": str,
            "price": int,
            "name": str,
            "token_confirmation": str
          }
        atau None jika gagal.
        """
        # Pastikan context sesuai user aktif
        self._refresh_context()

        if decoy_type not in self.VALID_TYPES:
            logger.warning("Unknown decoy type: %s", decoy_type)
            return None

        full_key = f"{self.file_prefix}{decoy_type}"

        # 1) Cache
        now_m = time.monotonic()
        with self._lock:
            cached = self.cache.get(full_key)
            if cached and now_m < float(cached.get("expires_at", 0.0)):
                data = cached.get("data")
                return data if isinstance(data, dict) else None

        # 2) Read config
        cfg_path = self._config_path(decoy_type)
        cfg_raw = self._safe_read_json(cfg_path)
        if cfg_raw is None:
            logger.error("Decoy config missing/unreadable: %s", cfg_path)
            self._maybe_print_warn(
                f"⚠️  File decoy '{cfg_path.name}' tidak ditemukan / tidak bisa dibaca di folder '{self.DECOY_DIR}'."
            )
            return None

        cfg = self._validate_config(cfg_raw, full_key)
        if cfg is None:
            return None

        # 3) Fetch detail dari API
        try:
            api_key = getattr(AuthInstance, "api_key", "") or ""
            tokens = AuthInstance.get_active_tokens()
            if not api_key:
                logger.warning("API key is empty; cannot fetch decoy.")
                return None
            if not tokens:
                logger.warning("No active tokens; cannot fetch decoy.")
                return None

            pkg_detail = get_package_details(
                api_key,
                tokens,
                cfg["family_code"],
                cfg["variant_code"],
                int(cfg["order"]),
                bool(cfg["is_enterprise"]),
                str(cfg["migration_type"]),
            )
            if not isinstance(pkg_detail, dict) or not pkg_detail:
                logger.error("Package detail empty/invalid for decoy %s.", full_key)
                return None

            opt = pkg_detail.get("package_option") or {}
            if not isinstance(opt, dict):
                opt = {}

            option_code = str(opt.get("package_option_code") or "").strip()
            if not option_code:
                logger.error("Option code missing in API response for decoy %s.", full_key)
                return None

            result = {
                "option_code": option_code,
                "price": int(cfg["price"]),
                "name": str(opt.get("name") or "Unknown Decoy"),
                "token_confirmation": str(pkg_detail.get("token_confirmation") or ""),
            }

            # 4) Store cache
            with self._lock:
                self.cache[full_key] = {
                    "expires_at": time.monotonic() + self.CACHE_TTL_SECONDS,
                    "data": result,
                }

            logger.info("Decoy %s updated successfully.", full_key)
            return result

        except Exception:
            logger.error("Error processing decoy %s.", full_key, exc_info=True)
            return None


# Singleton (API tetap)
DecoyInstance = DecoyPackage()