"""Wholescripts snapshot — stores the last-seen WS data in a local JSON file.

On every sync run we:
  1. Load the previous snapshot (if any) to get "WS Prev" values.
  2. After fetching fresh WS data from the API, save the new snapshot.

The snapshot file lives at ``data/ws_snapshot.json`` inside the project
directory.  It is a dict keyed by SKU with price, stock, cost.
"""

import json
from pathlib import Path
from typing import Dict, Optional

from src.logger import setup_logger

logger = setup_logger("wholescripts_sync.ws_snapshot")

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_FILE = SNAPSHOT_DIR / "ws_snapshot.json"


def load_snapshot() -> Dict[str, dict]:
    """Load the previous WS snapshot.  Returns {} on first run."""
    if not SNAPSHOT_FILE.exists():
        logger.info("No WS snapshot found — first run, WS Prev will be empty")
        return {}
    try:
        data = json.loads(SNAPSHOT_FILE.read_text())
        logger.info("Loaded WS snapshot with %d SKUs from %s", len(data), SNAPSHOT_FILE)
        return data
    except Exception as exc:
        logger.warning("Failed to load WS snapshot: %s — treating as empty", exc)
        return {}


def save_snapshot(ws_by_sku: Dict[str, dict]) -> None:
    """Save current WS data as the snapshot for the next run."""
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = {}
        for sku, ws in ws_by_sku.items():
            snapshot[sku] = {
                "retail_price": str(ws.get("retail_price", "")),
                "qty": int(ws.get("qty") or 0),
                "cost_price": str(ws.get("cost_price", "")),
                "product_name": ws.get("product_name", ""),
            }
        SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2))
        logger.info("Saved WS snapshot with %d SKUs to %s", len(snapshot), SNAPSHOT_FILE)
    except Exception as exc:
        logger.warning("Failed to save WS snapshot: %s", exc)


def get_ws_prev(snapshot: Dict[str, dict], sku: str) -> Optional[dict]:
    """Get previous WS values for a SKU, or None if not in snapshot."""
    return snapshot.get(sku)
