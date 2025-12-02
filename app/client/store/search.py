import logging
from typing import Dict, Any, List, Optional, Union

from app.client.engsel import send_api_request

# Setup Logger
logger = logging.getLogger(__name__)

# Type definitions
TokenDict = Dict[str, str]
ApiResponse = Dict[str, Any]

class SearchClient:
    """
    Client khusus untuk fitur pencarian (Store & Family).
    Memiliki logic parsing tingkat lanjut untuk menormalisasi response paket yang tidak konsisten.
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
        """Wrapper internal dengan centralized error handling"""
        final_payload = {
            "is_enterprise": False,
            "lang": "en",
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

    def get_family_list(
        self, 
        tokens: TokenDict, 
        subs_type: str = "PREPAID", 
        is_enterprise: bool = False
    ) -> Optional[ApiResponse]:
        """
        Mencari daftar opsi Family Plan yang tersedia.
        """
        response = self._send_request(
            path="api/v8/xl-stores/options/search/family-list",
            payload={"subs_type": subs_type, "is_enterprise": is_enterprise},
            id_token=tokens.get("id_token", ""),
            description="🔍 Searching Family List..."
        )

        if response and response.get("status") == "SUCCESS":
            data = response.get("data", [])
            count = len(data) if isinstance(data, list) else 0
            logger.info(f"✅ Found {count} family categories.")
            return response
        
        logger.error("❌ Failed to fetch family list.")
        return None

    def get_store_packages(
        self,
        tokens: TokenDict,
        subs_type: str = "PREPAID",
        is_enterprise: bool = False,
        preview_limit: int = 10
    ) -> Optional[ApiResponse]:
        """
        Mencari paket di Store dengan filter default.
        Fitur Utama: AUTO-NORMALIZATION response menjadi List rata.
        """
        payload = {
            "is_enterprise": is_enterprise,
            "substype": subs_type,
            "text_search": "",
            "filters": [
                {"unit": "THOUSAND", "id": "FIL_SEL_P", "type": "PRICE", "items": []},
                {"unit": "GB", "id": "FIL_SEL_MQ", "type": "DATA_TYPE", "items": []},
                {"unit": "PACKAGE_NAME", "id": "FIL_PKG_N", "type": "PACKAGE_NAME", "items": [{"id": "", "label": ""}]},
                {"unit": "DAY", "id": "FIL_SEL_V", "type": "VALIDITY", "items": []}
            ]
        }

        response = self._send_request(
            path="api/v9/xl-stores/options/search",
            payload=payload,
            id_token=tokens.get("id_token", ""),
            description="🔍 Searching Store Packages..."
        )

        if not response or response.get("status") != "SUCCESS":
            logger.error("❌ Failed to fetch store packages.")
            return None

        # --- SMART PARSING LOGIC ---
        # Normalisasi data karena struktur API v9 sering berubah-ubah
        raw_data = response.get("data")
        packages = self._normalize_package_list(raw_data)

        logger.info(f"✅ Found {len(packages)} packages/items.")
        
        if packages and preview_limit > 0:
            self._log_package_preview(packages, preview_limit)

        return response

    def _normalize_package_list(self, data: Any) -> List[Dict]:
        """
        Helper cerdas untuk mengekstrak list paket dari berbagai bentuk response API.
        """
        if isinstance(data, list):
            return data
        
        if isinstance(data, dict):
            # Coba cari key umum tempat list paket bersembunyi
            for key in ["packages", "items", "results", "list", "data"]:
                val = data.get(key)
                if isinstance(val, list):
                    return val
            # Jika dict tunggal dan tidak punya key list, anggap dia item tunggal
            return [data]
            
        return []

    def _log_package_preview(self, packages: List[Dict], limit: int):
        """Menampilkan tabel preview paket ke log agar mudah dibaca manusia."""
        try:
            sep = "-" * 65
            logger.info(sep)
            logger.info(f"{'NAME':<35} | {'PRICE':<12} | {'CODE'}")
            logger.info(sep)
            
            for pkg in packages[:limit]:
                if not isinstance(pkg, dict):
                    continue
                    
                name = pkg.get("name") or pkg.get("title") or pkg.get("package_name") or "Unknown"
                # Handle price (bisa int, str, atau nested dict)
                price = pkg.get("price", "N/A")
                if isinstance(price, dict):
                    price = price.get("amount", "N/A")
                
                code = pkg.get("package_variant_code") or pkg.get("code") or pkg.get("id") or "N/A"
                
                # Truncate nama yang kepanjangan
                name_str = str(name)[:33]
                logger.info(f"{name_str:<35} | {str(price):<12} | {code}")
            
            logger.info(sep)
        except Exception:
            pass # Jangan sampai error logging menghentikan flow


# =============================================================================
# COMPATIBILITY LAYER (Legacy Support)
# =============================================================================

def get_family_list(
    api_key: str,
    tokens: dict,
    subs_type: str = "PREPAID",
    is_enterprise: bool = False,
    logger: Optional[Any] = None # Parameter logger diabaikan agar konsisten
) -> Optional[Dict]:
    return SearchClient(api_key).get_family_list(tokens, subs_type, is_enterprise)

def get_store_packages(
    api_key: str,
    tokens: dict,
    subs_type: str = "PREPAID",
    is_enterprise: bool = False,
    logger: Optional[Any] = None,
    preview_limit: int = 10,
) -> Optional[Dict]:
    return SearchClient(api_key).get_store_packages(
        tokens, subs_type, is_enterprise, preview_limit
    )
