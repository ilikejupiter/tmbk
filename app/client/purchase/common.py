import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Union

# Import Core Client
from app.client.engsel import send_api_request

# Setup Logger
logger = logging.getLogger(__name__)

# Type definitions
TokenDict = Dict[str, str]
ApiResponse = Dict[str, Any]

# =============================================================================
# UTILITY FUNCTIONS
# Helper tingkat rendah untuk manipulasi URL, Header, dan Input User.
# =============================================================================

def sanitize_base_url(base: Optional[str]) -> str:
    """
    Membersihkan URL agar aman digunakan di Header Host.
    Menghapus protokol (http/https) dan trailing slash.
    """
    if not base:
        return ""
    return base.replace("https://", "").replace("http://", "").rstrip("/")

def java_like_timestamp(dt: datetime) -> str:
    """
    Konversi datetime ke format timestamp milidetik (String).
    Berguna untuk payload tertentu yang butuh format epoch string.
    """
    millis = int(dt.timestamp() * 1000)
    return str(millis)

def build_headers(
    base_api_url: Optional[str],
    ua: str,
    api_key: str,
    id_token: str,
    x_hv: str,
    sig_time_sec: int,
    x_sig: str,
    x_requested_at: str, # Expecting formatted string from encrypt.py usually
    version_app: str = "8.9.0",
) -> Dict[str, str]:
    """
    Manual header builder. 
    Note: Biasanya 'send_api_request' di engsel.py sudah menangani ini otomatis.
    Gunakan ini hanya jika Anda melakukan raw request via requests lib langsung.
    """
    host = sanitize_base_url(base_api_url)
    return {
        "host": host,
        "content-type": "application/json; charset=utf-8",
        "user-agent": ua,
        "x-api-key": api_key,
        "authorization": f"Bearer {id_token}",
        "x-hv": x_hv,
        "x-signature-time": str(sig_time_sec),
        "x-signature": x_sig,
        "x-request-id": str(uuid.uuid4()),
        "x-request-at": x_requested_at,
        "x-version-app": version_app,
    }

def standardize_response(decrypted_body: Any) -> Dict[str, Any]:
    """
    Menormalisasi response API menjadi format dictionary yang konsisten:
    {"status": "...", "data": ..., "message": "..."}
    """
    if decrypted_body is None:
        return {"status": "ERROR", "data": None, "message": "No response / Decryption failed"}
    
    # Jika response sudah berupa dict dan punya key status
    if isinstance(decrypted_body, dict):
        status = decrypted_body.get("status", "SUCCESS" if "data" in decrypted_body else "ERROR")
        data = decrypted_body.get("data", decrypted_body if status == "SUCCESS" else None)
        
        # Ambil pesan error dari berbagai kemungkinan key
        msg = (decrypted_body.get("message") or 
               decrypted_body.get("error_msg") or 
               decrypted_body.get("error") or "")
               
        return {"status": status, "data": data, "message": msg}

    # Fallback untuk tipe data lain (list/str)
    return {"status": "SUCCESS", "data": decrypted_body, "message": ""}

def prompt_overwrite(default_amount: int, ask_overwrite: bool, interactive: bool = False) -> int:
    """
    Helper interaktif untuk mengubah nominal (misal: nominal pulsa/pembayaran).
    """
    if not ask_overwrite:
        return default_amount
    
    if not interactive:
        # Jika mode non-interaktif (otomatis), jangan memblokir proses
        return default_amount

    try:
        print(f"\n[?] Current amount is: {default_amount}")
        print("    Enter new amount to overwrite, or press Enter to keep default.")
        amount_str = input("    > ")
        
        if amount_str.strip() == "":
            return default_amount
        
        new_amount = int(amount_str)
        logger.info(f"User overwrote amount to: {new_amount}")
        return new_amount
    except ValueError:
        logger.warning("Invalid input. Using default amount.")
        return default_amount
    except Exception:
        return default_amount


# =============================================================================
# BUSINESS LOGIC
# Class Client untuk menangani fitur umum seperti Payment Methods.
# =============================================================================

class CommonClient:
    """
    Client untuk fitur-fitur umum (Common Features).
    """

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _send_request(
        self, 
        path: str, 
        payload: Dict[str, Any], 
        id_token: str, 
        description: str = ""
    ) -> ApiResponse:
        """Internal wrapper dengan logging & error handling."""
        final_payload = {
            "lang": "en",
            "is_enterprise": False,
            **payload
        }

        if description:
            logger.info(description)

        try:
            return send_api_request(
                self.api_key, 
                path, 
                final_payload, 
                id_token, 
                "POST"
            )
        except Exception as e:
            logger.error(f"Error executing {path}: {e}")
            return {"status": "Failed", "message": str(e), "data": None}

    def get_payment_methods(
        self,
        tokens: TokenDict,
        token_confirmation: str,
        payment_target: str = "PURCHASE" # Default purchase, bisa diganti misal 'BILL_PAYMENT'
    ) -> Optional[ApiResponse]:
        """
        Mengambil daftar metode pembayaran yang tersedia (Gopay, OVO, Pulsa, dll).
        """
        if not tokens.get("id_token"):
            logger.error("Missing ID Token for fetching payment methods.")
            return None

        # Payload preparation
        payload = {
            "payment_type": "PURCHASE",
            "payment_target": payment_target,
            "is_referral": False,
            "token_confirmation": token_confirmation
        }

        response = self._send_request(
            path="payments/api/v8/payment-methods-option",
            payload=payload,
            id_token=tokens["id_token"],
            description="💳 Fetching payment methods..."
        )

        # Standardize & Validate
        normalized = standardize_response(response)
        
        if normalized["status"] == "SUCCESS" and normalized["data"]:
            count = len(normalized["data"]) if isinstance(normalized["data"], list) else 0
            logger.info(f"✅ Found {count} payment options.")
            return normalized["data"]
        
        logger.error(f"❌ Failed to fetch payment methods. Message: {normalized['message']}")
        return None


# =============================================================================
# COMPATIBILITY LAYER (Legacy Support)
# =============================================================================

def get_payment_methods(
    api_key: str,
    tokens: dict,
    token_confirmation: str,
    payment_target: str,
) -> Optional[Any]:
    """Legacy wrapper for get_payment_methods"""
    return CommonClient(api_key).get_payment_methods(tokens, token_confirmation, payment_target)