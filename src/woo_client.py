import time
import requests
from typing import Dict, List, Optional, Tuple

from src.config import Config
from src.logger import setup_logger

logger = setup_logger("wholescripts_sync.woo_client")

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4
BACKOFF_BASE = 1  # seconds


class WooClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (Config.WOO_CONSUMER_KEY, Config.WOO_CONSUMER_SECRET)
        self.base_url = Config.woo_base_url()
        self.cost_meta_key = Config.WOO_COST_META_KEY

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", 60)

        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.request(method, url, **kwargs)

                if resp.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "Retryable %d from %s %s (attempt %d/%d), waiting %.1fs",
                        resp.status_code, method, path, attempt + 1, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue

                return resp

            except requests.exceptions.RequestException as exc:
                if attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "Request error %s %s (attempt %d/%d): %s, waiting %.1fs",
                        method, path, attempt + 1, MAX_RETRIES, exc, wait,
                    )
                    time.sleep(wait)
                    continue
                raise

        return resp  # last response

    def fetch_all_products(self) -> List[dict]:
        """Paginate through all Woo products."""
        all_products = []
        page = 1
        per_page = 100

        while True:
            logger.info("Fetching Woo products page %d", page)
            resp = self._request("GET", f"/products?per_page={per_page}&page={page}")
            resp.raise_for_status()

            products = resp.json()
            if not products:
                break

            all_products.extend(products)

            total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
            if page >= total_pages:
                break
            page += 1

        logger.info("Fetched %d total Woo products", len(all_products))
        return all_products

    def _extract_meta_value(self, meta_data: list, key: str) -> Optional[str]:
        for m in meta_data:
            if m.get("key") == key:
                return str(m.get("value", ""))
        return None

    def build_sku_map(self, products: List[dict]) -> Dict[str, dict]:
        """Build a dict keyed by SKU from Woo products.

        Returns:
            {sku: {id, regular_price, stock_quantity, cost_price, meta_data}}
        """
        sku_map: Dict[str, dict] = {}
        skipped = 0

        for p in products:
            sku = (p.get("sku") or "").strip()
            if not sku:
                skipped += 1
                continue

            meta_data = p.get("meta_data", [])
            cost_price = self._extract_meta_value(meta_data, self.cost_meta_key)

            sku_map[sku] = {
                "id": p["id"],
                "regular_price": p.get("regular_price", ""),
                "stock_quantity": p.get("stock_quantity"),
                "cost_price": cost_price,
                "name": p.get("name", ""),
            }

        if skipped:
            logger.info("Skipped %d Woo products with no SKU", skipped)

        logger.info("Built Woo SKU map: %d unique SKUs", len(sku_map))
        return sku_map

    def build_id_map(self, products: List[dict]) -> Dict[int, dict]:
        """Build a dict keyed by Woo product ID.

        Returns:
            {product_id: {id, regular_price, stock_quantity, cost_price, name, sku}}
        """
        id_map: Dict[int, dict] = {}
        for p in products:
            meta_data = p.get("meta_data", [])
            cost_price = self._extract_meta_value(meta_data, self.cost_meta_key)
            id_map[p["id"]] = {
                "id": p["id"],
                "sku": (p.get("sku") or "").strip(),
                "regular_price": p.get("regular_price", ""),
                "stock_quantity": p.get("stock_quantity"),
                "cost_price": cost_price,
                "name": p.get("name", ""),
            }
        logger.info("Built Woo ID map: %d products", len(id_map))
        return id_map

    def fetch_variations_for_lookup(
        self, products: List[dict], needed_ids: set
    ) -> List[dict]:
        """Fetch variations whose IDs are in needed_ids.

        Builds a variation_id→parent_id map from parent products' 'variations'
        arrays, then batch-fetches only the parent's variations we need.
        """
        # Map variation_id → parent_id
        var_to_parent: Dict[int, int] = {}
        for p in products:
            if p.get("type") == "variable":
                for vid in p.get("variations", []):
                    if vid in needed_ids:
                        var_to_parent[vid] = p["id"]

        if not var_to_parent:
            return []

        # Group by parent
        parents: Dict[int, List[int]] = {}
        for vid, pid in var_to_parent.items():
            parents.setdefault(pid, []).append(vid)

        logger.info(
            "Fetching variations for %d parent products (%d variation IDs needed)",
            len(parents), len(var_to_parent),
        )

        all_variations = []
        for parent_id, var_ids in parents.items():
            try:
                resp = self._request(
                    "GET",
                    f"/products/{parent_id}/variations?per_page=100",
                )
                if resp.status_code == 200:
                    variations = resp.json()
                    for v in variations:
                        if v["id"] in needed_ids:
                            all_variations.append(v)
                else:
                    logger.warning(
                        "Failed to fetch variations for parent %d: HTTP %d",
                        parent_id, resp.status_code,
                    )
            except Exception as exc:
                logger.warning("Error fetching variations for parent %d: %s", parent_id, exc)

        logger.info("Fetched %d matching variations", len(all_variations))
        return all_variations

    def update_product(self, product_id: int, payload: dict) -> Tuple[int, dict]:
        """PUT update a single product. Returns (status_code, response_json)."""
        resp = self._request("PUT", f"/products/{product_id}", json=payload)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:500]}
        return resp.status_code, body

    def update_variation(self, parent_id: int, variation_id: int, payload: dict) -> Tuple[int, dict]:
        """PUT update a single variation. Returns (status_code, response_json)."""
        resp = self._request("PUT", f"/products/{parent_id}/variations/{variation_id}", json=payload)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:500]}
        return resp.status_code, body

    # ── ATUM Multi-Inventory ──────────────────────────────────────────

    # Priority order for selecting which inventory to update
    _INVENTORY_PRIORITY = ["Dropship", "Jupiter Inventory", "Boca Inventory", "Main Inventory"]

    def fetch_inventories(self, product_id: int) -> List[dict]:
        """GET all ATUM inventories for a product (or variation).

        Works with both simple products and variations — use the
        product/variation ID directly.
        """
        resp = self._request("GET", f"/products/{product_id}/inventories")
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
        logger.debug("No inventories for product %d (HTTP %d)", product_id, resp.status_code)
        return []

    def select_inventories(self, inventories: List[dict]) -> List[dict]:
        """Pick the inventories to update based on priority.

        Priority:
          1. Dropship alone (if it exists)
          2. BOTH Jupiter Inventory AND Boca Inventory (if no Dropship)
          3. Whichever of Jupiter / Boca exists alone
          4. Main Inventory
          5. First available inventory as last resort
        """
        if not inventories:
            return []

        by_name = {inv.get("name", ""): inv for inv in inventories}

        # 1. Dropship wins outright
        if "Dropship" in by_name:
            return [by_name["Dropship"]]

        # 2/3. Collect Jupiter + Boca (could be both or just one)
        jb = [by_name[n] for n in ("Jupiter Inventory", "Boca Inventory") if n in by_name]
        if jb:
            return jb

        # 4. Main Inventory
        if "Main Inventory" in by_name:
            return [by_name["Main Inventory"]]

        # 5. Fallback
        return [inventories[0]]

    def update_inventory(
        self, product_id: int, inventory_id: int, stock_quantity: int, purchase_price: Optional[float] = None
    ) -> Tuple[int, dict]:
        """PUT update an ATUM inventory — enable stock management and set quantity.

        Endpoint: /products/<product_id>/inventories/<inventory_id>
        """
        meta: dict = {
            "manage_stock": True,
            "stock_quantity": stock_quantity,
            "stock_status": "instock" if stock_quantity > 0 else "outofstock",
        }
        if purchase_price is not None:
            meta["purchase_price"] = purchase_price

        payload = {"meta_data": meta}
        resp = self._request("PUT", f"/products/{product_id}/inventories/{inventory_id}", json=payload)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:500]}
        return resp.status_code, body
