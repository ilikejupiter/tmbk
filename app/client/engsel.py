# -*- coding: utf-8 -*-
"""
engsel.py - Hardened HTTP Layer (Python 3.9.18)

What’s improved vs original:
- Timeouts: connect/read timeout (tuple) with sane defaults and env overrides
- Retries: urllib3 Retry for network + 429/5xx, respects Retry-After
- No resp.raise_for_status() -> we can classify 401/429/5xx cleanly + still parse body
- Response normalization: always returns dict, includes category/http_status when error
- Still backwards compatible: same globals + wrapper functions + singleton _client

Env knobs (optional):
  BASE_API_URL
  UA
  API_KEY

  MYXL_HTTP_CONNECT_TIMEOUT   default 10
  MYXL_HTTP_READ_TIMEOUT      default 30
  MYXL_HTTP_RETRIES           default 3
  MYXL_HTTP_BACKOFF           default 0.6
  MYXL_HTTP_POOL_MAXSIZE      default 24
  MYXL_HTTP_POOL_CONNECTIONS  default 24
  MYXL_HTTP_RETRY_POST        default 1   (retry POST is on; set 0 to disable)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Mapping, Tuple, Union
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

try:
    # urllib3 v2+
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    # very old urllib3 fallback
    Retry = None  # type: ignore

# Project imports
from app.client.encrypt import (
    encryptsign_xdata,
    java_like_timestamp,
    decrypt_xdata,
    API_KEY,
)

logger = logging.getLogger(__name__)

# =============================================================================
# GLOBAL VARIABLES (Legacy Compatibility)
# =============================================================================
BASE_API_URL = os.getenv("BASE_API_URL")
UA = os.getenv("UA")

# =============================================================================
# Helpers
# =============================================================================

TimeoutType = Union[int, float, Tuple[float, float]]


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return float(str(v).strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    s = str(v).strip().lower()
    return s not in ("0", "false", "no", "off", "")


def _as_str(v: Any) -> str:
    try:
        return "" if v is None else str(v)
    except Exception:
        return ""


def _safe_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _join_url(base: str, path: str) -> str:
    base = (base or "").strip().rstrip("/")
    path = (path or "").strip().lstrip("/")
    return f"{base}/{path}"


def _classify_http(status_code: int) -> str:
    if status_code == 401:
        return "AUTH"
    if status_code == 429:
        return "RATE_LIMIT"
    if 500 <= status_code <= 599:
        return "SERVER"
    if 400 <= status_code <= 499:
        return "CLIENT"
    return "OK"


def _parse_retry_after(resp: requests.Response) -> Optional[int]:
    """
    Extract Retry-After (seconds). If date format given, skip parsing to keep stdlib-only.
    """
    ra = resp.headers.get("Retry-After")
    if not ra:
        return None
    try:
        return int(ra.strip())
    except Exception:
        return None


# =============================================================================
# Config
# =============================================================================

@dataclass
class EngselConfig:
    """Konfigurasi terpusat untuk Client Engsel"""
    base_url: str = field(default_factory=lambda: BASE_API_URL or os.getenv("BASE_API_URL", "https://api.xl.co.id"))
    api_key: str = field(default_factory=lambda: API_KEY or os.getenv("API_KEY", ""))
    user_agent: str = field(default_factory=lambda: UA or os.getenv("UA", "Mozilla/5.0"))

    # Hardened HTTP settings
    connect_timeout: float = field(default_factory=lambda: float(_env_int("MYXL_HTTP_CONNECT_TIMEOUT", 10)))
    read_timeout: float = field(default_factory=lambda: float(_env_int("MYXL_HTTP_READ_TIMEOUT", 30)))
    retries: int = field(default_factory=lambda: _env_int("MYXL_HTTP_RETRIES", 3))
    backoff_factor: float = field(default_factory=lambda: _env_float("MYXL_HTTP_BACKOFF", 0.6))
    pool_connections: int = field(default_factory=lambda: _env_int("MYXL_HTTP_POOL_CONNECTIONS", 24))
    pool_maxsize: int = field(default_factory=lambda: _env_int("MYXL_HTTP_POOL_MAXSIZE", 24))
    retry_post: bool = field(default_factory=lambda: _env_bool("MYXL_HTTP_RETRY_POST", True))

    app_version: str = "8.9.1"  # centralized versioning

    def __post_init__(self):
        if not self.base_url:
            self.base_url = "https://api.xl.co.id"
        self.base_url = self.base_url.strip().rstrip("/")
        # defensive lower bounds
        if self.connect_timeout <= 0:
            self.connect_timeout = 10.0
        if self.read_timeout <= 0:
            self.read_timeout = 30.0
        if self.retries < 0:
            self.retries = 0
        if self.pool_connections < 1:
            self.pool_connections = 8
        if self.pool_maxsize < 1:
            self.pool_maxsize = 8


# =============================================================================
# Client
# =============================================================================

class EngselClient:
    """
    Hardened client for XL API:
    - requests.Session + urllib3 Retry
    - explicit error classification (401/429/5xx)
    - stable output dict
    """

    def __init__(self, config: Optional[EngselConfig] = None):
        self.config = config or EngselConfig()
        self._session = self._init_session()

    def _init_session(self) -> requests.Session:
        session = requests.Session()

        if Retry is None or self.config.retries == 0:
            adapter = HTTPAdapter(
                pool_connections=self.config.pool_connections,
                pool_maxsize=self.config.pool_maxsize,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            return session

        # Decide allowed methods (retrying POST is on by default for API stability)
        allowed = {"GET"}
        if self.config.retry_post:
            allowed.add("POST")

        # urllib3 Retry API compatibility (allowed_methods vs method_whitelist)
        retry_kwargs = dict(
            total=self.config.retries,
            connect=self.config.retries,
            read=self.config.retries,
            status=self.config.retries,
            backoff_factor=self.config.backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        try:
            retries = Retry(allowed_methods=allowed, **retry_kwargs)  # type: ignore[arg-type]
        except TypeError:
            retries = Retry(method_whitelist=allowed, **retry_kwargs)  # type: ignore[call-arg]

        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=self.config.pool_connections,
            pool_maxsize=self.config.pool_maxsize,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _get_clean_host(self) -> str:
        try:
            parsed = urlparse(self.config.base_url)
            if parsed.netloc:
                return parsed.netloc
            # fallback
            return self.config.base_url.replace("https://", "").replace("http://", "").split("/")[0]
        except Exception:
            return "api.xl.co.id"

    def _make_timeout(self, timeout: Optional[int]) -> Tuple[float, float]:
        """
        Per-request timeout override:
        - If timeout provided -> use it as READ timeout, connect stays config.connect_timeout capped by timeout.
        """
        if timeout is None:
            return (float(self.config.connect_timeout), float(self.config.read_timeout))
        try:
            t = float(timeout)
        except Exception:
            return (float(self.config.connect_timeout), float(self.config.read_timeout))
        if t <= 0:
            return (float(self.config.connect_timeout), float(self.config.read_timeout))
        return (min(float(self.config.connect_timeout), t), t)

    def _decrypt_if_possible(self, response_json: Dict[str, Any]) -> Dict[str, Any]:
        """
        Only try decrypt when payload looks like encrypted xdata.
        """
        if not isinstance(response_json, dict):
            return {}
        if "xdata" not in response_json or "xtime" not in response_json:
            return {}
        try:
            dec = decrypt_xdata(self.config.api_key, response_json)
            return dec if isinstance(dec, dict) else {}
        except Exception:
            return {}

    def _send_request(
        self,
        path: str,
        payload: Dict[str, Any],
        id_token: str,
        method: str = "POST",
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Core function:
        1) Encrypt payload + signature
        2) HTTP request with retry/timeout
        3) Parse JSON (if possible), attempt decrypt (if xdata present)
        4) Classify 401/429/5xx + return stable dict
        """
        method_u = (method or "POST").upper().strip()

        if not self.config.api_key:
            return {"status": "ERROR", "category": "CONFIG", "message": "Missing API Key"}

        # 1) Encrypt + signature
        try:
            encrypted_data = encryptsign_xdata(
                api_key=self.config.api_key,
                method=method_u,
                path=path,
                id_token=id_token,
                payload=payload,
            )
            if not encrypted_data or "encrypted_body" not in encrypted_data:
                raise ValueError("Encryption returned empty/invalid data")
        except Exception as e:
            logger.error("Encryption failed for %s: %s", path, e)
            return {"status": "ERROR", "category": "CRYPTO", "message": f"Encryption failed: {e}"}

        # Prepare headers
        xtime = int(encrypted_data["encrypted_body"]["xtime"])
        sig_time_sec = str(xtime // 1000)
        now = datetime.now(timezone.utc).astimezone()

        headers: Dict[str, str] = {
            "host": self._get_clean_host(),
            "content-type": "application/json; charset=utf-8",
            "user-agent": self.config.user_agent,
            "x-api-key": self.config.api_key,
            "x-hv": "v3",
            "x-signature-time": sig_time_sec,
            "x-signature": _as_str(encrypted_data.get("x_signature")),
            "x-request-id": str(uuid.uuid4()),
            "x-request-at": java_like_timestamp(now),
            "x-version-app": self.config.app_version,
        }

        # Keep legacy header format: Authorization Bearer id_token
        # (Even if id_token empty, we simply omit to avoid sending "Bearer ")
        idt = _as_str(id_token).strip()
        if idt:
            headers["authorization"] = f"Bearer {idt}"

        url = _join_url(self.config.base_url, path)
        body = encrypted_data["encrypted_body"]
        req_timeout = self._make_timeout(timeout)

        # 2) HTTP request
        try:
            if method_u == "POST":
                resp = self._session.post(url, headers=headers, json=body, timeout=req_timeout)
            else:
                resp = self._session.get(url, headers=headers, timeout=req_timeout)
        except requests.exceptions.Timeout:
            logger.error("Timeout %s %s", method_u, path)
            return {"status": "ERROR", "category": "TIMEOUT", "message": "Request timed out"}
        except requests.RequestException as e:
            logger.error("Network error %s %s: %s", method_u, path, e)
            return {"status": "ERROR", "category": "NETWORK", "message": f"Network error: {e}"}
        except Exception as e:
            logger.error("Unexpected error %s %s: %s", method_u, path, e)
            return {"status": "ERROR", "category": "SYSTEM", "message": f"System error: {e}"}

        http_status = int(getattr(resp, "status_code", 0) or 0)
        category = _classify_http(http_status)

        # 3) Parse JSON (best-effort)
        response_json: Dict[str, Any] = {}
        try:
            response_json = resp.json()
            if not isinstance(response_json, dict):
                response_json = {"raw": response_json}
        except json.JSONDecodeError:
            # server might return HTML (nginx) on failures
            response_json = {"raw": (resp.text or "")[:500]}
        except Exception:
            response_json = {"raw": (resp.text or "")[:500]}

        # Attempt decrypt if it’s xdata-shaped
        decrypted = self._decrypt_if_possible(response_json)
        final_payload = decrypted if decrypted else response_json

        # 4) Classification + stable return
        if http_status >= 400:
            # Attach useful fields without breaking old code
            out = _safe_dict(final_payload)
            out.setdefault("status", "ERROR")
            out["http_status"] = http_status
            out["category"] = category

            if category == "RATE_LIMIT":
                out["retry_after"] = _parse_retry_after(resp)
                out.setdefault("message", "Rate limited (HTTP 429)")
            elif category == "AUTH":
                out.setdefault("message", "Unauthorized (HTTP 401) - token may be expired/invalid")
            elif category == "SERVER":
                out.setdefault("message", f"Server error (HTTP {http_status})")
            else:
                out.setdefault("message", f"HTTP error (HTTP {http_status})")

            return out

        # Success path: prefer decrypted dict if available
        if isinstance(final_payload, dict) and final_payload:
            return final_payload

        # If everything empty, give a minimal success-ish payload
        return {"status": "SUCCESS", "http_status": http_status, "data": final_payload}

    # =========================================================================
    # BUSINESS LOGIC METHODS (kept)
    # =========================================================================

    def get_balance(self, id_token: str) -> Optional[Any]:
        logger.info("Fetching balance...")
        res = self._send_request(
            "api/v8/packages/balance-and-credit",
            {"is_enterprise": False, "lang": "en"},
            id_token,
            "POST",
        )
        return _safe_dict(res).get("data", {}).get("balance")

    def get_family(
        self,
        tokens: Dict,
        family_code: str,
        is_enterprise: Optional[bool] = None,
        migration_type: Optional[str] = None,
    ) -> Optional[Dict]:
        if not family_code:
            return None
        id_token = tokens.get("id_token")
        if not id_token:
            return None

        ent_opts = [is_enterprise] if is_enterprise is not None else [False, True]
        mig_opts = [migration_type] if migration_type is not None else ["NONE", "PRE_TO_PRIOH", "PRIOH_TO_PRIO", "PRIO_TO_PRIOH"]

        for mt in mig_opts:
            for ie in ent_opts:
                payload = {
                    "is_show_tagging_tab": True,
                    "is_dedicated_event": True,
                    "is_transaction_routine": False,
                    "migration_type": mt,
                    "package_family_code": family_code,
                    "is_autobuy": False,
                    "is_enterprise": ie,
                    "is_pdlp": True,
                    "referral_code": "",
                    "is_migration": False,
                    "lang": "en",
                }

                res = self._send_request("api/v8/xl-stores/options/list", payload, id_token, "POST")
                if isinstance(res, dict) and res.get("status") == "SUCCESS" and "data" in res:
                    pf = _safe_dict(res["data"]).get("package_family", {})
                    if isinstance(pf, dict) and pf.get("name"):
                        logger.info("Family found: %s (Ent:%s, Mig:%s)", pf["name"], ie, mt)
                        return res.get("data")
        return None

    def get_package_detail(self, tokens: Dict, option_code: str, family_code: str = "", variant_code: str = "") -> Optional[Dict]:
        if not option_code:
            return None
        payload = {
            "is_transaction_routine": False,
            "migration_type": "NONE",
            "package_family_code": family_code,
            "family_role_hub": "",
            "is_autobuy": False,
            "is_enterprise": False,
            "is_shareable": False,
            "is_migration": False,
            "lang": "en",
            "package_option_code": option_code,
            "is_upsell_pdp": False,
            "package_variant_code": variant_code,
        }
        res = self._send_request("api/v8/xl-stores/options/detail", payload, tokens.get("id_token", ""), "POST")
        return _safe_dict(res).get("data") if isinstance(res, dict) else None

    def get_addons(self, tokens: Dict, option_code: str) -> Dict:
        if not option_code:
            return {}
        payload = {"is_enterprise": False, "lang": "en", "package_option_code": option_code}
        res = self._send_request("api/v8/xl-stores/options/addons-pinky-box", payload, tokens.get("id_token", ""), "POST")
        return _safe_dict(res).get("data", {}) if isinstance(res, dict) else {}

    def intercept_page(self, tokens: Dict, option_code: str, is_enterprise: bool = False) -> Dict:
        payload = {"is_enterprise": is_enterprise, "lang": "en", "package_option_code": option_code}
        return self._send_request("misc/api/v8/utility/intercept-page", payload, tokens.get("id_token", ""), "POST") or {}

    def login_info(self, tokens: Dict, is_enterprise: bool = False) -> Optional[Dict]:
        payload = {"access_token": tokens.get("access_token", ""), "is_enterprise": is_enterprise, "lang": "en"}
        res = self._send_request("api/v8/auth/login", payload, tokens.get("id_token", ""), "POST")
        return _safe_dict(res).get("data") if isinstance(res, dict) else None

    def get_package_by_order(self, tokens: Dict, family_code: str, variant_code: str, order: int) -> Optional[Dict]:
        family_data = self.get_family(tokens, family_code)
        if not family_data:
            return None

        option_code = None
        for variant in family_data.get("package_variants", []):
            if isinstance(variant, dict) and variant.get("package_variant_code") == variant_code:
                for option in variant.get("package_options", []):
                    if isinstance(option, dict) and option.get("order") == order:
                        option_code = option.get("package_option_code")
                        break
                break

        if option_code:
            return self.get_package_detail(tokens, option_code, family_code, variant_code)
        return None

    def get_notifications(self, tokens: Dict) -> Optional[Dict]:
        return self._send_request("api/v8/notification-non-grouping", {"is_enterprise": False, "lang": "en"}, tokens.get("id_token", ""), "POST")

    def get_notification_detail(self, tokens: Dict, notif_id: str) -> Optional[Dict]:
        return self._send_request("api/v8/notification/detail", {"is_enterprise": False, "lang": "en", "notification_id": notif_id}, tokens.get("id_token", ""), "POST")

    def get_pending_transaction(self, tokens: Dict) -> Dict:
        res = self._send_request("api/v8/profile", {"is_enterprise": False, "lang": "en"}, tokens.get("id_token", ""), "POST")
        return _safe_dict(res).get("data", {}) if isinstance(res, dict) else {}

    def get_transaction_history(self, tokens: Dict) -> Dict:
        res = self._send_request("payments/api/v8/transaction-history", {"is_enterprise": False, "lang": "en"}, tokens.get("id_token", ""), "POST")
        return _safe_dict(res).get("data", {"list": []}) if isinstance(res, dict) else {"list": []}

    def get_tiering_info(self, tokens: Dict) -> Dict:
        res = self._send_request("gamification/api/v8/loyalties/tiering/info", {"is_enterprise": False, "lang": "en"}, tokens.get("id_token", ""), "POST")
        return _safe_dict(res).get("data", {}) if isinstance(res, dict) else {}

    def unsubscribe(self, tokens: Dict, quota_code: str, domain: str, subtype: str) -> bool:
        payload = {
            "product_subscription_type": subtype,
            "quota_code": quota_code,
            "product_domain": domain,
            "is_enterprise": False,
            "unsubscribe_reason_code": "",
            "lang": "en",
            "family_member_id": "",
        }
        res = self._send_request("api/v8/packages/unsubscribe", payload, tokens.get("id_token", ""), "POST")
        return isinstance(res, dict) and res.get("code") == "000"

    def dashboard_segments(self, tokens: Dict) -> Dict:
        return self._send_request("dashboard/api/v8/segments", {"access_token": tokens.get("access_token", "")}, tokens.get("id_token", ""), "POST") or {}

    def get_profile(self, access_token: str, id_token: str) -> Dict:
        payload = {"access_token": access_token, "app_version": self.config.app_version, "is_enterprise": False, "lang": "en"}
        res = self._send_request("api/v8/profile", payload, id_token, "POST")
        return _safe_dict(res).get("data", {}) if isinstance(res, dict) else {}

    def get_families_by_category(self, tokens: Dict, category_code: str) -> Optional[Dict]:
        payload = {
            "migration_type": "",
            "is_enterprise": False,
            "is_shareable": False,
            "package_category_code": category_code,
            "with_icon_url": True,
            "is_migration": False,
            "lang": "en",
        }
        res = self._send_request("api/v8/xl-stores/families", payload, tokens.get("id_token", ""), "POST")
        return _safe_dict(res).get("data") if isinstance(res, dict) and res.get("status") == "SUCCESS" else None

    def validate_puk(self, tokens: Dict, msisdn: str, puk: str) -> Dict:
        payload = {"is_enterprise": False, "puk": puk, "is_enc": False, "msisdn": msisdn, "lang": "en"}
        return self._send_request("api/v8/infos/validate-puk", payload, tokens.get("id_token", ""), "POST") or {}

    def get_quota_details(self, tokens: Dict) -> Dict:
        res = self._send_request("api/v8/packages/quota-details", {"is_enterprise": False, "lang": "en", "family_member_id": ""}, tokens.get("id_token", ""), "POST")
        return _safe_dict(res).get("data", {"quotas": []}) if isinstance(res, dict) else {"quotas": []}


# =============================================================================
# COMPATIBILITY LAYER (Backward Compatibility)
# =============================================================================

_client = EngselClient()


def _ensure_api_key(api_key: str) -> None:
    """Update API Key di singleton jika berbeda."""
    if api_key and api_key != _client.config.api_key:
        _client.config.api_key = api_key


def send_api_request(api_key: str, path: str, payload_dict: dict, id_token: str, method: str = "POST", timeout: int = 30):
    _ensure_api_key(api_key)
    return _client._send_request(path, payload_dict, id_token, method, timeout=timeout)


def get_balance(api_key: str, id_token: str):
    _ensure_api_key(api_key)
    return _client.get_balance(id_token)


def get_family(api_key: str, tokens: dict, family_code: str, is_enterprise: Optional[bool] = None, migration_type: Optional[str] = None):
    _ensure_api_key(api_key)
    return _client.get_family(tokens, family_code, is_enterprise, migration_type)


def get_package(api_key: str, tokens: dict, package_option_code: str, package_family_code: str = "", package_variant_code: str = ""):
    _ensure_api_key(api_key)
    return _client.get_package_detail(tokens, package_option_code, package_family_code, package_variant_code)


def get_addons(api_key: str, tokens: dict, package_option_code: str):
    _ensure_api_key(api_key)
    return _client.get_addons(tokens, package_option_code)


def intercept_page(api_key: str, tokens: dict, option_code: str, is_enterprise: bool = False):
    _ensure_api_key(api_key)
    return _client.intercept_page(tokens, option_code, is_enterprise)


def login_info(api_key: str, tokens: dict, is_enterprise: bool = False):
    _ensure_api_key(api_key)
    return _client.login_info(tokens, is_enterprise)


def get_package_details(api_key: str, tokens: dict, family_code: str, variant_code: str, option_order: int, is_enterprise: Optional[bool] = None, migration_type: Optional[str] = None):
    _ensure_api_key(api_key)
    return _client.get_package_by_order(tokens, family_code, variant_code, option_order)


def get_notifications(api_key: str, tokens: dict):
    _ensure_api_key(api_key)
    return _client.get_notifications(tokens)


def get_notification_detail(api_key: str, tokens: dict, notification_id: str):
    _ensure_api_key(api_key)
    return _client.get_notification_detail(tokens, notification_id)


def get_pending_transaction(api_key: str, tokens: dict):
    _ensure_api_key(api_key)
    return _client.get_pending_transaction(tokens)


def get_transaction_history(api_key: str, tokens: dict):
    _ensure_api_key(api_key)
    return _client.get_transaction_history(tokens)


def get_tiering_info(api_key: str, tokens: dict):
    _ensure_api_key(api_key)
    return _client.get_tiering_info(tokens)


def unsubscribe(api_key: str, tokens: dict, quota_code: str, product_domain: str, product_subscription_type: str):
    _ensure_api_key(api_key)
    return _client.unsubscribe(tokens, quota_code, product_domain, product_subscription_type)


def dashboard_segments(api_key: str, tokens: dict):
    _ensure_api_key(api_key)
    return _client.dashboard_segments(tokens)


def get_profile(api_key: str, access_token: str, id_token: str):
    _ensure_api_key(api_key)
    return _client.get_profile(access_token, id_token)


def get_families(api_key: str, tokens: dict, package_category_code: str):
    _ensure_api_key(api_key)
    return _client.get_families_by_category(tokens, package_category_code)


def validate_puk(api_key: str, tokens: dict, msisdn: str, puk: str):
    _ensure_api_key(api_key)
    return _client.validate_puk(tokens, msisdn, puk)


def get_quota_details(api_key: str, tokens: dict):
    _ensure_api_key(api_key)
    return _client.get_quota_details(tokens)


# Extra utilities (kept)
def check_service_availability(api_key: str, tokens: dict) -> bool:
    _ensure_api_key(api_key)
    balance = _client.get_balance(tokens.get("id_token", ""))
    return balance is not None


def get_api_status(api_key: str, tokens: dict) -> dict:
    _ensure_api_key(api_key)
    status = {"auth": False, "balance": False, "packages": False, "timestamp": datetime.now().isoformat()}
    try:
        if "access_token" in tokens and "id_token" in tokens:
            prof = _client.get_profile(tokens["access_token"], tokens["id_token"])
            status["auth"] = bool(prof and prof.get("profile"))

            bal = _client.get_balance(tokens["id_token"])
            status["balance"] = bal is not None

            quota = _client.get_quota_details(tokens)
            status["packages"] = bool(quota and "quotas" in quota)
    except Exception as e:
        status["error"] = str(e)
    return status