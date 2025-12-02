import json
import logging
import time
import uuid
import traceback  # Wajib ada untuk melihat penyebab crash
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import requests

# Import dependencies internal
from app.client.encrypt import (
    API_KEY, 
    build_encrypted_field, 
    decrypt_xdata, 
    encryptsign_xdata, 
    get_x_signature_payment, 
    java_like_timestamp
)
from app.client.engsel import BASE_API_URL, UA, intercept_page, send_api_request
from app.client.purchase.common import prompt_overwrite, standardize_response
from app.type_dict import PaymentItem

# Setup Logger agar tampil di layar dengan jelas
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("BalancePurchase")

class BalancePurchaseClient:
    """
    Client Pembelian Pulsa (Balance) - Ultimate Stable Version.
    Fitur:
    - Anti Force Close (Crash Protection)
    - Full JSON Logging (S&K/Detail tidak dipotong)
    - Modular & Clean
    """

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get_headers(self, id_token: str, x_sig: str, xtime_str: str, x_req_at: str) -> Dict[str, str]:
        """Menyusun header manual untuk request payment."""
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
        access_token: str,
        timestamp_override: int
    ) -> Dict[str, Any]:
        """
        Menyusun payload settlement yang kompleks.
        """
        # Generate encrypted fields on the fly
        enc_payment_token = build_encrypted_field(urlsafe_b64=True)
        enc_auth_id = build_encrypted_field(urlsafe_b64=True)

        # Mengambil harga asli dari item terakhir (biasanya target utama)
        original_price = items[-1].get("item_price", 0) if items else 0

        return {
            "total_discount": 0,
            "is_enterprise": False,
            "payment_token": "",
            "token_payment": token_payment,
            "activated_autobuy_code": "",
            "cc_payment_type": "",
            "is_myxl_wallet": False,
            "pin": "",
            "ewallet_promo_id": "",
            "members": [],
            "total_fee": 0,
            "fingerprint": "",
            "autobuy_threshold_setting": {"label": "", "type": "", "value": 0},
            "is_use_point": False,
            "lang": "en",
            "payment_method": "BALANCE", 
            "timestamp": timestamp_override, # Timestamp disinkronkan dengan payment options
            "points_gained": 0,
            "can_trigger_rating": False,
            "akrab_members": [],
            "akrab_parent_alias": "",
            "referral_unique_code": "",
            "coupon": "",
            "payment_for": payment_for,
            "with_upsell": False,
            "topup_number": topup_number,
            "stage_token": stage_token,
            "authentication_id": "",
            "encrypted_payment_token": enc_payment_token,
            "token": "",
            "token_confirmation": "",
            "access_token": access_token,
            "wallet_number": "",
            "encrypted_authentication_id": enc_auth_id,
            "additional_data": {
                "original_price": original_price,
                "is_spend_limit_temporary": False,
                "migration_type": "",
                "akrab_m2m_group_id": "false",
                "spend_limit_amount": 0,
                "is_spend_limit": False,
                "mission_id": "",
                "tax": 0,
                "quota_bonus": 0,
                "cashtag": "",
                "is_family_plan": False,
                "combo_details": [],
                "is_switch_plan": False,
                "discount_recurring": 0,
                "is_akrab_m2m": False,
                "balance_type": "PREPAID_BALANCE",
                "has_bonus": False,
                "discount_promo": 0
            },
            "total_amount": amount,
            "is_using_autobuy": False,
            "items": items,
        }

    def execute_purchase(
        self,
        tokens: Dict[str, str],
        items: List[PaymentItem],
        payment_for: str,
        amount_to_pay: int,
        token_confirmation_idx: int = 0,
        topup_number: str = "",
        stage_token: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Core logic untuk eksekusi pembelian.
        Dibungkus try-catch global untuk mencegah aplikasi keluar tiba-tiba.
        """
        try:
            logger.info("-" * 50)
            logger.info(f"🚀 MEMULAI TRANSAKSI (Balance)")
            logger.info(f"💰 Total Bayar : Rp {amount_to_pay}")
            logger.info(f"📦 Jumlah Item : {len(items)}")

            if not items:
                logger.error("❌ Error: Item list kosong!")
                return None

            # Safety Check Index
            if token_confirmation_idx >= len(items):
                logger.error(f"❌ Error: Index token confirmation ({token_confirmation_idx}) di luar batas.")
                return None

            target_item_code = items[token_confirmation_idx].get("item_code", "")
            token_confirmation = items[token_confirmation_idx].get("token_confirmation", "")

            # 1. Trigger Intercept Page (Standard Flow XL)
            try:
                intercept_page(self.api_key, tokens, items[0].get("item_code", ""), False)
            except Exception:
                pass # Ignore intercept errors

            # 2. Get Payment Methods & Token Payment
            logger.info("📡 Mengambil Opsi Pembayaran...")
            payment_res = self._fetch_payment_options(tokens, target_item_code, token_confirmation)
            
            if not payment_res:
                logger.error("❌ Gagal mengambil payment options. Transaksi dibatalkan.")
                return None

            token_payment = payment_res.get("token_payment")
            ts_to_sign = payment_res.get("timestamp")

            if not token_payment or not ts_to_sign:
                logger.error("❌ Respon payment options tidak lengkap (Missing token/timestamp).")
                return None

            # 3. Construct Payload
            payload = self._build_settlement_payload(
                amount=amount_to_pay,
                items=items,
                payment_for=payment_for,
                token_payment=token_payment,
                topup_number=topup_number,
                stage_token=stage_token,
                access_token=tokens["access_token"],
                timestamp_override=ts_to_sign
            )

            # 4. Encrypt Payload & Generate Signature
            path = "payments/api/v8/settlement-multipayment"
            
            try:
                encrypted_data = encryptsign_xdata(
                    api_key=self.api_key,
                    method="POST",
                    path=path,
                    id_token=tokens["id_token"],
                    payload=payload
                )
            except Exception as e:
                logger.error(f"❌ Gagal Enkripsi Payload: {e}")
                return None
            
            xtime = int(encrypted_data["encrypted_body"]["xtime"])
            sig_time_sec = xtime // 1000
            x_req_at = java_like_timestamp(datetime.fromtimestamp(sig_time_sec, tz=timezone.utc))

            # Generate Payment Specific Signature
            payment_targets = ";".join([i["item_code"] for i in items])
            x_sig = get_x_signature_payment(
                self.api_key,
                tokens["access_token"],
                ts_to_sign,
                payment_targets,
                token_payment,
                "BALANCE",
                payment_for,
                path
            )

            # 5. Send Request
            headers = self._get_headers(tokens["id_token"], x_sig, str(sig_time_sec), x_req_at)
            url = f"{BASE_API_URL}/{path}"
            
            logger.info(f"📨 Mengirim Settlement Request...")
            
            resp = requests.post(url, headers=headers, json=encrypted_data["encrypted_body"], timeout=60)
            
            # 6. Decrypt & Show Full Result
            try:
                if resp.status_code >= 500:
                    logger.error(f"❌ Server Error {resp.status_code}. Response bukan JSON.")
                    return {"status": "ERROR", "message": "Server Error", "raw": resp.text}

                decrypted = decrypt_xdata(self.api_key, resp.json())
                result = standardize_response(decrypted)
                
                logger.info("=" * 50)
                if result["status"] == "SUCCESS":
                    logger.info("✅ TRANSAKSI BERHASIL!")
                else:
                    logger.error(f"❌ TRANSAKSI GAGAL: {result['message']}")
                
                # FIX "KEPOTONG": Print JSON Full Dump
                print("\n[DETAIL LOG TRANSAKSI LENGKAP]")
                print(json.dumps(decrypted, indent=4)) # Indent 4 agar rapi dan terbaca semua
                print("=" * 50)
                
                return decrypted 
                
            except Exception as e:
                logger.error(f"❌ Gagal Dekripsi Response: {e}")
                logger.debug(f"Raw Response: {resp.text}")
                return {"status": "ERROR", "message": "Decryption Failed", "raw": resp.text}

        except Exception as e:
            # INI FITUR ANTI-CLOSE: Menangkap semua error tak terduga
            print("\n" + "!"*50)
            print("💥 TERJADI CRASH PADA SCRIPT 💥")
            print(f"Penyebab: {str(e)}")
            print("-" * 50)
            print("Jejak Error (Traceback):")
            traceback.print_exc() # Cetak detail error
            print("!"*50)
            input("\n[Tekan Enter untuk kembali ke menu...]") # Tahan layar
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
        
        try:
            res = send_api_request(self.api_key, path, payload, tokens["id_token"], "POST")
            normalized = standardize_response(res)
            
            if normalized["status"] == "SUCCESS":
                return normalized["data"]
            
            logger.error(f"Gagal ambil payment options: {normalized['message']}")
            return None
        except Exception as e:
            logger.error(f"Error koneksi payment options: {e}")
            return None


# =============================================================================
# COMPATIBILITY LAYER (Legacy Support)
# =============================================================================

def settlement_balance(
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
) -> Optional[Dict]:
    """Legacy wrapper agar tidak merusak main.py"""
    
    # 1. Determine Amount (Logic Lama dipertahankan)
    default_price = 0
    if overwrite_amount != -1:
        default_price = overwrite_amount
    elif amount_idx != -1 and items:
        # Safety check list index
        if 0 <= amount_idx < len(items):
            default_price = items[amount_idx].get("item_price", 0)
    elif items:
        default_price = items[0].get("item_price", 0)

    # 2. Prompt user (Interactive)
    final_amount = prompt_overwrite(default_price, ask_overwrite, interactive=True)

    # 3. Execute Safe Method
    client = BalancePurchaseClient(api_key)
    return client.execute_purchase(
        tokens=tokens,
        items=items,
        payment_for=payment_for,
        amount_to_pay=final_amount,
        token_confirmation_idx=token_confirmation_idx,
        topup_number=topup_number,
        stage_token=stage_token
    )