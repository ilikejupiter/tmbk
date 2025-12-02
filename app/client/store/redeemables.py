from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from app.client.engsel import send_api_request

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

TokenDict = Mapping[str, str]
ApiResponse = Dict[str, Any]
JsonDict = Dict[str, Any]


def _status_is_success(response: Optional[ApiResponse]) -> bool:
    if not isinstance(response, dict):
        return False
    return str(response.get("status", "")).strip().upper() == "SUCCESS"


class RedeemableClient:
    """
    Client untuk Personalization & Redeemables.
    Fokus: struktur response konsisten, helper pencarian/flatten yang berguna untuk automation.
    """

    def __init__(self, api_key: str, *, lang: str = "en", default_is_enterprise: bool = False) -> None:
        self.api_key = api_key
        self.lang = lang
        self.default_is_enterprise = default_is_enterprise

    def _build_payload(self, payload: JsonDict) -> JsonDict:
        return {"is_enterprise": self.default_is_enterprise, "lang": self.lang, **(payload or {})}

    def _send_request(
        self,
        *,
        path: str,
        payload: JsonDict,
        id_token: str,
        description: str = "",
        method: str = "POST",
    ) -> ApiResponse:
        if description:
            logger.info(description)

        if not id_token:
            return {"status": "FAILED", "message": "Missing id_token", "data": None}

        try:
            res = send_api_request(self.api_key, path, self._build_payload(payload), id_token, method)
            if not isinstance(res, dict):
                return {"status": "FAILED", "message": "Non-dict response from API", "data": None}
            if not res:
                return {"status": "FAILED", "message": "Empty response", "data": None}
            return res
        except Exception as e:
            logger.exception("Error executing %s", path)
            return {"status": "FAILED", "message": str(e), "data": None}

    def get_redeemables(self, tokens: TokenDict, is_enterprise: bool = False) -> Optional[ApiResponse]:
        """
        Mengambil daftar item yang bisa diklaim (Redeemables).
        """
        response = self._send_request(
            path="api/v8/personalization/redeemables",
            payload={"is_enterprise": is_enterprise},
            id_token=tokens.get("id_token", ""),
            description="🎁 Fetching Redeemable items...",
        )

        if _status_is_success(response):
            data = response.get("data", {}) or {}
            categories = data.get("categories", []) if isinstance(data, dict) else []
            logger.info("✅ Found %s categories of redeemables.", len(categories) if isinstance(categories, list) else 0)
            return response

        logger.error("❌ Failed to fetch redeemables. Status=%s", response.get("status"))
        return None

    def iter_redeemables(self, tokens: TokenDict, *, category_name: str = "") -> List[Dict[str, Any]]:
        """
        Flatten redeemables jadi list paket saja (opsional filter kategori).
        Setiap item ditambah metadata kategori supaya gampang dipakai.
        """
        raw = self.get_redeemables(tokens)
        if not raw or not isinstance(raw.get("data"), dict):
            return []

        data = raw["data"]
        categories = data.get("categories", [])
        if not isinstance(categories, list):
            return []

        out: List[Dict[str, Any]] = []
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            cat_name = str(cat.get("name", "") or "")
            if category_name and category_name.upper() not in cat_name.upper():
                continue

            packages = cat.get("packages", [])
            if not isinstance(packages, list):
                continue

            for pkg in packages:
                if not isinstance(pkg, dict):
                    continue
                item = dict(pkg)
                item["_category_name"] = cat_name
                out.append(item)

        return out

    def find_redeemable_by_keyword(
        self,
        tokens: TokenDict,
        keyword: str,
        category_name: str = "INTERNET",
        *,
        search_in_description: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Cari item redeemable berdasarkan keyword (name/description).
        """
        if not keyword:
            return []

        items = self.iter_redeemables(tokens, category_name=category_name)
        kw = keyword.strip().lower()

        found: List[Dict[str, Any]] = []
        for pkg in items:
            name = str(pkg.get("name", "") or "").lower()
            desc = str(pkg.get("description", "") or "").lower()

            if kw in name or (search_in_description and kw in desc):
                found.append(pkg)

        logger.info("🔎 Search '%s': Found %s items.", keyword, len(found))
        return found


# =============================================================================
# COMPATIBILITY LAYER (Legacy Support)
# =============================================================================

def get_redeemables(
    api_key: str,
    tokens: dict,
    is_enterprise: bool = False,
) -> Dict[str, Any]:
    """
    Legacy wrapper.
    Dibikin selalu mengembalikan dict (bukan None) biar caller lama tidak crash.
    """
    client = RedeemableClient(api_key)
    res = client.get_redeemables(tokens, is_enterprise)
    return res or {"status": "FAILED", "message": "Failed to fetch redeemables", "data": None}