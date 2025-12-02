# -*- coding: utf-8 -*-
"""
CIAM Client (XL) - Modern & Stable
Kompatibel Python 3.9.18

Fokus perbaikan:
- Konfigurasi terpusat via dataclass CiamConfig
- Session requests dengan Retry dan timeout
- Header dinamis & statis terstruktur
- Parsing respons konsisten (selalu inject _status_code)
- Validasi input & error handling yang ramah
- Backward compatibility layer (fungsi global tetap tersedia)

Catatan:
- Mengandalkan modul `app.client.encrypt` untuk:
  - java_like_timestamp, ts_gmt7_without_colon
  - ax_api_signature
  - load_ax_fp, ax_device_id
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Pastikan path import ini sesuai struktur proyekmu
from app.client.encrypt import (
    java_like_timestamp,
    ts_gmt7_without_colon,
    ax_api_signature,
    load_ax_fp,
    ax_device_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CiamConfig",
    "CiamClient",
    "get_new_token",
    "get_otp",
    "submit_otp",
    "extend_session",
    "get_auth_code",
    "validate_contact",
]


@dataclass
class CiamConfig:
    """Konfigurasi terpusat untuk CIAM Client."""
    base_url: str = field(default_factory=lambda: os.getenv("BASE_CIAM_URL", "https://api.xl.co.id"))
    basic_auth: str = field(default_factory=lambda: os.getenv("BASIC_AUTH", ""))
    user_agent: str = field(default_factory=lambda: os.getenv("UA", "Mozilla/5.0"))
    device_id: str = field(default_factory=ax_device_id)
    fingerprint: str = field(default_factory=load_ax_fp)
    timeout: int = 30  # detik (read timeout). Connect timeout default 10s (lihat _init_session)

    def cleaned_base(self) -> str:
        # Hilangkan trailing slash agar join endpoint konsisten
        return self.base_url[:-1] if self.base_url.endswith("/") else self.base_url


class CiamClient:
    """
    Klien modern untuk berinteraksi dengan layanan CIAM.
    Aman dipakai di CLI/daemon/CI (timeout, retry, logging).
    """

    def __init__(self, config: Optional[CiamConfig] = None):
        self.config = config or CiamConfig()
        if not self.config.basic_auth:
            logger.warning("BASIC_AUTH environment variable is not set!")

        self._session = self._init_session()
        self._static_headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "Authorization": f"Basic {self.config.basic_auth}",
            "Ax-Device-Id": self.config.device_id,
            "Ax-Fingerprint": self.config.fingerprint,
            "Ax-Request-Device": "samsung",          # Tetap konsisten utk menghindari flag fraud
            "Ax-Request-Device-Model": "SM-N935F",
            "Ax-Substype": "PREPAID",
            "User-Agent": self.config.user_agent,
        }

    # ------------------------------------------------------------------ session

    def _init_session(self) -> requests.Session:
        session = requests.Session()
        # Retry konservatif untuk 5xx & koneksi; sertakan POST (aman untuk endpoint idempotent server)
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=frozenset({"HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        # PROXIES otomatis mengikuti env (requests default)
        return session

    # ------------------------------------------------------------------ helpers

    def _get_gmt7_now(self) -> datetime:
        return datetime.now(timezone(timedelta(hours=7)))

    def _get_dynamic_headers(self) -> Dict[str, str]:
        now = self._get_gmt7_now()
        host = self.config.cleaned_base().replace("https://", "").replace("http://", "")
        if "/" in host:
            host = host.split("/")[0]
        return {
            "Ax-Request-At": java_like_timestamp(now),
            "Ax-Request-Id": str(uuid.uuid4()),
            "Host": host,
        }

    def _build_url(self, endpoint: str) -> str:
        base = self.config.cleaned_base()
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        return f"{base}{endpoint}"

    def _merge_headers(self, overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        final_headers = dict(self._static_headers)
        final_headers.update(self._get_dynamic_headers())
        if overrides:
            final_headers.update(overrides)
        return final_headers

    def _parse_json(self, response: requests.Response) -> Optional[Dict[str, Any]]:
        try:
            data = response.json()
        except ValueError:
            snippet = response.text[:200] if response.text else ""
            logger.error("Response invalid JSON (status %s): %s", response.status_code, snippet)
            return None

        if isinstance(data, dict):
            data.setdefault("_status_code", response.status_code)
        return data

    def _make_request(
        self,
        method: str,
        endpoint: str,
        headers: Optional[Dict[str, str]] = None,
        return_full_response: bool = False,
        **kwargs: Any
    ) -> Union[Optional[Dict[str, Any]], requests.Response]:
        """
        Wrapper request:
        - Mengatur URL, header, timeout (connect=10s, read=config.timeout)
        - Konsisten mengembalikan dict + `_status_code` saat return_full_response=False
        """
        url = self._build_url(endpoint)
        final_headers = self._merge_headers(headers)

        # Timeout (connect, read)
        if "timeout" not in kwargs:
            kwargs["timeout"] = (10, self.config.timeout)

        try:
            resp = self._session.request(method=method, url=url, headers=final_headers, **kwargs)

            # Log 5xx untuk visibilitas, tapi tetap parse dan kembalikan JSON agar caller bisa ambil pesan server
            if resp.status_code >= 500:
                logger.error("Server Error %s pada %s: %s", resp.status_code, endpoint, (resp.text or "")[:200])

            if return_full_response:
                return resp

            return self._parse_json(resp)

        except requests.Timeout:
            logger.error("Timeout request ke %s", endpoint)
            return None
        except requests.RequestException as e:
            logger.error("Request error %s: %s", endpoint, e)
            return None
        except Exception as e:
            logger.exception("Unhandled error saat request %s: %s", endpoint, e)
            return None

    # ------------------------------------------------------------------ public

    @staticmethod
    def validate_contact(contact: str) -> bool:
        """
        Validasi MSISDN: harus mulai 628, panjang 10-14, seluruhnya digit.
        """
        if not contact or not contact.startswith("628"):
            return False
        if not (10 <= len(contact) <= 14):
            return False
        return contact.isdigit()

    def request_otp(self, contact: str) -> Optional[str]:
        """
        Meminta OTP via SMS untuk `contact`. Mengembalikan `subscriber_id` jika sukses.
        """
        if not self.validate_contact(contact):
            logger.error("Invalid contact format: %s", contact)
            return None

        resp = self._make_request(
            "GET",
            "/realms/xl-ciam/auth/otp",
            params={"contact": contact, "contactType": "SMS", "alternateContact": "false"},
            headers={"Content-Type": "application/json"},
        )
        if resp and "subscriber_id" in resp:
            return resp["subscriber_id"]
        logger.error("Failed requesting OTP: %s", resp)
        return None

    def extend_session(self, subscriber_id: str) -> Optional[str]:
        """
        Meminta exchange code berbasis DEVICEID (perpanjang sesi).
        """
        if not subscriber_id:
            return None

        try:
            b64_id = base64.b64encode(subscriber_id.encode("utf-8")).decode("ascii")
        except Exception:
            return None

        resp = self._make_request(
            "GET",
            "/realms/xl-ciam/auth/extend-session",
            params={"contact": b64_id, "contactType": "DEVICEID"},
            headers={"Content-Type": "application/json"},
        )
        if resp and resp.get("_status_code") == 200:
            return (resp.get("data") or {}).get("exchange_code")
        logger.warning("Failed extend session: %s", resp)
        return None

    def submit_otp(self, api_key: str, contact_type: str, contact: str, code: str) -> Optional[Dict[str, Any]]:
        """
        Submit OTP untuk login CIAM.
        - `contact_type` = "SMS" atau "DEVICEID"
        - Payload dikirim sebagai string form-urlencoded manual agar tidak di-URL-encode ulang oleh requests
        - Penandatanganan mengikuti pola server (Ax-Api-Signature)
        """
        # Validasi minimal
        if contact_type == "SMS" and not self.validate_contact(contact):
            logger.error("Invalid number for SMS login: %s", contact)
            return None

        # Final contact: DEVICEID → base64
        final_contact = base64.b64encode(contact.encode("utf-8")).decode("ascii") if contact_type == "DEVICEID" else contact

        now = self._get_gmt7_now()
        ts_sign = ts_gmt7_without_colon(now)
        ts_head = ts_gmt7_without_colon(now - timedelta(minutes=5))

        sig = ax_api_signature(api_key, ts_sign, final_contact, code, contact_type)
        if not sig:
            logger.error("Failed to create Ax-Api-Signature")
            return None

        payload_str = (
            f"contactType={contact_type}&code={code}"
            f"&grant_type=password&contact={final_contact}&scope=openid"
        )

        headers = {
            "Ax-Api-Signature": sig,
            "Ax-Request-At": ts_head,  # Override jam header sesuai perilaku server
            "Content-Type": "application/x-www-form-urlencoded",
        }

        logger.info("Submitting OTP...")
        resp = self._make_request(
            "POST",
            "/realms/xl-ciam/protocol/openid-connect/token",
            data=payload_str,
            headers=headers,
        )
        if resp and "error" not in resp:
            logger.info("Login successful.")
            return resp

        logger.error("Login failed: %s", resp)
        return None

    def refresh_token(self, api_key: str, refresh_token: str, subscriber_id: str) -> Optional[Dict[str, Any]]:
        """
        Refresh token utama. Jika sesi tidak aktif, otomatis mencoba extend-session + submit_otp DEVICEID.
        """
        response = self._make_request(
            "POST",
            "/realms/xl-ciam/protocol/openid-connect/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            return_full_response=True,
        )
        if response is None:
            return None

        if response.status_code == 200:
            return self._parse_json(response)

        if response.status_code == 400:
            # Coba recovery khusus "Session not active"
            try:
                resp_json = response.json()
            except Exception:
                resp_json = {}

            if resp_json.get("error_description") == "Session not active":
                logger.warning("Session expired, attempting auto-extension...")
                if not subscriber_id:
                    logger.error("Subscriber ID is missing for session extension")
                    return None

                exch_code = self.extend_session(subscriber_id)
                if not exch_code:
                    logger.error("Failed to get exchange code.")
                    return None

                extend_result = self.submit_otp(api_key, "DEVICEID", subscriber_id, exch_code)
                if extend_result:
                    return extend_result

                # Pesan spesifik dari teks error
                if "Invalid refresh token" in (response.text or ""):
                    logger.error("Refresh token is invalid or expired. Please login again.")
                return None

            logger.error("Failed to refresh token: %s", response.text)
            return None

        # Status lain: kembalikan None; caller akan memutuskan langkah selanjutnya
        return None

    def get_auth_code(self, tokens: Union[Dict[str, Any], str], pin: str, msisdn: str) -> Optional[str]:
        """
        Mendapatkan authorization_code untuk transaksi tertentu.
        `tokens` bisa dict (berisi access_token) atau string (access_token langsung).
        """
        try:
            access_token = tokens.get("access_token") if isinstance(tokens, dict) else tokens
            pin_b64 = base64.b64encode(pin.encode("utf-8")).decode("ascii")
        except Exception:
            return None

        resp = self._make_request(
            "POST",
            "/ciam/auth/authorization-token/generate",
            json={"pin": pin_b64, "transaction_type": "SHARE_BALANCE", "receiver_msisdn": msisdn},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        if resp and resp.get("status") in ("Success", "SUCCESS"):
            return (resp.get("data") or {}).get("authorization_code")
        logger.error("Failed getting auth code: %s", resp)
        return None


# =============================================================================
# COMPATIBILITY LAYER (Backward Compatibility)
# =============================================================================

_global_client = CiamClient()

def get_new_token(api_key: str, refresh_token: str, subscriber_id: str) -> Optional[dict]:
    return _global_client.refresh_token(api_key, refresh_token, subscriber_id)

def get_otp(contact: str) -> Optional[str]:
    return _global_client.request_otp(contact)

def submit_otp(api_key: str, contact_type: str, contact: str, code: str):
    return _global_client.submit_otp(api_key, contact_type, contact, code)

def extend_session(subscriber_id: str) -> Optional[str]:
    return _global_client.extend_session(subscriber_id)

def get_auth_code(tokens: dict, pin: str, msisdn: str):
    return _global_client.get_auth_code(tokens, pin, msisdn)

def validate_contact(contact: str) -> bool:
    return _global_client.validate_contact(contact)