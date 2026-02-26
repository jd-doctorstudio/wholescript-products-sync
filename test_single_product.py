#!/var/www/wholescripts-sync/venv/bin/python3
"""Interactive live test — sync a SINGLE product between Wholescripts and WooCommerce.

Usage:
    python3 test_single_product.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import Config
from src.woo_client import WooClient
from src.wholescripts_client import WholescriptsClient
from src.mapper import _fmt_price, ws_sku_to_short
from src.lookup import fetch_sku_lookup


# ── Pretty helpers ──────────────────────────────────────────────────

CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def banner(msg):
    print(f"\n{BOLD}{CYAN}{'─' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {msg}{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 60}{RESET}")


def step(n, msg):
    print(f"\n{BOLD}{GREEN}[Step {n}]{RESET} {msg}")


def info(msg):
    print(f"  {DIM}→{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}⚠  {msg}{RESET}")


def error(msg):
    print(f"  {RED}✗  {msg}{RESET}")


def success(msg):
    print(f"  {GREEN}✓  {msg}{RESET}")


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    val = input(f"  {BOLD}>{RESET} {prompt}{suffix}: ").strip()
    return val if val else default


def flow_arrow(left, right, label=""):
    tag = f" ({label})" if label else ""
    print(f"    {CYAN}{left}{RESET}  →  {GREEN}{right}{RESET}{tag}")


# ── Main ────────────────────────────────────────────────────────────

def main():
    banner("Wholescripts → WooCommerce  ·  Single Product Live Test")

    # ── Step 1: Product type ────────────────────────────────────────
    step(1, "What type of product are you testing?")
    print(f"    {BOLD}1{RESET} = Simple product")
    print(f"    {BOLD}2{RESET} = Variable product (has variations)")
    choice = ask("Enter 1 or 2", "1")
    is_variable = choice == "2"
    product_type = "variable" if is_variable else "simple"
    info(f"Product type: {BOLD}{product_type}{RESET}")

    # ── Step 2: Search WooCommerce ──────────────────────────────────
    step(2, "Search for the product in WooCommerce")
    search_term = ask("Enter product name (or part of it)")
    if not search_term:
        error("No search term provided. Exiting.")
        sys.exit(1)

    info(f"Searching WooCommerce for \"{search_term}\"...")
    woo = WooClient()

    params = {"search": search_term, "per_page": 20}
    if is_variable:
        params["type"] = "variable"
    else:
        params["type"] = "simple"

    resp = woo._request("GET", "/products", params=params)
    products = resp.json() if resp.status_code == 200 else []

    if not products:
        error(f"No {product_type} products found matching \"{search_term}\".")
        sys.exit(1)

    print(f"\n  {BOLD}Found {len(products)} product(s):{RESET}")
    print(f"  {'ID':<10} {'SKU':<25} {'Name'}")
    print(f"  {'─' * 10} {'─' * 25} {'─' * 40}")
    for p in products:
        pid = p["id"]
        psku = p.get("sku") or "(no sku)"
        pname = (p.get("name") or "")[:50]
        print(f"  {pid:<10} {psku:<25} {pname}")

    # ── Step 3: Pick product ────────────────────────────────────────
    step(3, "Select the product to test")
    product_id = int(ask("Enter the Product ID from above"))

    # Fetch full product details
    resp2 = woo._request("GET", f"/products/{product_id}")
    if resp2.status_code != 200:
        error(f"Could not fetch product {product_id}: HTTP {resp2.status_code}")
        sys.exit(1)
    product = resp2.json()

    target_id = product_id
    target_name = product["name"]
    target_sku = product.get("sku") or ""
    parent_id = None  # only set for variations

    # ── Step 3b: If variable, pick a variation ──────────────────────
    if is_variable:
        var_ids = product.get("variations", [])
        if not var_ids:
            error("This product has no variations.")
            sys.exit(1)

        info(f"Fetching {len(var_ids)} variation(s)...")
        variations = []
        for vid in var_ids:
            vresp = woo._request("GET", f"/products/{product_id}/variations/{vid}")
            if vresp.status_code == 200:
                variations.append(vresp.json())

        print(f"\n  {BOLD}Variations:{RESET}")
        print(f"  {'ID':<10} {'SKU':<25} {'Price':<10} {'Stock':<8} {'Attributes'}")
        print(f"  {'─' * 10} {'─' * 25} {'─' * 10} {'─' * 8} {'─' * 30}")
        for v in variations:
            vid = v["id"]
            vsku = v.get("sku") or "(no sku)"
            vprice = v.get("regular_price") or "—"
            vstock = v.get("stock_quantity") or "—"
            attrs = ", ".join(f'{a["name"]}={a["option"]}' for a in v.get("attributes", []))
            print(f"  {vid:<10} {vsku:<25} {vprice:<10} {str(vstock):<8} {attrs}")

        var_id = int(ask("Enter the Variation ID to test"))
        # Find the selected variation
        variation = next((v for v in variations if v["id"] == var_id), None)
        if not variation:
            error(f"Variation {var_id} not found.")
            sys.exit(1)

        parent_id = product_id
        target_id = var_id
        target_sku = variation.get("sku") or ""
        target_name = f"{product['name']} → variation {var_id}"

    # ── Step 4: Show current WooCommerce values ─────────────────────
    step(4, f"Current WooCommerce values for {BOLD}{target_name}{RESET}")

    if is_variable and variation:
        cur_price = variation.get("regular_price") or "0.00"
        cur_stock = variation.get("stock_quantity") or 0
        cur_cost = woo._extract_meta_value(variation.get("meta_data", []), Config.WOO_COST_META_KEY) or "0.00"
        cur_purchase = variation.get("purchase_price") or "0.00"
    else:
        cur_price = product.get("regular_price") or "0.00"
        cur_stock = product.get("stock_quantity") or 0
        cur_cost = woo._extract_meta_value(product.get("meta_data", []), Config.WOO_COST_META_KEY) or "0.00"
        cur_purchase = product.get("purchase_price") or "0.00"

    print(f"    {'Field':<20} {'Current Value'}")
    print(f"    {'─' * 20} {'─' * 20}")
    print(f"    {'regular_price':<20} {cur_price}")
    print(f"    {'stock_quantity':<20} {cur_stock}")
    print(f"    {'_op_cost_price':<20} {cur_cost}")
    print(f"    {'purchase_price':<20} {cur_purchase}")

    # Show ATUM inventories
    inventories = woo.fetch_inventories(target_id)
    if inventories:
        print(f"\n    {BOLD}ATUM Inventories:{RESET}")
        for inv in inventories:
            inv_meta = inv.get("meta_data", {})
            print(f"    • {inv['name']} (id={inv['id']}) — manage_stock={inv_meta.get('manage_stock')}, qty={inv_meta.get('stock_quantity')}")
        chosen_list = woo.select_inventories(inventories)
        inv_names = [c.get("name") for c in chosen_list]
        info(f"Will update inventory: {BOLD}{', '.join(inv_names)}{RESET}")
    else:
        info("No ATUM inventories — will update main WooCommerce stock only")

    # ── Step 5: Enter test values ───────────────────────────────────
    step(5, "Enter the test values you want to push")
    info("These simulate what Wholescripts would send.")
    info("Press Enter to keep the current value unchanged.\n")

    new_price = ask("New regular_price", cur_price)
    new_cost = ask("New cost/purchase_price", cur_cost)
    new_stock = ask("New stock_quantity", str(cur_stock))

    new_price = _fmt_price(new_price)
    new_cost = _fmt_price(new_cost)
    new_stock = int(new_stock)

    # ── Step 6: Confirm ─────────────────────────────────────────────
    step(6, "Review changes before applying")
    print(f"\n    {BOLD}Product:{RESET}  {target_name} (ID={target_id}, SKU={target_sku})")
    print(f"    {BOLD}Type:{RESET}     {product_type}")
    print()
    print(f"    {'Field':<20} {'Before':<15} {'After'}")
    print(f"    {'─' * 20} {'─' * 15} {'─' * 15}")

    price_changed = new_price != _fmt_price(cur_price)
    cost_changed = new_cost != _fmt_price(cur_cost)
    stock_changed = new_stock != int(cur_stock or 0)

    def mark(changed):
        return f" {YELLOW}← changed{RESET}" if changed else ""

    print(f"    {'regular_price':<20} {_fmt_price(cur_price):<15} {new_price}{mark(price_changed)}")
    print(f"    {'cost_price':<20} {_fmt_price(cur_cost):<15} {new_cost}{mark(cost_changed)}")
    print(f"    {'stock_quantity':<20} {str(int(cur_stock or 0)):<15} {new_stock}{mark(stock_changed)}")

    if not (price_changed or cost_changed or stock_changed):
        warn("No values changed — nothing to update.")
        sys.exit(0)

    confirm = ask("Apply these changes LIVE? (yes/no)", "no")
    if confirm.lower() not in ("yes", "y"):
        info("Cancelled. No changes applied.")
        sys.exit(0)

    # ── Step 7: Apply the update ────────────────────────────────────
    step(7, "Applying update...")

    cost_meta_key = Config.WOO_COST_META_KEY
    payload = {
        "regular_price": new_price,
        "manage_stock": True,
        "stock_quantity": new_stock,
        "purchase_price": float(new_cost),
        "meta_data": [
            {"key": cost_meta_key, "value": new_cost},
        ],
    }

    banner("SYNC FLOW")

    # Line 1: SKU match
    print(f"\n  {BOLD}1. SKU Match{RESET}")
    flow_arrow(f"Woo SKU: {target_sku or '(none)'}", f"Product ID: {target_id}", product_type)

    # Line 2: Update product/variation
    print(f"\n  {BOLD}2. WooCommerce Update{RESET}")
    if parent_id:
        status_code, resp_body = woo.update_variation(parent_id, target_id, payload)
    else:
        status_code, resp_body = woo.update_product(target_id, payload)

    if 200 <= status_code < 300:
        success(f"PUT /products/{target_id} — HTTP {status_code}")
        flow_arrow(f"price {_fmt_price(cur_price)}", new_price, "regular_price")
        flow_arrow(f"cost {_fmt_price(cur_cost)}", new_cost, "_op_cost_price + purchase_price")
        flow_arrow(f"stock {int(cur_stock or 0)}", str(new_stock), "stock_quantity")
    else:
        error(f"PUT /products/{target_id} — HTTP {status_code}")
        error(f"Response: {json.dumps(resp_body)[:200]}")
        sys.exit(1)

    # Line 3: ATUM inventory update
    print(f"\n  {BOLD}3. ATUM Inventory Update{RESET}")
    if inventories:
        chosen_list = woo.select_inventories(inventories)
        for chosen in chosen_list:
            inv_id = chosen["id"]
            inv_name = chosen.get("name", "?")
            inv_meta_before = chosen.get("meta_data", {})
            inv_status, inv_resp = woo.update_inventory(
                target_id, int(inv_id),
                stock_quantity=new_stock,
                purchase_price=float(new_cost),
            )
            if 200 <= inv_status < 300:
                success(f"PUT /products/{target_id}/inventories/{inv_id} — HTTP {inv_status}")
                flow_arrow(
                    f"manage_stock={inv_meta_before.get('manage_stock')}",
                    "manage_stock=True",
                    inv_name,
                )
                flow_arrow(
                    f"qty={inv_meta_before.get('stock_quantity')}",
                    f"qty={new_stock}",
                    inv_name,
                )
            else:
                error(f"PUT inventories/{inv_id} ({inv_name}) — HTTP {inv_status}")
    else:
        info("No ATUM inventories — main stock already updated above")

    # ── Summary ─────────────────────────────────────────────────────
    banner("RESULT")
    print(f"""
  {BOLD}Product:{RESET}  {target_name}
  {BOLD}ID:{RESET}       {target_id}
  {BOLD}SKU:{RESET}      {target_sku}
  {BOLD}Type:{RESET}     {product_type}

  {GREEN}regular_price   {_fmt_price(cur_price)}  →  {new_price}{RESET}
  {GREEN}_op_cost_price  {_fmt_price(cur_cost)}  →  {new_cost}{RESET}
  {GREEN}purchase_price  {_fmt_price(cur_purchase)}  →  {new_cost}{RESET}
  {GREEN}stock_quantity  {int(cur_stock or 0)}  →  {new_stock}{RESET}
""")

    if inventories:
        chosen_list = woo.select_inventories(inventories)
        inv_names = [c.get("name") for c in chosen_list]
        print(f"  {GREEN}ATUM inventory:  {', '.join(inv_names)}  →  manage_stock=True, qty={new_stock}{RESET}")

    success("Done! Product updated successfully.")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{DIM}Cancelled.{RESET}")
        sys.exit(0)
