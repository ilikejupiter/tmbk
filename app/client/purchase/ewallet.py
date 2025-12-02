import json
import logging
import time
import uuid
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import requests

# Import dependencies internal
from app.client.encrypt import (
    API_KEY, 
    decrypt_xdata, 
    encryptsign_xdata, 
    get_x_signature_payment, 
    java_like_timestamp
)
from app.client.engsel import BASE_API_URL, UA, intercept_page, send_api_request
from app.client.purchase.common import prompt_overwrite, standardize_response
from app.type_dict import PaymentItem

# Setup Logger
logger = logging.getLogger(__name__)

class EWalletPurchaseClient:
    """
    Client khusus untuk menangani pembelian menggunakan E-Wallet 
    (DANA, OVO, GOPAY, SHOPEEPAY).
    """

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get_headers(self, id_token: str, x_sig: str, xtime_str: str, x_req_at: str) -> Dict[str, str]:
        """Helper untuk menyusun header manual."""
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

    def _build_settlement_payload(
        self,
        amount: int,
        items: List[PaymentItem],
        payment_for: str,
        token_payment: str,
        wallet_number: str,
        payment_method: str,
        access_token: str
    ) -> Dict[str, Any]:
        """
        Menyusun payload spesifik untuk E-Wallet.
        """
        return {
            "akrab": {
                "akrab_members": [],
                "akrab_parent_alias": "",
                "members": []
            },
            "can_trigger_rating": False,
            "total_discount": 0,
            "coupon": "",
            "payment_for": payment_for,
            "topup_number": "",
            "is_enterprise": False,
            "autobuy": {
                "is_using_autobuy": False,
                "activated_autobuy_code": "",
                "autobuy_threshold_setting": {
                    "label": "",
                    "type": "",
                    "value": 0
                }
            },
            "cc_payment_type": "",
            "access_token": access_token,
            "is_myxl_wallet": False,
            "wallet_number": wallet_number,
            "additional_data": {},
            "total_amount": amount,
            "total_fee": 0,
            "is_use_point": False,
            "lang": "en",
            "items": items,
            "verification_token": token_payment, # Mapping penting: token_payment -> verification_token
            "payment_method": payment_method,
            "timestamp": int(time.time()) # Placeholder, akan di-override
        }

    def execute_purchase(
        self,
        tokens: Dict[str, str],
        items: List[PaymentItem],
        payment_for: str,
        amount_to_pay: int,
        wallet_number: str,
        payment_method: str,
        token_confirmation_idx: int = 0
    ) -> Optional[Dict[str, Any]]:
        """
        Core logic untuk eksekusi pembelian via E-Wallet.
        """
        if not items:
            logger.error("No items to purchase.")
            return None

        target_item_code = items[token_confirmation_idx].get("item_code", "")
        token_confirmation = items[token_confirmation_idx].get("token_confirmation", "")

        # 1. Trigger Intercept Page
        logger.info("Triggering intercept page...")
        try:
            intercept_page(self.api_key, tokens, items[0].get("item_code", ""), False)
        except Exception:
            pass

        # 2. Get Payment Methods & Token Payment
        logger.info("Fetching payment options...")
        payment_res = self._fetch_payment_options(tokens, target_item_code, token_confirmation)
        if not payment_res:
            return None

        token_payment = payment_res.get("token_payment")
        ts_to_sign = payment_res.get("timestamp")

        # 3. Construct Payload
        payload = self._build_settlement_payload(
            amount=amount_to_pay,
            items=items,
            payment_for=payment_for,
            token_payment=token_payment,
            wallet_number=wallet_number,
            payment_method=payment_method,
            access_token=tokens["access_token"]
        )
        
        # PENTING: Timestamp harus sinkron dengan data payment options
        payload["timestamp"] = ts_to_sign

        # 4. Encrypt Payload
        path = "payments/api/v8/settlement-multipayment/ewallet"
        encrypted_data = encryptsign_xdata(
            api_key=self.api_key,
            method="POST",
            path=path,
            id_token=tokens["id_token"],
            payload=payload
        )
        
        xtime = int(encrypted_data["encrypted_body"]["xtime"])
        sig_time_sec = xtime // 1000
        x_req_at = java_like_timestamp(datetime.fromtimestamp(sig_time_sec, tz=timezone.utc))

        # 5. Generate Signature
        payment_targets = ";".join([i["item_code"] for i in items])
        x_sig = get_x_signature_payment(
            self.api_key,
            tokens["access_token"],
            ts_to_sign,
            payment_targets,
            token_payment,
            payment_method,
            payment_for,
            path
        )

        # 6. Send Request
        headers = self._get_headers(tokens["id_token"], x_sig, str(sig_time_sec), x_req_at)
        url = f"{BASE_API_URL}/{path}"
        
        logger.info(f"🚀 Sending E-Wallet settlement ({payment_method})...")
        try:
            resp = requests.post(url, headers=headers, json=encrypted_data["encrypted_body"], timeout=60)
            
            # Decrypt & Handle Response
            try:
                decrypted = decrypt_xdata(self.api_key, resp.json())
                result = standardize_response(decrypted)
                
                if result["status"] == "SUCCESS":
                    logger.info("✅ E-Wallet Transaction Initiated!")
                    self._handle_success_deeplink(result["data"], payment_method)
                else:
                    logger.error(f"❌ Transaction Failed: {result['message']}")
                
                return decrypted
                
            except Exception as e:
                logger.error(f"Decryption failed: {e}")
                return {"status": "ERROR", "message": "Response decryption failed", "raw": resp.text}

        except requests.RequestException as e:
            logger.error(f"Network error during settlement: {e}")
            return None

    def _fetch_payment_options(self, tokens: Dict, target_code: str, token_conf: str) -> Optional[Dict]:
        """Internal helper untuk mengambil opsi pembayaran."""
        path = "payments/api/v8/payment-methods-option"
        payload = {
            "payment_type": "PURCHASE",
            "is_enterprise": False,
            "payment_target": target_code,
            "lang": "en",
            "is_referral": False,
            "token_confirmation": token_conf
        }
        res = send_api_request(self.api_key, path, payload, tokens["id_token"], "POST")
        normalized = standardize_response(res)
        
        if normalized["status"] == "SUCCESS":
            return normalized["data"]
        
        logger.error(f"Failed to fetch payment methods: {normalized['message']}")
        return None

    def _handle_success_deeplink(self, data: Dict, method: str):
        """Helper untuk menampilkan instruksi pembayaran ke user."""
        if not data: return
        
        deeplink = data.get("deeplink", "")
        if method == "OVO":
            print("\n🔔 Silahkan cek notifikasi di aplikasi OVO Anda untuk konfirmasi pembayaran.")
        elif deeplink:
            print(f"\n🔔 Silahkan selesaikan pembayaran melalui link berikut:\n🔗 {deeplink}")
        else:
            print("\n🔔 Pembayaran berhasil diinisiasi. Silahkan cek aplikasi E-Wallet Anda.")


# =============================================================================
# INTERACTIVE FUNCTIONS
# =============================================================================

def show_multipayment(
    api_key: str,
    tokens: dict,
    items: List[PaymentItem],
    payment_for: str,
    ask_overwrite: bool,
    overwrite_amount: int = -1,
    token_confirmation_idx: int = 0,
    amount_idx: int = -1,
):
    """
    Interactive function untuk memilih E-Wallet dan memasukkan nomor HP.
    """
    # 1. Select Payment Method
    print("\n--- Pilihan E-Wallet ---")
    print("1. DANA")
    print("2. ShopeePay")
    print("3. GoPay")
    print("4. OVO")
    
    method_map = {
        "1": "DANA",
        "2": "SHOPEEPAY",
        "3": "GOPAY",
        "4": "OVO"
    }
    
    payment_method = ""
    wallet_number = ""
    
    while True:
        choice = input("Pilih metode (1-4): ").strip()
        if choice in method_map:
            payment_method = method_map[choice]
            break
        print("❌ Pilihan tidak valid.")

    # 2. Input Wallet Number (Only for DANA & OVO usually required, but safe to ask)
    # ShopeePay/GoPay biasanya pakai deeplink app-to-app, tapi API kadang butuh nomor.
    if payment_method in ["DANA", "OVO", "SHOPEEPAY", "GOPAY"]:
        while True:
            wallet_number = input(f"Masukkan nomor {payment_method} (08xxx): ").strip()
            # Validasi Regex sederhana: Mulai 08, 10-13 digit
            if re.match(r"^08\d{8,11}$", wallet_number):
                break
            print("❌ Nomor tidak valid. Format: 08xxxxxxxxxx (10-13 digit).")

    # 3. Determine Amount
    default_price = 0
    if overwrite_amount != -1:
        default_price = overwrite_amount
    elif amount_idx != -1 and items:
        default_price = items[amount_idx].get("item_price", 0)
    elif items:
        default_price = items[0].get("item_price", 0)

    final_amount = prompt_overwrite(default_price, ask_overwrite, interactive=True)

    # 4. Execute
    client = EWalletPurchaseClient(api_key)
    client.execute_purchase(
        tokens=tokens,
        items=items,
        payment_for=payment_for,
        amount_to_pay=final_amount,
        wallet_number=wallet_number,
        payment_method=payment_method,
        token_confirmation_idx=token_confirmation_idx
    )


# =============================================================================
# COMPATIBILITY LAYER (Legacy Support)
# =============================================================================

def settlement_multipayment(
    api_key: str,
    tokens: dict,
    items: List[PaymentItem],
    wallet_number: str,
    payment_method: str,
    payment_for: str,
    ask_overwrite: bool,
    overwrite_amount: int = -1,
    token_confirmation_idx: int = 0,
    amount_idx: int = -1,
) -> Optional[Dict]:
    """Legacy wrapper for settlement_multipayment"""
    
    # Logic penentuan harga legacy
    default_price = 0
    if overwrite_amount != -1:
        default_price = overwrite_amount
    elif amount_idx != -1 and items:
        default_price = items[amount_idx].get("item_price", 0)
    elif items:
        default_price = items[0].get("item_price", 0)
        
    final_amount = prompt_overwrite(default_price, ask_overwrite, interactive=True)

    client = EWalletPurchaseClient(api_key)
    return client.execute_purchase(
        tokens=tokens,
        items=items,
        payment_for=payment_for,
        amount_to_pay=final_amount,
        wallet_number=wallet_number,
        payment_method=payment_method,
        token_confirmation_idx=token_confirmation_idx
    )