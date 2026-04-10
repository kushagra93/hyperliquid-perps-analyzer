import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.settings import HL_PERP_DEX

logger = logging.getLogger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


def _build_session() -> requests.Session:
    """
    Build a shared HTTP session for Hyperliquid calls.
    - trust_env=False prevents accidental proxy hijacking from shell env.
    - Retry handles transient transport failures at scale.
    """
    session = requests.Session()
    session.trust_env = False

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_SESSION = _build_session()


def fetch_meta_and_asset_ctxs() -> list | None:
    """
    Fetch Hyperliquid [meta, assetCtxs] payload.
    Returns None on failure.
    """
    try:
        payload = {"type": "metaAndAssetCtxs"}
        if HL_PERP_DEX:
            payload["dex"] = HL_PERP_DEX

        resp = _SESSION.post(HL_INFO_URL, json=payload, timeout=10, verify=False)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Error fetching Hyperliquid metaAndAssetCtxs: {e}")
        return None
