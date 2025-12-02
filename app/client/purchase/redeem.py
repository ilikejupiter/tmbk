import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import requests

# Import dependencies
from app.client.encrypt import (
    API_KEY,
    build_encrypted_field,
    decrypt_xdata,
    encryptsign_xdata,
    get_x_signature_bounty,
    get_x_signature_loyalty,
    get_x_signature_bounty_allotment,
    java_like_timestamp,
)
from app.client.engsel import BASE_API_URL, UA
from app.client.purchase.common import standardize_response

# Setup Logger
logger = logging.getLogger(__name__)

class RedeemClient:
    """
    Client khusus untuk menangani penukaran hadiah, poin, dan voucher (Loyalty & Bounties).
    """

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get_headers(self, id_token: str, x_sig: str, xtime_str: str, x_req_at: str) -> Dict[str, str]:
        """Helper standard untuk menyusun header."""
        clean_host = BASE_API_URL.replace("https://", "").replace("http://", "").split("/")[0]
        return {
            "host": clean_host,
            "content-type": "application/json; charset=utf-8",
            "user-agent": UA,
            "x-api-key": self.api_key,
            "authorization": f"Bearer {id_token}",
            "x-hv": "v3",
            "x-signature-time": xtime_str,
            "x-signature": x_sig,
            "x-request-id": str(uuid.uuid4()),
            "x-request-at": x_req_at,
            "x-version-app": "8.9.0",
        }

    def _send_encrypted_request(
        self,
        path: str,
        payload: Dict[str, Any],
        tokens: Dict[str, str],
        signature_func: callable,
        signature_kwargs: Dict[str, Any],
        ts_to_sign: int
    ) -> Optional[Dict[str, Any]]:
        """
        Core Wrapper untuk enkripsi -> sign -> request -> decrypt.
        Mengurangi duplikasi kode secara drastis.
        """
        # 1. Encrypt Payload
        try:
            encrypted_data = encryptsign_xdata(
                api_key=self.api_key,
                method="POST",
                path=path,
                id_token=tokens["id_token"],
                payload=payload
            )
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            return None
        
        xtime = int(encrypted_data["encrypted_body"]["xtime"])
        sig_time_sec = xtime // 1000
        x_req_at = java_like_timestamp(datetime.fromtimestamp(sig_time_sec, tz=timezone.utc))

        # 2. Generate Signature using specific logic
        try:
            # Inject dynamic arguments if needed
            if "sig_time_sec" in signature_kwargs:
                signature_kwargs["sig_time_sec"] = ts_to_sign  # Use external TS usually
            
            x_sig = signature_func(**signature_kwargs)
        except Exception as e:
            logger.error(f"Signature generation failed: {e}")
            return None

        # 3. Send Request
        headers = self._get_headers(tokens["id_token"], x_sig, str(sig_time_sec), x_req_at)
        url = f"{BASE_API_URL}/{path}"

        logger.info(f"🎁 Sending redeem request to /{path}...")
        try:
            resp = requests.post(url, headers=headers, json=encrypted_data["encrypted_body"], timeout=30)
            
            # 4. Decrypt Response
            try:
                decrypted = decrypt_xdata(self.api_key, resp.json())
                result = standardize_response(decrypted)
                
                if result["status"] == "SUCCESS":
                    logger.info("✅ Redeem Successful!")
                else:
                    logger.error(f"❌ Redeem Failed: {result['message']}")
                
                return decrypted

            except Exception as e:
                logger.error(f"Decryption failed: {e}")
                return {"status": "ERROR", "message": "Decryption failed", "raw": resp.text}

        except requests.RequestException as e:
            logger.error(f"Network error: {e}")
            return None

    def settlement_bounty(
        self,
        tokens: Dict,
        token_confirmation: str,
        ts_to_sign: int,
        payment_target: str,
        price: int,
        item_name: str = "",
    ) -> Optional[Dict]:
        """
        Menukarkan Bounty/Voucher.
        """
        path = "api/v8/personalization/bounties-exchange"
        
        # Build Payload
        payload = {
            "total_discount": 0, "is_enterprise": False, "payment_token": "",
            "token_payment": "", "activated_autobuy_code": "", "cc_payment_type": "",
            "is_myxl_wallet": False, "pin": "", "ewallet_promo_id": "", "members": [],
            "total_fee": 0, "fingerprint": "",
            "autobuy_threshold_setting": {"label": "", "type": "", "value": 0},
            "is_use_point": False, "lang": "en", "payment_method": "BALANCE",
            "timestamp": ts_to_sign,
            "points_gained": 0, "can_trigger_rating": False,
            "akrab_members": [], "akrab_parent_alias": "", "referral_unique_code": "",
            "coupon": "", "payment_for": "REDEEM_VOUCHER", "with_upsell": False,
            "topup_number": "", "stage_token": "", "authentication_id": "",
            "encrypted_payment_token": build_encrypted_field(urlsafe_b64=True),
            "token": "", "token_confirmation": token_confirmation,
            "access_token": tokens["access_token"],
            "wallet_number": "",
            "encrypted_authentication_id": build_encrypted_field(urlsafe_b64=True),
            "additional_data": {
                "original_price": 0, "is_spend_limit_temporary": False, "migration_type": "",
                "akrab_m2m_group_id": "", "spend_limit_amount": 0, "is_spend_limit": False,
                "mission_id": "", "tax": 0, "benefit_type": "", "quota_bonus": 0,
                "cashtag": "", "is_family_plan": False, "combo_details": [],
                "is_switch_plan": False, "discount_recurring": 0, "is_akrab_m2m": False,
                "balance_type": "", "has_bonus": False, "discount_promo": 0
            },
            "total_amount": 0, "is_using_autobuy": False,
            "items": [{
                "item_code": payment_target, "product_type": "", "item_price": price,
                "item_name": item_name, "tax": 0
            }]
        }

        # Prepare Signature Args
        sig_args = {
            "api_key": self.api_key,
            "access_token": tokens["access_token"],
            "sig_time_sec": ts_to_sign,
            "package_code": payment_target,
            "token_payment": token_confirmation
        }

        return self._send_encrypted_request(
            path, payload, tokens, get_x_signature_bounty, sig_args, ts_to_sign
        )

    def settlement_loyalty(
        self,
        tokens: Dict,
        token_confirmation: str,
        ts_to_sign: int,
        payment_target: str,
        price: int,
    ) -> Optional[Dict]:
        """
        Menukarkan Poin Loyalty (Tiering).
        """
        path = "gamification/api/v8/loyalties/tiering/exchange"
        
        payload = {
            "item_code": payment_target,
            "amount": 0, "partner": "", "is_enterprise": False, "item_name": "",
            "lang": "en", "points": price,
            "timestamp": ts_to_sign,
            "token_confirmation": token_confirmation
        }

        sig_args = {
            "api_key": self.api_key,
            "sig_time_sec": ts_to_sign,
            "package_code": payment_target,
            "token_confirmation": token_confirmation,
            "path": path
        }

        return self._send_encrypted_request(
            path, payload, tokens, get_x_signature_loyalty, sig_args, ts_to_sign
        )

    def bounty_allotment(
        self,
        tokens: Dict,
        ts_to_sign: int,
        destination_msisdn: str,
        item_name: str,
        item_code: str,
        token_confirmation: str,
    ) -> Optional[Dict]:
        """
        Mengirim Hadiah/Gift (Allotment).
        """
        path = "gamification/api/v8/loyalties/tiering/bounties-allotment"
        
        payload = {
            "destination_msisdn": destination_msisdn,
            "item_code": item_code,
            "is_enterprise": False,
            "item_name": item_name,
            "lang": "en",
            "timestamp": int(datetime.now().timestamp()), # Timestamp payload beda dgn sign biasanya
            "token_confirmation": token_confirmation,
        }

        sig_args = {
            "api_key": self.api_key,
            "sig_time_sec": ts_to_sign,
            "package_code": item_code,
            "token_confirmation": token_confirmation,
            "destination_msisdn": destination_msisdn,
            "path": path
        }

        return self._send_encrypted_request(
            path, payload, tokens, get_x_signature_bounty_allotment, sig_args, ts_to_sign
        )


# =============================================================================
# COMPATIBILITY LAYER (Legacy Support)
# =============================================================================

def settlement_bounty(
    api_key: str,
    tokens: dict,
    token_confirmation: str,
    ts_to_sign: int,
    payment_target: str,
    price: int,
    item_name: str = "",
) -> Optional[Dict]:
    return RedeemClient(api_key).settlement_bounty(
        tokens, token_confirmation, ts_to_sign, payment_target, price, item_name
    )

def settlement_loyalty(
    api_key: str,
    tokens: dict,
    token_confirmation: str,
    ts_to_sign: int,
    payment_target: str,
    price: int,
) -> Optional[Dict]:
    return RedeemClient(api_key).settlement_loyalty(
        tokens, token_confirmation, ts_to_sign, payment_target, price
    )

def bounty_allotment(
    api_key: str,
    tokens: dict,
    ts_to_sign: int,
    destination_msisdn: str,
    item_name: str,
    item_code: str,
    token_confirmation: str,
) -> Optional[Dict]:
    return RedeemClient(api_key).bounty_allotment(
        tokens, ts_to_sign, destination_msisdn, item_name, item_code, token_confirmation
    )