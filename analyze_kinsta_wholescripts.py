#!/usr/bin/env python3
"""Analyze Wholescripts ↔ WooCommerce product overlap using NAME matching.

SKUs differ between the two systems, so matching is done by normalized
product name.  The script auto-detects which environment is active based
on the URLs configured in .env:

  Wholescripts  test → testservices.wholescripts.com
  Wholescripts  prod → api.wholescripts.com
  WooCommerce   prod → store.doctorsstudio.com
  WooCommerce   stag → dsstore.kinsta.cloud

Usage:
    python3 analyze_kinsta_wholescripts.py
"""

import os
import re
import sys
import time
import shutil
import logging
import requests
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import Config
from src.wholescripts_client import WholescriptsClient

# Suppress library noise
logging.disable(logging.WARNING)

# ── ANSI helpers ───────────────────────────────────────────────────────
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"

COLS = shutil.get_terminal_size((100, 24)).columns


def _detect_env():
    """Return human-readable labels for current WooCommerce + Wholescripts endpoints."""
    woo_url = Config.WOO_API_URL or ""
    ws_url = Config.WS_API_URL or ""

    if "kinsta.cloud" in woo_url:
        woo_label = "Kinsta staging"
    elif "doctorsstudio.com" in woo_url:
        woo_label = "Production"
    else:
        woo_label = woo_url

    if "testservices" in ws_url:
        ws_label = "Test"
    elif "api.wholescripts.com" in ws_url:
        ws_label = "Production"
    else:
        ws_label = ws_url

    return woo_label, ws_label


def banner(woo_label: str, ws_label: str):
    art = f"""
{CYAN}{BOLD}╔{'═' * (COLS - 2)}╗
║  WooCommerce ↔ Wholescripts  —  Name-Match Analyzer{' ' * max(0, COLS - 56)}║
╚{'═' * (COLS - 2)}╝{RESET}
"""
    print(art)
    print(f"  {BOLD}WooCommerce:{RESET}  {woo_label}  ({Config.WOO_API_URL})")
    print(f"  {BOLD}Wholescripts:{RESET} {ws_label}  ({Config.WS_API_URL})")
    print()


def section(title: str):
    print(f"\n{BOLD}{MAGENTA}{'─' * 3} {title} {'─' * max(0, COLS - len(title) - 6)}{RESET}")


def ok(msg: str):
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str):
    print(f"  {YELLOW}⚠{RESET} {msg}")


def fail(msg: str):
    print(f"  {RED}✗{RESET} {msg}")


def info(msg: str):
    print(f"  {DIM}→{RESET} {msg}")


# ── Name normalization ─────────────────────────────────────────────────
def normalize(name: str) -> str:
    """Lowercase, strip trademarks, collapse whitespace."""
    if not name:
        return ""
    n = name.lower().strip()
    n = n.replace("\u2122", "").replace("\u00ae", "").replace("\u00a9", "")
    n = re.sub(r"&[a-z]+;", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# ── Lightweight WooCommerce REST client ────────────────────────────────
RETRYABLE = {429, 500, 502, 503, 504}
MAX_RETRIES = 4


def _woo_get(session, base_url: str, path: str, params: dict = None) -> requests.Response:
    url = f"{base_url}{path}"
    resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, params=params, timeout=60)
            if resp.status_code in RETRYABLE and attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            return resp
        except requests.exceptions.RequestException:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    return resp


def fetch_woo_products(session, base_url: str) -> List[dict]:
    """Fetch all WooCommerce products (paginated)."""
    all_products = []
    page = 1
    while True:
        print(f"\r  {DIM}→ Fetching products page {page}...{RESET}", end="", flush=True)
        resp = _woo_get(session, base_url, "/products", {"per_page": 100, "page": page})
        if resp.status_code != 200:
            print()
            fail(f"HTTP {resp.status_code} on page {page}: {resp.text[:200]}")
            break
        batch = resp.json()
        if not batch:
            break
        all_products.extend(batch)
        total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break
        page += 1
    print(f"\r{' ' * COLS}\r", end="")
    return all_products


def fetch_woo_variations(session, base_url: str, parent_id: int) -> List[dict]:
    """Fetch all variations for a parent product."""
    all_vars = []
    page = 1
    while True:
        resp = _woo_get(session, base_url, f"/products/{parent_id}/variations", {"per_page": 100, "page": page})
        if resp.status_code != 200:
            break
        variations = resp.json()
        if not variations:
            break
        all_vars.extend(variations)
        total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break
        page += 1
    return all_vars


# ── Build name maps ────────────────────────────────────────────────────

def build_woo_name_map(session, base_url: str) -> Tuple[Dict[str, list], int]:
    """Fetch all WooCommerce products + variations, build {normalized_name: [items]}."""
    section("Fetching WooCommerce Products")
    products = fetch_woo_products(session, base_url)
    ok(f"Fetched {len(products)} top-level products")

    items = []
    variable_parents = []
    for p in products:
        ptype = p.get("type", "simple")
        item = {
            "id": p["id"],
            "sku": (p.get("sku") or "").strip(),
            "name": (p.get("name") or "").strip(),
            "type": ptype,
            "price": p.get("regular_price", ""),
            "stock": p.get("stock_quantity"),
            "status": p.get("status", ""),
        }
        items.append(item)
        if ptype == "variable":
            variable_parents.append(p)

    if variable_parents:
        info(f"Fetching variations for {len(variable_parents)} variable products...")
        for i, parent in enumerate(variable_parents, 1):
            print(f"\r  {DIM}→ Variations {i}/{len(variable_parents)} "
                  f"({parent.get('name', '')[:35]})...{RESET}", end="", flush=True)
            variations = fetch_woo_variations(session, base_url, parent["id"])
            for v in variations:
                attrs = ", ".join(a.get("option", "") for a in v.get("attributes", []))
                items.append({
                    "id": v["id"],
                    "sku": (v.get("sku") or "").strip(),
                    "name": f"{parent.get('name', '')} - {attrs}".strip(" -"),
                    "type": "variation",
                    "price": v.get("regular_price", ""),
                    "stock": v.get("stock_quantity"),
                    "status": v.get("status", ""),
                    "parent_id": parent["id"],
                })
        print(f"\r{' ' * COLS}\r", end="")

    ok(f"Total WooCommerce items (incl. variations): {BOLD}{len(items)}{RESET}")

    name_map: Dict[str, list] = {}
    for item in items:
        norm = normalize(item["name"])
        if norm:
            name_map.setdefault(norm, []).append(item)

    ok(f"Unique normalized names: {len(name_map)}")
    return name_map, len(items)


def build_ws_name_map() -> Tuple[Dict[str, dict], int]:
    """Fetch Wholescripts catalog and build {normalized_name: item}."""
    section("Fetching Wholescripts Catalog")
    ws = WholescriptsClient()
    products = ws.fetch_product_list()
    ok(f"Fetched {len(products)} Wholescripts products")

    name_map: Dict[str, dict] = {}
    for p in products:
        name = (p.get("productName") or "").strip()
        sku = (p.get("sku") or "").strip()
        norm = normalize(name)
        if norm:
            name_map[norm] = {
                "sku": sku,
                "name": name,
                "retail_price": p.get("retailPrice"),
                "cost_price": p.get("wholesalePrice"),
                "qty": p.get("quantity"),
            }

    ok(f"Unique normalized names: {len(name_map)}")
    return name_map, len(products)


# ── Cross-reference by name ────────────────────────────────────────────

def analyze(woo_names: Dict[str, list], ws_names: Dict[str, dict]):
    section("Name-Match Cross-Reference")

    matched = []   # same name found in both
    woo_only = []  # in WooCommerce but not Wholescripts
    ws_only = []   # in Wholescripts but not WooCommerce

    for norm, woo_items in woo_names.items():
        if norm in ws_names:
            ws = ws_names[norm]
            for wi in woo_items:
                matched.append({
                    "name": wi["name"],
                    "woo_sku": wi["sku"],
                    "woo_id": wi["id"],
                    "woo_type": wi["type"],
                    "woo_price": wi["price"],
                    "woo_stock": wi["stock"],
                    "ws_sku": ws["sku"],
                    "ws_name": ws["name"],
                    "ws_price": ws["retail_price"],
                    "ws_cost": ws["cost_price"],
                    "ws_qty": ws["qty"],
                })
        else:
            for wi in woo_items:
                woo_only.append(wi)

    for norm, ws in ws_names.items():
        if norm not in woo_names:
            ws_only.append(ws)

    # ── Summary ──
    print()
    print(f"  {BOLD}Name matches:{RESET}            {GREEN}{len(matched):>6}{RESET}")
    print(f"  {BOLD}WooCommerce only:{RESET}        {CYAN}{len(woo_only):>6}{RESET}")
    print(f"  {BOLD}Wholescripts only:{RESET}       {YELLOW}{len(ws_only):>6}{RESET}")

    # ── Matched table ──
    if matched:
        section(f"Matched by Name ({len(matched)} — showing first 40)")
        print()
        hdr = f"  {BOLD}{'Product Name':<50} {'Woo SKU':<18} {'WS SKU':<22} {'Woo $':>8} {'WS $':>8}{RESET}"
        print(hdr)
        print(f"  {'─' * 50} {'─' * 18} {'─' * 22} {'─' * 8} {'─' * 8}")
        for i, m in enumerate(sorted(matched, key=lambda x: x["name"])):
            if i >= 40:
                print(f"  {DIM}... and {len(matched) - 40} more{RESET}")
                break
            name = m["name"][:50]
            woo_price = m["woo_price"] or "—"
            ws_price = str(m["ws_price"] or "—")
            print(f"  {name:<50} {m['woo_sku']:<18} {m['ws_sku']:<22} {woo_price:>8} {ws_price:>8}")

    # ── WooCommerce-only (first 20) ──
    if woo_only:
        section(f"WooCommerce Only — NOT in Wholescripts ({len(woo_only)} — first 20)")
        print()
        for i, w in enumerate(sorted(woo_only, key=lambda x: x["name"])[:20]):
            print(f"  {w['name'][:60]:<60} SKU={w['sku'] or '—'}")
        if len(woo_only) > 20:
            print(f"  {DIM}... and {len(woo_only) - 20} more{RESET}")

    # ── Wholescripts-only (first 20) ──
    if ws_only:
        section(f"Wholescripts Only — NOT in WooCommerce ({len(ws_only)} — first 20)")
        print()
        for i, w in enumerate(sorted(ws_only, key=lambda x: x["name"])[:20]):
            print(f"  {w['name'][:60]:<60} SKU={w['sku'] or '—'}")
        if len(ws_only) > 20:
            print(f"  {DIM}... and {len(ws_only) - 20} more{RESET}")

    return matched, woo_only, ws_only


def main():
    woo_label, ws_label = _detect_env()
    banner(woo_label, ws_label)

    if not Config.WOO_API_URL or not Config.WOO_CONSUMER_KEY:
        fail("WOO_API_URL / WOO_CONSUMER_KEY not configured in .env")
        sys.exit(1)

    # Prepare WooCommerce session
    session = requests.Session()
    session.auth = (Config.WOO_CONSUMER_KEY, Config.WOO_CONSUMER_SECRET)
    base_url = f"{Config.WOO_API_URL.rstrip('/')}/wp-json/{Config.WOO_API_VERSION}"

    woo_names, woo_total = build_woo_name_map(session, base_url)
    ws_names, ws_total = build_ws_name_map()

    matched, woo_only, ws_only = analyze(woo_names, ws_names)

    section("Done")
    print()


if __name__ == "__main__":
    main()
