# path: app/service/crypto_helper.py
# -*- coding: utf-8 -*-
"""
Crypto Helpers - modern & resilient (Python 3.9+)

Tambahan fitur:
- Multi-key decrypt untuk XDATA (key rotation) via env `XDATA_KEYS` dan/atau file `xdata.keys`.
- Dukungan format kunci: 'hex:...' dan 'b64:...' selain string utf-8 biasa.
- Self-test util untuk diagnosa cepat.

Back-compat:
- encrypt_xdata/decrypt_xdata shape & perilaku tetap.
- Circle MSISDN encrypt/decrypt tetap urlsafe_b64(cipher) + iv_hex16.

Env yang dipakai:
- XDATA_KEY                : kunci utama XDATA (dipakai untuk encrypt)
- XDATA_KEYS               : kandidat tambahan untuk decrypt (comma-separated)
- AX_API_SIG_KEY           : HMAC AX-API-Signature
- X_API_BASE_SECRET        : HMAC X-Signature
- ENCRYPTED_FIELD_KEY      : kunci AES untuk Circle MSISDN
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from pathlib import Path
from typing import Generator, Iterable, Optional

from base64 import urlsafe_b64decode, urlsafe_b64encode
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

logger = logging.getLogger(__name__)

# Env secrets
XDATA_KEY = os.getenv("XDATA_KEY", "")                   # AES key for xdata (encrypt uses this)
AX_API_SIG_KEY = os.getenv("AX_API_SIG_KEY", "")         # HMAC key (base64 output)
X_API_BASE_SECRET = os.getenv("X_API_BASE_SECRET", "")   # HMAC base secret
ENCRYPTED_FIELD_KEY = os.getenv("ENCRYPTED_FIELD_KEY", "")  # AES key for circle

_BLOCK_SIZE = 16


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _key_bytes(key_str: str) -> Optional[bytes]:
    """
    Decode key string → bytes (support flexible formats) & valid length.
    Accepted:
      - "hex:...." -> hex decode
      - "b64:...." -> base64 decode (std)
      - raw string -> utf-8 bytes
    Valid lengths: 16/24/32 bytes (AES-128/192/256).
    """
    if not key_str:
        return None
    s = str(key_str).strip()
    try:
        if s.lower().startswith("hex:"):
            kb = bytes.fromhex(s[4:].strip())
        elif s.lower().startswith("b64:"):
            kb = base64.b64decode(s[4:].strip(), validate=False)
        else:
            kb = s.encode("utf-8")
    except Exception:
        return None
    return kb if len(kb) in (16, 24, 32) else None


def _fix_b64(s: str) -> str:
    if not s:
        return ""
    s2 = "".join(str(s).split())  # remove whitespace/newlines
    pad_len = (4 - (len(s2) % 4)) % 4
    return s2 + ("=" * pad_len)


def derive_iv(xtime_ms: int) -> bytes:
    """
    Derive IV deterministik dari timestamp (kompat lama):
    ASCII dari 16 char pertama sha256(xtime_ms).
    """
    try:
        sha_hex = hashlib.sha256(str(int(xtime_ms)).encode("utf-8")).hexdigest()
        iv = sha_hex[:16].encode("ascii")
        return iv if len(iv) == _BLOCK_SIZE else (b"0" * _BLOCK_SIZE)
    except Exception as e:
        logger.error("Error deriving IV: %s", e)
        return b"0" * _BLOCK_SIZE


def _safe_unpad(data: bytes) -> Optional[bytes]:
    try:
        return unpad(data, _BLOCK_SIZE, style="pkcs7")
    except ValueError:
        return None


def _hmac_sha512_hex(key_str: str, msg_str: str) -> str:
    try:
        return hmac.new(key_str.encode("utf-8"), msg_str.encode("utf-8"), hashlib.sha512).hexdigest()
    except Exception as e:
        logger.error("HMAC Generation Failed: %s", e)
        return ""


def _iter_candidate_keys() -> Generator[bytes, None, None]:
    """
    Yields kandidat kunci untuk decrypt XDATA:
    1) XDATA_KEY
    2) Semua dari env XDATA_KEYS (comma-separated)
    3) Setiap baris file 'xdata.keys' (jika ada)
    Duplikasi & invalid akan diskip.
    """
    seen: set[bytes] = set()

    def _push_one(raw: str):
        kb = _key_bytes(raw)
        if kb and kb not in seen:
            seen.add(kb)
            yield kb

    # 1) utama
    if XDATA_KEY:
        yield from _push_one(XDATA_KEY)

    # 2) env list
    extra = os.getenv("XDATA_KEYS", "")
    for item in (extra.split(",") if extra else []):
        item_s = item.strip()
        if item_s:
            yield from _push_one(item_s)

    # 3) file list
    p = Path("xdata.keys")
    if p.exists():
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                yield from _push_one(s)
        except Exception as e:
            logger.warning("Failed reading xdata.keys: %s", e)


# ---------------------------------------------------------------------------
# XDATA AES-CBC
# ---------------------------------------------------------------------------

def encrypt_xdata(plaintext: str, xtime_ms: int) -> str:
    """
    Encrypt plaintext -> urlsafe base64 ciphertext.
    Returns "" on failure (backward compatible).
    """
    try:
        if not plaintext:
            return ""
        key = _key_bytes(XDATA_KEY)
        if not key:
            logger.error("XDATA_KEY tidak valid atau kosong (butuh 16/24/32 bytes).")
            return ""
        iv = derive_iv(xtime_ms)
        ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext.encode("utf-8"), _BLOCK_SIZE, style="pkcs7"))
        return urlsafe_b64encode(ct).decode("ascii")
    except Exception as e:
        logger.error("Encrypt XData Failed: %s", e)
        return ""


def _try_decrypt_with_key(ct_b64: str, xtime_ms: int, key: bytes) -> Optional[str]:
    try:
        iv = derive_iv(xtime_ms)
        ct = urlsafe_b64decode(_fix_b64(ct_b64))
        pt_padded = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
        pt = _safe_unpad(pt_padded)
        return pt.decode("utf-8", errors="strict") if pt is not None else None
    except Exception:
        return None


def decrypt_xdata(xdata: str, xtime_ms: int) -> str:
    """
    Decrypt urlsafe base64 ciphertext -> plaintext.
    - Coba multi-key: XDATA_KEY → XDATA_KEYS → file xdata.keys
    - Return "{}" pada kegagalan (kompat lama).
    """
    try:
        if not xdata:
            return "{}"
        for key in _iter_candidate_keys():
            pt = _try_decrypt_with_key(xdata, xtime_ms, key)
            if pt is not None:
                return pt
        logger.warning("Decrypt XData gagal untuk semua kandidat key.")
        return "{}"
    except Exception as e:
        logger.error("Decrypt XData Failed: %s", e)
        return "{}"


def decrypt_xdata_any(xdata: str, xtime_ms: int) -> str:
    """Alias eksplisit ke decrypt_xdata (multi-key)."""
    return decrypt_xdata(xdata, xtime_ms)


# ---------------------------------------------------------------------------
# Family/Circle AES-CBC (MSISDN)
# ---------------------------------------------------------------------------

def encrypt_circle_msisdn(msisdn: str) -> str:
    """
    Output: urlsafe_b64(ciphertext) + iv_hex(16 chars).
    """
    try:
        if not msisdn:
            return ""
        key = _key_bytes(ENCRYPTED_FIELD_KEY)
        if not key:
            return ""
        iv_hex = os.urandom(8).hex()  # 16 hex chars
        iv = iv_hex.encode("ascii")   # used as ASCII bytes (legacy)
        ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(msisdn.encode("utf-8"), _BLOCK_SIZE, style="pkcs7"))
        return urlsafe_b64encode(ct).decode("ascii") + iv_hex
    except Exception as e:
        logger.error("Encrypt Circle MSISDN Failed: %s", e)
        return ""


def decrypt_circle_msisdn(encrypted_msisdn_b64: str) -> str:
    """
    Input: urlsafe_b64(ciphertext) + iv_hex(16 chars) → plaintext.
    """
    try:
        if not encrypted_msisdn_b64 or len(encrypted_msisdn_b64) < 16:
            return ""
        key = _key_bytes(ENCRYPTED_FIELD_KEY)
        if not key:
            return ""
        iv_hex = encrypted_msisdn_b64[-16:]
        ct_b64 = encrypted_msisdn_b64[:-16]
        iv = iv_hex.encode("ascii")
        ct = urlsafe_b64decode(_fix_b64(ct_b64))
        pt_padded = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
        pt = _safe_unpad(pt_padded)
        return pt.decode("utf-8", errors="strict") if pt is not None else ""
    except Exception as e:
        logger.error("Decrypt Circle MSISDN Failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# HMAC Signatures
# ---------------------------------------------------------------------------

def make_x_signature(id_token: str, method: str, path: str, sig_time_sec: int) -> str:
    key_str = f"{X_API_BASE_SECRET};{id_token};{method};{path};{sig_time_sec}"
    msg_str = f"{id_token};{sig_time_sec};"
    return _hmac_sha512_hex(key_str, msg_str)


def make_x_signature_payment(
    access_token: str,
    sig_time_sec: int,
    package_code: str,
    token_payment: str,
    payment_method: str,
    payment_for: str,
    path: str,
) -> str:
    key_str = f"{X_API_BASE_SECRET};{sig_time_sec}#ae-hei_9Tee6he+Ik3Gais5=;POST;{path};{sig_time_sec}"
    msg_str = f"{access_token};{token_payment};{sig_time_sec};{payment_for};{payment_method};{package_code};"
    return _hmac_sha512_hex(key_str, msg_str)


def make_ax_api_signature(ts_for_sign: str, contact: str, code: str, contact_type: str) -> str:
    """
    Return base64(HMAC_SHA256(AX_API_SIG_KEY, preimage)).
    """
    try:
        if not AX_API_SIG_KEY:
            logger.error("AX_API_SIG_KEY is missing")
            return ""
        key_bytes = AX_API_SIG_KEY.encode("ascii", errors="strict")  # intentionally ascii, back-compat
        preimage = f"{ts_for_sign}password{contact_type}{contact}{code}openid"
        digest = hmac.new(key_bytes, preimage.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")
    except Exception as e:
        logger.error("AX Signature Failed: %s", e)
        return ""


def make_x_signature_bounty(access_token: str, sig_time_sec: int, package_code: str, token_payment: str) -> str:
    path = "api/v8/personalization/bounties-exchange"
    key_str = f"{X_API_BASE_SECRET};{access_token};{sig_time_sec}#ae-hei_9Tee6he+Ik3Gais5=;POST;{path};{sig_time_sec}"
    msg_str = f"{access_token};{token_payment};{sig_time_sec};{package_code};"
    return _hmac_sha512_hex(key_str, msg_str)


def make_x_signature_loyalty(sig_time_sec: int, package_code: str, token_confirmation: str, path: str) -> str:
    key_str = f"{X_API_BASE_SECRET};{sig_time_sec}#ae-hei_9Tee6he+Ik3Gais5=;POST;{path};{sig_time_sec}"
    msg_str = f"{token_confirmation};{sig_time_sec};{package_code};"
    return _hmac_sha512_hex(key_str, msg_str)


def make_x_signature_bounty_allotment(
    sig_time_sec: int,
    package_code: str,
    token_confirmation: str,
    path: str,
    destination_msisdn: str,
) -> str:
    key_str = f"{X_API_BASE_SECRET};{sig_time_sec}#ae-hei_9Tee6he+Ik3Gais5=;{destination_msisdn};POST;{path};{sig_time_sec}"
    msg_str = f"{token_confirmation};{sig_time_sec};{destination_msisdn};{package_code};"
    return _hmac_sha512_hex(key_str, msg_str)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def _valid_len(s: str) -> Optional[int]:
    kb = _key_bytes(s)
    return len(kb) if kb else None


def crypto_self_test() -> dict:
    """
    Jalankan serangkaian tes ringan agar cepat tahu setup aman.
    Tidak membocorkan nilai kunci; hanya panjang & hasil boolean.
    """
    report = {
        "xdata_key_len": _valid_len(XDATA_KEY),
        "circle_key_len": _valid_len(ENCRYPTED_FIELD_KEY),
        "ax_api_sig_key_present": bool(AX_API_SIG_KEY),
        "x_api_base_secret_present": bool(X_API_BASE_SECRET),
        "xdata_roundtrip_ok": False,
        "circle_roundtrip_ok": False,
    }

    try:
        # XDATA round-trip
        sample = '{"ping":"pong"}'
        xtime = 1700000000000  # fixed for reproducibility
        ct = encrypt_xdata(sample, xtime)
        if ct:
            pt = decrypt_xdata(ct, xtime)
            report["xdata_roundtrip_ok"] = (pt == sample)
    except Exception:
        report["xdata_roundtrip_ok"] = False

    try:
        # Circle round-trip
        msisdn = "6281234567890"
        enc = encrypt_circle_msisdn(msisdn)
        if enc:
            dec = decrypt_circle_msisdn(enc)
            report["circle_roundtrip_ok"] = (dec == msisdn)
    except Exception:
        report["circle_roundtrip_ok"] = False

    return report