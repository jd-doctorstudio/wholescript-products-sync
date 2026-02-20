from typing import Dict, List, Tuple, Optional

from src.config import Config
from src.logger import setup_logger

logger = setup_logger("wholescripts_sync.mapper")


def _fmt_price(value) -> str:
    """Format a numeric value as a 2-decimal string for Woo."""
    if value is None:
        return "0.00"
    return f"{float(value):.2f}"


def _prices_equal(a: Optional[str], b: Optional[str]) -> bool:
    """Compare two price strings numerically (2-decimal precision)."""
    try:
        return f"{float(a or 0):.2f}" == f"{float(b or 0):.2f}"
    except (ValueError, TypeError):
        return str(a) == str(b)


def _stock_equal(a: Optional[int], b: Optional[int]) -> bool:
    """Compare two stock quantities."""
    return int(a or 0) == int(b or 0)


def ws_sku_to_short(ws_sku: str) -> str:
    """Convert Wholescripts long SKU to the 9-digit short format used in the lookup table.

    Example: '000000000300104424' -> '300104424'
    """
    return ws_sku.lstrip("0") or ws_sku


def compute_updates(
    ws_by_sku: Dict[str, dict],
    woo_by_id: Dict[int, dict],
    sku_lookup: Dict[str, int],
    woo_by_sku: Optional[Dict[str, dict]] = None,
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Match Wholescripts SKUs to Woo products using:
      1) Lookup table (short SKU -> Woo product ID)  [primary]
      2) Direct SKU match against Woo SKU map         [fallback]

    Returns:
        (updates, skipped, missing_in_woo)
    """
    updates = []
    skipped = []
    missing_in_woo = []

    cost_meta_key = Config.WOO_COST_META_KEY
    atum_meta_key = Config.ATUM_PURCHASE_PRICE_META_KEY

    matched_via_lookup = 0
    matched_via_direct = 0

    for sku, ws in ws_by_sku.items():
        ws_retail = _fmt_price(ws["retail_price"])
        ws_qty = int(ws.get("qty") or 0)
        ws_cost = _fmt_price(ws["cost_price"])

        # --- Resolve Woo product ---
        woo = None

        # Strategy 1: Lookup table (short SKU -> product_id -> Woo product)
        short_sku = ws_sku_to_short(sku)
        if short_sku in sku_lookup:
            woo_id = sku_lookup[short_sku]
            if woo_id in woo_by_id:
                woo = woo_by_id[woo_id]
                matched_via_lookup += 1

        # Strategy 2: Direct SKU match (fallback)
        if woo is None and woo_by_sku and sku in woo_by_sku:
            woo = woo_by_sku[sku]
            matched_via_direct += 1

        if woo is None:
            missing_in_woo.append({"sku": sku, "ws_name": ws.get("product_name", "")})
            continue

        woo_id = woo["id"]

        woo_retail = woo.get("regular_price", "") or "0.00"
        woo_qty = woo.get("stock_quantity") or 0
        woo_cost = woo.get("cost_price") or "0.00"

        price_same = _prices_equal(woo_retail, ws_retail)
        stock_same = _stock_equal(woo_qty, ws_qty)
        cost_same = _prices_equal(woo_cost, ws_cost)

        if price_same and stock_same and cost_same:
            skipped.append({
                "sku": sku,
                "woo_product_id": woo_id,
                "reason": "no_change",
            })
            continue

        payload = {
            "regular_price": ws_retail,
            "manage_stock": True,
            "stock_quantity": ws_qty,
            "meta_data": [
                {"key": cost_meta_key, "value": ws_cost},
                {"key": atum_meta_key, "value": ws_cost},
            ],
        }

        updates.append({
            "sku": sku,
            "woo_product_id": woo_id,
            "payload": payload,
            "prev": {
                "regular_price": woo_retail,
                "stock_quantity": int(woo_qty),
                "cost_price": woo_cost,
            },
            "new_vals": {
                "regular_price": ws_retail,
                "stock_quantity": ws_qty,
                "cost_price": ws_cost,
            },
            "ws_name": ws.get("product_name", ""),
        })

    logger.info(
        "Mapping complete: %d updates, %d skipped (no change), %d missing in Woo",
        len(updates), len(skipped), len(missing_in_woo),
    )
    logger.info(
        "Match sources: %d via lookup table, %d via direct SKU",
        matched_via_lookup, matched_via_direct,
    )
    return updates, skipped, missing_in_woo
