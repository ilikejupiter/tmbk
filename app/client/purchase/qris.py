import base64
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import requests

# Try import qrcode safely (Anti-Crash)
try:
    import qrcode
    HAS_QRCODE_LIB = True
except ImportError:
    HAS_QRCODE_LIB = False

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

class QrisPurchaseClient:
    """
    Client khusus untuk menangani pembelian via QRIS.
    Flow: Intercept -> Payment Options -> Settlement -> Get Transaction ID -> Get QR Code -> Render.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get_headers(self, id_token: str, x_sig: str, xtime_str: str, x_req_at: str) -> Dict[str, str]:
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
        topup_number: str,
        stage_token: str,
        access_token: str
    ) -> Dict[str, Any]:
        """Menyusun payload spesifik QRIS."""
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
            "topup_number": topup_number,
            "stage_token": stage_token,
            "is_enterprise": False,
            "autobuy": {
                "is_using_autobuy": False,
                "activated_autobuy_code": "",
                "autobuy_threshold_setting": {"label": "", "type": "", "value": 0}
            },
            "access_token": access_token,
            "is_myxl_wallet": False,
            "additional_data": {
                "original_price": items[0]["item_price"] if items else 0,
                "is_spend_limit_temporary": False,
                "migration_type": "",
                "spend_limit_amount": 0,
                "is_spend_limit": False,
                "tax": 0,
                "benefit_type": "",
                "quota_bonus": 0,
                "cashtag": "",
                "is_family_plan": False,
                "combo_details": [],
                "is_switch_plan": False,
                "discount_recurring": 0,
                "has_bonus": False,
                "discount_promo": 0
            },
            "total_amount": amount,
            "total_fee": 0,
            "is_use_point": False,
            "lang": "en",
            "items": items,
            "verification_token": token_payment, # QRIS uses 'verification_token'
            "payment_method": "QRIS",
            "timestamp": int(time.time()), # Placeholder
        }

    def execute_transaction(
        self,
        tokens: Dict[str, str],
        items: List[PaymentItem],
        payment_for: str,
        amount_to_pay: int,
        token_confirmation_idx: int = 0,
        topup_number: str = "",
        stage_token: str = ""
    ) -> Optional[str]:
        """
        Langkah 1: Melakukan Settlement dan mendapatkan Transaction ID.
        Returns: transaction_id (str) atau None.
        """
        if not items:
            logger.error("No items to purchase.")
            return None

        target_item_code = items[token_confirmation_idx].get("item_code", "")
        token_confirmation = items[token_confirmation_idx].get("token_confirmation", "")

        # 1. Intercept Page
        try:
            intercept_page(self.api_key, tokens, items[0].get("item_code", ""), False)
        except Exception:
            pass

        # 2. Get Payment Methods
        logger.info("Fetching payment options for QRIS...")
        payment_res = self._fetch_payment_options(tokens, target_item_code, token_confirmation)
        if not payment_res:
            return None

        token_payment = payment_res.get("token_payment")
        ts_to_sign = payment_res.get("timestamp")

        # 3. Build & Encrypt Payload
        payload = self._build_settlement_payload(
            amount_to_pay, items, payment_for, token_payment, 
            topup_number, stage_token, tokens["access_token"]
        )
        payload["timestamp"] = ts_to_sign

        path = "payments/api/v8/settlement-multipayment/qris"
        encrypted_data = encryptsign_xdata(
            api_key=self.api_key, method="POST", path=path,
            id_token=tokens["id_token"], payload=payload
        )
        
        xtime = int(encrypted_data["encrypted_body"]["xtime"])
        sig_time_sec = xtime // 1000
        x_req_at = java_like_timestamp(datetime.fromtimestamp(sig_time_sec, tz=timezone.utc))

        # 4. Sign Request
        payment_targets = ";".join([i["item_code"] for i in items])
        x_sig = get_x_signature_payment(
            self.api_key, tokens["access_token"], ts_to_sign,
            payment_targets, token_payment, "QRIS", payment_for, path
        )

        # 5. Send Request
        headers = self._get_headers(tokens["id_token"], x_sig, str(sig_time_sec), x_req_at)
        url = f"{BASE_API_URL}/{path}"
        
        logger.info("🚀 Sending QRIS settlement request...")
        try:
            resp = requests.post(url, headers=headers, json=encrypted_data["encrypted_body"], timeout=60)
            decrypted = decrypt_xdata(self.api_key, resp.json())
            result = standardize_response(decrypted)
            
            if result["status"] == "SUCCESS":
                trx_id = result["data"].get("transaction_code")
                logger.info(f"✅ Settlement Success! Trx ID: {trx_id}")
                return trx_id
            else:
                logger.error(f"❌ Settlement Failed: {result['message']}")
                return None
                
        except Exception as e:
            logger.error(f"Error during QRIS settlement: {e}")
            return None

    def get_qr_string(self, tokens: Dict, transaction_id: str) -> Optional[str]:
        """
        Langkah 2: Mengambil string QR Code mentah berdasarkan Transaction ID.
        """
        path = "payments/api/v8/pending-detail"
        payload = {
            "transaction_id": transaction_id,
            "is_enterprise": False,
            "lang": "en",
            "status": ""
        }
        
        logger.info("Fetching QR Code string...")
        res = send_api_request(self.api_key, path, payload, tokens["id_token"], "POST")
        result = standardize_response(res)
        
        if result["status"] == "SUCCESS":
            return result["data"].get("qr_code")
        
        logger.error(f"Failed to fetch QR String: {result['message']}")
        return None

    def render_qr_terminal(self, qr_string: str):
        """
        Langkah 3: Menampilkan QR Code di terminal dan link backup.
        """
        if not qr_string:
            return

        print("\n" + "="*40)
        print("   SCAN THIS QR CODE TO PAY")
        print("="*40 + "\n")

        # 1. Render ASCII QR
        if HAS_QRCODE_LIB:
            try:
                qr = qrcode.QRCode(
                    version=1,
                    error_correction=qrcode.constants.ERROR_CORRECT_L,
                    box_size=1,
                    border=1,
                )
                qr.add_data(qr_string)
                qr.make(fit=True)
                qr.print_ascii(invert=True)
            except Exception as e:
                logger.warning(f"Could not render ASCII QR: {e}")
        else:
            print("[Info] Library 'qrcode' not installed. Skipping ASCII render.")

        # 2. Render Web Link
        qris_b64 = base64.urlsafe_b64encode(qr_string.encode()).decode()
        qris_url = f"https://ki-ar-kod.netlify.app/?data={qris_b64}"
        
        print("\n" + "-"*40)
        print("JIKA QR TIDAK MUNCUL/SUSAH DI-SCAN:")
        print(f"🔗 Buka link: {qris_url}")
        print("-"*40 + "\n")

    def _fetch_payment_options(self, tokens: Dict, target_code: str, token_conf: str) -> Optional[Dict]:
        path = "payments/api/v8/payment-methods-option"
        payload = {
            "payment_type": "PURCHASE", "is_enterprise": False,
            "payment_target": target_code, "lang": "en",
            "is_referral": False, "token_confirmation": token_conf
        }
        res = send_api_request(self.api_key, path, payload, tokens["id_token"], "POST")
        normalized = standardize_response(res)
        return normalized["data"] if normalized["status"] == "SUCCESS" else None


# =============================================================================
# COMPATIBILITY & INTERACTIVE FUNCTIONS
# =============================================================================

def show_qris_payment(
    api_key: str,
    tokens: dict,
    items: List[PaymentItem],
    payment_for: str,
    ask_overwrite: bool,
    overwrite_amount: int = -1,
    token_confirmation_idx: int = 0,
    amount_idx: int = -1,
    topup_number: str = "",
    stage_token: str = "",
):
    """
    Main entry point untuk pembayaran QRIS.
    Menggabungkan semua langkah menjadi satu flow interaktif.
    """
    # 1. Determine Amount
    default_price = 0
    if overwrite_amount != -1:
        default_price = overwrite_amount
    elif amount_idx != -1 and items:
        default_price = items[amount_idx].get("item_price", 0)
    elif items:
        default_price = items[0].get("item_price", 0)

    final_amount = prompt_overwrite(default_price, ask_overwrite, interactive=True)

    # 2. Init Client
    client = QrisPurchaseClient(api_key)

    # 3. Execute Settlement
    trx_id = client.execute_transaction(
        tokens=tokens,
        items=items,
        payment_for=payment_for,
        amount_to_pay=final_amount,
        token_confirmation_idx=token_confirmation_idx,
        topup_number=topup_number,
        stage_token=stage_token
    )

    if not trx_id:
        print("❌ Gagal membuat transaksi QRIS.")
        return None

    # 4. Fetch QR String
    qr_code_str = client.get_qr_string(tokens, trx_id)

    # 5. Render
    if qr_code_str:
        client.render_qr_terminal(qr_code_str)
        return qr_code_str
    
    return None

# Legacy Wrapper for compatibility with old calls
def settlement_qris(
    api_key: str,
    tokens: dict,
    items: List[PaymentItem],
    payment_for: str,
    ask_overwrite: bool,
    overwrite_amount: int = -1,
    token_confirmation_idx: int = 0,
    amount_idx: int = -1,
    topup_number: str = "",
    stage_token: str = "",
):
    # Logic legacy hanya mengembalikan Trx ID
    # Namun flow ini jarang dipanggil sendirian, biasanya via show_qris_payment
    client = QrisPurchaseClient(api_key)
    
    # Resolve Amount Logic Duplicate
    default_price = items[amount_idx].get("item_price", 0) if (amount_idx != -1 and items) else 0
    if overwrite_amount != -1: default_price = overwrite_amount
    final_amount = prompt_overwrite(default_price, ask_overwrite, interactive=True)
    
    return client.execute_transaction(
        tokens, items, payment_for, final_amount, 
        token_confirmation_idx, topup_number, stage_token
    )

def get_qris_code(api_key: str, tokens: dict, transaction_id: str):
    return QrisPurchaseClient(api_key).get_qr_string(tokens, transaction_id)