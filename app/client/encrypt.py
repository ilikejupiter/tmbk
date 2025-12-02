# path: app/client/encrypt.py
# -*- coding: utf-8 -*-
"""
encrypt.py - modern & stable crypto utilities (Python 3.9+)

Tambahan fitur (dev-friendly, back-compat aman):
- crypto_diagnostics() → ringkasan kesehatan kunci & round-trip.
- decode_xdata_from_string()/decode_xdata_from_file() → dekripsi batch hasil log (mis. hasil.json).
- CLI: --diag, --decode-xdata, --decode-xdata-file, --enc-circle, --dec-circle.

Tidak mengubah output `encrypt_and_sign_xdata(...)` maupun body yang dikirim Engsel
(yaitu hanya {"xdata","xtime"}), sesuai pemakaian di _send_request() .
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from random import randint
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Optional dependency: pycryptodome
try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
except Exception as e:  # pragma: no cover
    AES = None  # type: ignore
    pad = None  # type: ignore
    _CRYPTO_IMPORT_ERR = e
else:
    _CRYPTO_IMPORT_ERR = None

# Helper crypto (project-local)
from app.service.crypto_helper import (
    encrypt_xdata as helper_enc_xdata,
    decrypt_xdata as helper_dec_xdata,
    encrypt_circle_msisdn as helper_enc_msisdn,
    decrypt_circle_msisdn as helper_dec_msisdn,
    make_x_signature,
    make_x_signature_payment,
    make_ax_api_signature,
    make_x_signature_bounty,
    make_x_signature_loyalty,
    make_x_signature_bounty_allotment,
    crypto_self_test as helper_crypto_self_test,
)

# =============================================================================
# ENV + DEFAULTS
# =============================================================================

API_KEY = os.getenv("API_KEY", "")
AES_KEY_ASCII = os.getenv("AES_KEY_ASCII", "")
AX_FP_KEY = os.getenv("AX_FP_KEY", "")
ENCRYPTED_FIELD_KEY = os.getenv("ENCRYPTED_FIELD_KEY", "")

_GMT7 = timezone(timedelta(hours=7))


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass(frozen=True)
class DeviceInfo:
    manufacturer: str
    model: str
    lang: str
    resolution: str
    tz_short: str
    ip: str
    font_scale: float
    android_release: str
    msisdn: str


@dataclass
class CryptoConfig:
    api_key: str = field(default_factory=lambda: API_KEY or "")
    aes_key_ascii: str = field(default_factory=lambda: AES_KEY_ASCII or "")
    ax_fp_key: str = field(default_factory=lambda: AX_FP_KEY or "")
    encrypted_field_key: str = field(default_factory=lambda: ENCRYPTED_FIELD_KEY or "")
    fp_file_path: Path = field(default_factory=lambda: Path("ax.fp"))


# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _crypto_ready() -> bool:
    if AES is None or pad is None:
        logger.error("pycryptodome tidak tersedia. Install: pip install pycryptodome. Root error: %r", _CRYPTO_IMPORT_ERR)
        return False
    return True


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _aes_key_bytes(key: str, *, allow_lengths=(16, 24, 32), encoding="utf-8") -> Optional[bytes]:
    if not key:
        return None
    try:
        b = key.encode(encoding)
    except Exception:
        return None
    return b if len(b) in allow_lengths else None


def _safe_json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        s = str(value).strip()
        return int(s) if s else default
    except Exception:
        return default


# =============================================================================
# CORE SERVICE
# =============================================================================

class EncryptionService:
    """
    Service modern untuk operasi kripto.
    Fokus: stabilitas, kompatibilitas, & error yang jelas.
    """

    def __init__(self, config: Optional[CryptoConfig] = None):
        self.config = config or CryptoConfig()

    # --- Time helpers ---

    def _get_gmt7_now(self) -> datetime:
        return datetime.now(_GMT7)

    # --- Fingerprint ---

    def build_fingerprint_plain(self, dev: DeviceInfo) -> str:
        """
        Bentuk string fingerprint deterministik.
        NOTE: Jangan bocorkan data sensitif ke logs.
        """
        return "|".join(
            [
                dev.manufacturer,
                dev.model,
                dev.lang,
                dev.resolution,
                dev.tz_short,
                dev.ip,
                f"{dev.font_scale:.2f}",
                dev.android_release,
                dev.msisdn,
            ]
        )

    def generate_ax_fingerprint(self, dev: DeviceInfo) -> str:
        """
        AES-CBC(IV=0) fingerprint string → base64.
        Strict (default): len(AX_FP_KEY)==32. Longgar jika MYXL_AX_FP_KEY_STRICT=0.
        """
        if not _crypto_ready():
            return ""
        try:
            strict = os.getenv("MYXL_AX_FP_KEY_STRICT", "1").strip().lower() not in ("0", "false", "no", "off")
            key_str = self.config.ax_fp_key or ""
            if strict:
                if len(key_str) != 32:
                    logger.error("Invalid AX_FP_KEY length (must be 32 chars in strict mode).")
                    return ""
                key_bytes = _aes_key_bytes(key_str, allow_lengths=(32,), encoding="ascii")
            else:
                key_bytes = _aes_key_bytes(key_str, allow_lengths=(16, 24, 32), encoding="utf-8")
            if not key_bytes:
                logger.error("AX_FP_KEY tidak valid (16/24/32 bytes).")
                return ""
            iv = b"\x00" * 16
            pt = self.build_fingerprint_plain(dev).encode("utf-8")
            ct = AES.new(key_bytes, AES.MODE_CBC, iv).encrypt(pad(pt, 16))
            return base64.b64encode(ct).decode("ascii")
        except Exception as e:
            logger.error("Fingerprint generation error: %s", e)
            return ""

    def load_or_create_fingerprint(self) -> str:
        try:
            p = self.config.fp_file_path
            if p.exists():
                content = p.read_text(encoding="utf-8", errors="strict").strip()
                if content and len(content) > 10:
                    return content
            dev = DeviceInfo(
                manufacturer=f"Vertu{randint(1000, 9999)}",
                model=f"Asterion X1 Ultra{randint(1000, 9999)}",
                lang="en",
                resolution="720x1540",
                tz_short="GMT07:00",
                ip="127.0.0.1",
                font_scale=1.0,
                android_release="14",
                msisdn="6281911120078",
            )
            new_fp = self.generate_ax_fingerprint(dev)
            if not new_fp:
                raise ValueError("Generated empty fingerprint (check AX_FP_KEY / pycryptodome).")
            _atomic_write_text(p, new_fp)
            return new_fp
        except Exception as e:
            logger.error("Fingerprint load/create error: %s", e)
            return "default_fingerprint_fallback_error"

    def get_ax_device_id(self) -> str:
        fp = self.load_or_create_fingerprint()
        return hashlib.md5(fp.encode("utf-8")).hexdigest()

    # --- Encrypted field ---

    def build_encrypted_field(self, iv_hex16: Optional[str] = None, urlsafe_b64: bool = False) -> str:
        """
        Encrypt empty padded block (legacy logic), return base64(ct) + iv_hex(16 chars).
        """
        if not _crypto_ready():
            return ""
        try:
            key_b = _aes_key_bytes(self.config.encrypted_field_key or "", allow_lengths=(16, 24, 32), encoding="utf-8")
            if not key_b:
                logger.warning("ENCRYPTED_FIELD_KEY missing/invalid. Must be 16/24/32 bytes.")
                return ""
            iv_hex = (iv_hex16 or secrets.token_hex(8)).strip()
            if len(iv_hex) != 16 or any(c not in "0123456789abcdefABCDEF" for c in iv_hex):
                raise ValueError("IV must be exactly 16 hex characters.")
            iv = iv_hex.encode("ascii", errors="strict")
            pt = pad(b"", 16)
            ct = AES.new(key_b, AES.MODE_CBC, iv=iv).encrypt(pt)
            encoder = base64.urlsafe_b64encode if urlsafe_b64 else base64.b64encode
            return encoder(ct).decode("ascii") + iv_hex
        except Exception as e:
            logger.error("Build encrypted field error: %s", e)
            return ""

    # --- Timestamp formats ---

    def java_like_timestamp(self, now: datetime) -> str:
        try:
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            ms2 = f"{int(now.microsecond / 10000):02d}"
            tz = now.strftime("%z")
            tz_colon = f"{tz[:-2]}:{tz[-2:]}" if tz else "+00:00"
            return now.strftime(f"%Y-%m-%dT%H:%M:%S.{ms2}") + tz_colon
        except Exception:
            return now.isoformat()

    def ts_gmt7_without_colon(self, dt: datetime) -> str:
        try:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_GMT7)
            else:
                dt = dt.astimezone(_GMT7)
            millis = f"{int(dt.microsecond / 1000):03d}"
            tz = dt.strftime("%z")
            return dt.strftime(f"%Y-%m-%dT%H:%M:%S.{millis}") + tz
        except Exception as e:
            logger.error("ts_gmt7_without_colon error: %s", e)
            return ""

    # --- XDATA ---

    def encrypt_and_sign_xdata(self, method: str, path: str, id_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Return {"x_signature": str, "encrypted_body": {"xdata": str, "xtime": int}}
        """
        try:
            plain_body = _safe_json_dumps(payload)
            xtime = int(time.time() * 1000)
            xdata = helper_enc_xdata(plain_body, xtime)
            if not xdata:
                raise ValueError("Encryption failed from helper (empty result)")
            sig_time_sec = xtime // 1000
            x_sig = make_x_signature(id_token, method, path, sig_time_sec)
            if not x_sig:
                raise ValueError("Signature generation failed")
            return {"x_signature": x_sig, "encrypted_body": {"xdata": xdata, "xtime": xtime}}
        except Exception as e:
            logger.error("EncryptSign XData error: %s", e)
            return {}

    def decrypt_xdata_payload(self, encrypted_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if not isinstance(encrypted_payload, dict):
                raise ValueError(f"Invalid payload type: {type(encrypted_payload)}")
            xdata = encrypted_payload.get("xdata")
            xtime = encrypted_payload.get("xtime")
            if not xdata or xtime is None:
                raise ValueError("Missing xdata or xtime in payload")
            xtime_i = _safe_int(xtime)
            if xtime_i is None:
                raise ValueError("xtime must be int-like")
            plaintext = helper_dec_xdata(str(xdata), int(xtime_i))  # helper returns "{}" on failure
            try:
                parsed = json.loads(plaintext)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        except Exception as e:
            logger.error("Decrypt XData error: %s", e)
            return {}

    # --- DEV/OPS: Diagnostics & Tools ---

    def crypto_diagnostics(self) -> Dict[str, Any]:
        """
        Ringkas kondisi kripto (tanpa bocorkan secret).
        """
        try:
            fp_path = str(self.config.fp_file_path)
            fp = self.load_or_create_fingerprint()
            device_id = hashlib.md5(fp.encode("utf-8")).hexdigest()
            now = datetime.now(timezone.utc).astimezone()
            report = helper_crypto_self_test()
            return {
                "pycryptodome_ok": _crypto_ready(),
                "fingerprint_file": fp_path,
                "device_id_prefix": device_id[:12],
                "ts_java_like": self.java_like_timestamp(now),
                "ts_gmt7_no_colon": self.ts_gmt7_without_colon(now),
                "self_test": report,
            }
        except Exception as e:
            logger.error("Diagnostics error: %s", e)
            return {"pycryptodome_ok": False, "error": str(e)}

    # --- DEV/OPS: Log decoder ---

    @staticmethod
    def _extract_xdata_pairs(text: str) -> List[Tuple[str, int]]:
        """
        Cari pasangan ("xdata": "...", "xtime": <int>) di string bebas.
        Mengembalikan list pair dalam urutan kemunculan.
        """
        if not text:
            return []
        # long, greedy-safe patterns
        xdata_re = re.compile(r'"xdata"\s*:\s*"([^"]+)"')
        xtime_re = re.compile(r'"xtime"\s*:\s*(\d{5,})')
        xs = [m.group(1) for m in xdata_re.finditer(text)]
        ts = [int(m.group(1)) for m in xtime_re.finditer(text)]
        # Pairing by index order (best-effort)
        n = min(len(xs), len(ts))
        return list(zip(xs[:n], ts[:n]))

    def decode_xdata_from_string(self, raw: str) -> List[Dict[str, Any]]:
        """
        Decode semua pasangan xdata/xtime dalam string mentah → list of dict.
        """
        out: List[Dict[str, Any]] = []
        for xdata, xtime in self._extract_xdata_pairs(raw):
            pt = helper_dec_xdata(xdata, xtime)
            try:
                out.append(json.loads(pt))
            except Exception:
                out.append({})
        return out

    def decode_xdata_from_file(self, path: str | Path) -> List[Dict[str, Any]]:
        """
        Baca file (mis. 'hasil.json'), decode semua xdata/xtime yang ditemukan.
        """
        try:
            txt = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger.error("Cannot read file %s: %s", path, e)
            return []
        return self.decode_xdata_from_string(txt)


# =============================================================================
# COMPATIBILITY LAYER (legacy function names/signatures)
# =============================================================================

_service = EncryptionService()

def load_ax_fp() -> str:
    return _service.load_or_create_fingerprint()

def ax_device_id() -> str:
    return _service.get_ax_device_id()

def ax_fingerprint(dev: DeviceInfo, secret_key: str) -> str:
    return _service.generate_ax_fingerprint(dev)

def build_encrypted_field(iv_hex16: Optional[str] = None, urlsafe_b64: bool = False) -> str:
    return _service.build_encrypted_field(iv_hex16, urlsafe_b64)

def java_like_timestamp(now: datetime) -> str:
    return _service.java_like_timestamp(now)

def ts_gmt7_without_colon(dt: datetime) -> str:
    return _service.ts_gmt7_without_colon(dt)

def ax_api_signature(api_key: str, ts_for_sign: str, contact: str, code: str, contact_type: str) -> str:
    return make_ax_api_signature(ts_for_sign, contact, code, contact_type)

def encryptsign_xdata(api_key: str, method: str, path: str, id_token: str, payload: dict) -> dict:
    return _service.encrypt_and_sign_xdata(method, path, id_token, payload if isinstance(payload, dict) else {})  # type: ignore

def decrypt_xdata(api_key: str, encrypted_payload: dict) -> dict:
    return _service.decrypt_xdata_payload(encrypted_payload if isinstance(encrypted_payload, dict) else {})  # type: ignore

def encrypt_circle_msisdn(api_key: str, msisdn: str) -> str:
    return helper_enc_msisdn(msisdn)

def decrypt_circle_msisdn(api_key: str, encrypted_msisdn_b64: str) -> str:
    return helper_dec_msisdn(encrypted_msisdn_b64)

def get_x_signature_payment(api_key: str, access_token: str, sig_time_sec: int, package_code: str, token_payment: str, payment_method: str, payment_for: str, path: str) -> str:
    return make_x_signature_payment(access_token, sig_time_sec, package_code, token_payment, payment_method, payment_for, path)

def get_x_signature_bounty(api_key: str, access_token: str, sig_time_sec: int, package_code: str, token_payment: str) -> str:
    return make_x_signature_bounty(access_token, sig_time_sec, package_code, token_payment)

def get_x_signature_loyalty(api_key: str, sig_time_sec: int, package_code: str, token_confirmation: str, path: str) -> str:
    return make_x_signature_loyalty(sig_time_sec, package_code, token_confirmation, path)

def get_x_signature_bounty_allotment(api_key: str, sig_time_sec: int, package_code: str, token_confirmation: str, destination_msisdn: str, path: str) -> str:
    return make_x_signature_bounty_allotment(sig_time_sec, package_code, token_confirmation, path, destination_msisdn)


# =============================================================================
# CLI (devtools)
# =============================================================================

def _build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="encrypt.py", description="Crypto Devtools (diagnose & decode xdata)")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("diag", help="Tampilkan crypto diagnostics")

    d1 = sub.add_parser("decode-xdata", help="Decode semua pasangan xdata/xtime dari string mentah")
    d1.add_argument("payload", help="String mentah yang mengandung xdata/xtime (mis. hasil dump)")

    d2 = sub.add_parser("decode-xdata-file", help="Decode xdata/xtime dari file (mis. hasil.json)")
    d2.add_argument("path", help="Path file")

    e1 = sub.add_parser("enc-circle", help="Enkripsi MSISDN (Circle)")
    e1.add_argument("msisdn", help="Nomor ponsel")

    e2 = sub.add_parser("dec-circle", help="Dekripsi MSISDN terenkripsi (Circle)")
    e2.add_argument("cipher", help="urlsafe_b64(cipher)+iv_hex16")

    return p


def _main_cli(argv: Optional[List[str]] = None) -> int:
    args = _build_cli().parse_args(argv)
    svc = _service

    if args.cmd == "diag":
        print(json.dumps(svc.crypto_diagnostics(), indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "decode-xdata":
        res = svc.decode_xdata_from_string(args.payload)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "decode-xdata-file":
        res = svc.decode_xdata_from_file(args.path)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "enc-circle":
        print(encrypt_circle_msisdn("", args.msisdn))
        return 0

    if args.cmd == "dec-circle":
        print(decrypt_circle_msisdn("", args.cipher))
        return 0

    _build_cli().print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(_main_cli())