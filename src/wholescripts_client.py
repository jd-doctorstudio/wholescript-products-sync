import requests
from typing import Dict, List, Optional

from src.config import Config
from src.logger import setup_logger

logger = setup_logger("wholescripts_sync.ws_client")


class WholescriptsClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (Config.WS_USERNAME, Config.WS_PASSWORD)
        self.base_url = Config.WS_API_URL.rstrip("/")

    def fetch_product_list(self) -> List[dict]:
        url = f"{self.base_url}/Orders/ProductList"
        logger.info("Fetching Wholescripts product list from %s", url)

        resp = self.session.get(url, timeout=120)
        resp.raise_for_status()

        data = resp.json()
        if not isinstance(data, list):
            raise ValueError(f"Expected list from ProductList, got {type(data).__name__}")

        logger.info("Fetched %d products from Wholescripts", len(data))
        return data

    def build_sku_map(self, products: List[dict]) -> Dict[str, dict]:
        """Build a dict keyed by normalized SKU.

        Returns:
            {sku: {retail_price, qty, cost_price, product_name}}
        """
        sku_map: Dict[str, dict] = {}
        skipped = 0

        for item in products:
            sku = (item.get("sku") or "").strip()
            if not sku:
                skipped += 1
                continue

            sku_map[sku] = {
                "retail_price": item.get("retailPrice"),
                "qty": item.get("quantity"),
                "cost_price": item.get("wholesalePrice"),
                "product_name": (item.get("productName") or "").strip(),
            }

        if skipped:
            logger.warning("Skipped %d Wholescripts items with missing/blank SKU", skipped)

        logger.info("Built Wholescripts SKU map: %d unique SKUs", len(sku_map))
        return sku_map
