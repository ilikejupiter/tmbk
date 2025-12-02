# -*- coding: utf-8 -*-
"""
Bookmark Manager - stabil & thread-safe (Python 3.9.18)
- Atomic write .tmp -> replace
- Schema auto-migration
- Cegah duplikat
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

class Bookmark:
    _instance = None
    _initialized = False
    _lock = threading.RLock()

    def __new__(cls):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    self.file_path = Path(os.environ.get("MYXL_BOOKMARK_FILE", "bookmark.json"))
                    self.packages: List[Dict[str, Any]] = []
                    self._load()
                    self._initialized = True

    # ------------------------------------------------------------------ IO --
    def _atomic_save(self) -> None:
        tmp = self.file_path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self.packages, f, indent=2, ensure_ascii=False)
            tmp.replace(self.file_path)
        except Exception as e:
            logger.error(f"Gagal menyimpan bookmark: {e}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def _load(self) -> None:
        if not self.file_path.exists():
            self._atomic_save()
            return
        try:
            with self.file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.packages = data
                self._ensure_schema()
            else:
                logger.warning("Format bookmark salah (bukan list). Reset.")
                self.packages = []
                self._atomic_save()
        except json.JSONDecodeError:
            logger.error("bookmark.json corrupt. Membuat backup dan reset.")
            try:
                self.file_path.rename(self.file_path.with_suffix(".bak"))
            except OSError:
                pass
            self.packages = []
            self._atomic_save()
        except Exception as e:
            logger.error(f"Gagal memuat bookmark: {e}")
            self.packages = []

    # -------------------------------------------------------------- schema --
    def _ensure_schema(self) -> None:
        updated = False
        required = {
            "family_code": "",
            "family_name": "Unknown Family",
            "is_enterprise": False,
            "variant_name": "Unknown Variant",
            "option_name": "Unknown Option",
            "order": 0,
        }
        for p in self.packages:
            for k, dv in required.items():
                if k not in p:
                    p[k] = dv
                    updated = True
        if updated:
            logger.info("Schema bookmark diperbarui.")
            self._atomic_save()

    # --------------------------------------------------------------- CRUD --
    def add_bookmark(self, family_code: str, family_name: str, is_enterprise: bool,
                     variant_name: str, option_name: str, order: int) -> bool:
        with self._lock:
            for p in self.packages:
                if p.get("family_code") == family_code and p.get("variant_name") == variant_name and p.get("order") == order:
                    logger.info("Bookmark sudah ada.")
                    return False
            item = {
                "family_code": family_code,
                "family_name": family_name,
                "is_enterprise": is_enterprise,
                "variant_name": variant_name,
                "option_name": option_name,
                "order": int(order),
                "created_at": int(time.time()),
            }
            self.packages.append(item)
            self._atomic_save()
            logger.info("Bookmark ditambahkan.")
            return True

    def remove_bookmark(self, family_code: str, is_enterprise: bool, variant_name: str, order: int) -> bool:
        with self._lock:
            before = len(self.packages)
            self.packages = [
                p for p in self.packages
                if not (
                    p.get("family_code") == family_code and
                    p.get("is_enterprise") == is_enterprise and
                    p.get("variant_name") == variant_name and
                    p.get("order") == int(order)
                )
            ]
            if len(self.packages) < before:
                self._atomic_save()
                logger.info("Bookmark dihapus.")
                return True
            return False

    def get_bookmarks(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.packages)

# Singleton
BookmarkInstance = Bookmark()