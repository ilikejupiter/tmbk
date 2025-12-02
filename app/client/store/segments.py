from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

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


class SegmentsClient:
    """
    Client untuk mengambil konfigurasi Store Segments (Kategori Paket).
    Versi ini lebih tahan perubahan struktur karena traversal-nya recursive.
    """

    # beberapa API suka pakai key anak berbeda-beda
    _CHILD_KEYS: Tuple[str, ...] = ("children", "items", "segments", "menus", "submenus", "sub_segments")

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
            return res
        except Exception as e:
            logger.exception("Error executing %s", path)
            return {"status": "FAILED", "message": str(e), "data": None}

    def get_segments(self, tokens: TokenDict, is_enterprise: bool = False) -> Optional[ApiResponse]:
        """
        Ambil raw response segments (menu structure).
        """
        response = self._send_request(
            path="api/v8/configs/store/segments",
            payload={"is_enterprise": is_enterprise},
            id_token=tokens.get("id_token", ""),
            description="📊 Fetching Store Segments...",
        )

        if _status_is_success(response):
            data = response.get("data", [])
            count = len(data) if isinstance(data, list) else 0
            logger.info("✅ Retrieved %s segment groups.", count)
            return response

        logger.error("❌ Failed to fetch segments. Status=%s", response.get("status"))
        return None

    def get_segment_slugs(self, tokens: TokenDict, *, include_duplicates: bool = False) -> List[Dict[str, str]]:
        """
        Mengambil daftar 'slug' dan 'label' dari struktur segments, termasuk nested.

        Return format tetap simple dan backward-friendly:
            [{'label': 'Paket Utama', 'slug': 'main-package'}, ...]
        """
        raw = self.get_segments(tokens)
        if not raw or "data" not in raw:
            return []

        root = raw.get("data")
        nodes: List[Dict[str, Any]] = []
        if isinstance(root, list):
            for x in root:
                if isinstance(x, dict):
                    nodes.append(x)
        elif isinstance(root, dict):
            nodes.append(root)
        else:
            return []

        results: List[Dict[str, str]] = []
        seen: set = set()

        def walk(node: Dict[str, Any]) -> None:
            slug = node.get("slug")
            label = node.get("label") or node.get("name") or node.get("title") or ""
            if slug:
                key = str(slug)
                if include_duplicates or key not in seen:
                    results.append({"label": str(label) if label else key, "slug": key})
                    seen.add(key)

            # traverse children
            for ck in self._CHILD_KEYS:
                child = node.get(ck)
                if isinstance(child, list):
                    for c in child:
                        if isinstance(c, dict):
                            walk(c)
                elif isinstance(child, dict):
                    walk(child)

        for n in nodes:
            walk(n)

        if results:
            logger.info("📑 Available Segments (total=%s):", len(results))
            for item in results[:50]:  # guard: jangan spam log
                logger.info("   - %-28s (Slug: %s)", item.get("label", ""), item.get("slug", ""))
            if len(results) > 50:
                logger.info("   ... and %s more", len(results) - 50)

        return results


# =============================================================================
# COMPATIBILITY LAYER (Legacy Support)
# =============================================================================

def get_segments(
    api_key: str,
    tokens: dict,
    is_enterprise: bool = False,
    logger: Optional[Any] = None,  # diterima untuk backward compat
) -> Optional[Dict[str, Any]]:
    _ = logger
    return SegmentsClient(api_key).get_segments(tokens, is_enterprise)


def get_available_slugs(api_key: str, tokens: dict) -> List[Dict[str, str]]:
    return SegmentsClient(api_key).get_segment_slugs(tokens)