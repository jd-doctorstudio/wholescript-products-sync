#!/usr/bin/env python3
"""Check if Woo variations have Wholescripts-format SKUs we're missing."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import Config
from src.wholescripts_client import WholescriptsClient
from src.woo_client import WooClient

Config.validate()

ws_client = WholescriptsClient()
woo_client = WooClient()

# Get WS SKUs
ws_products = ws_client.fetch_product_list()
ws_by_sku = ws_client.build_sku_map(ws_products)
ws_skus = set(ws_by_sku.keys())

# Get Woo parent products
woo_products = woo_client.fetch_all_products()

# Find variable products
variable_products = [p for p in woo_products if p.get('type') == 'variable']
print(f"\nVariable products to check: {len(variable_products)}")

# Check a sample of variable products for variation SKUs
import requests
session = requests.Session()
session.auth = (Config.WOO_CONSUMER_KEY, Config.WOO_CONSUMER_SECRET)
base = Config.woo_base_url()

new_matches = 0
variation_sku_count = 0
sample_matches = []

# Check first 20 variable products that have variations
checked = 0
for p in variable_products:
    variation_ids = p.get('variations', [])
    if not variation_ids:
        continue

    checked += 1
    if checked > 30:  # Sample 30 to avoid rate limits
        break

    parent_sku = (p.get('sku') or '').strip()
    resp = session.get(f"{base}/products/{p['id']}/variations?per_page=100", timeout=60)
    if resp.status_code != 200:
        continue

    variations = resp.json()
    for v in variations:
        v_sku = (v.get('sku') or '').strip()
        if v_sku:
            variation_sku_count += 1
            if v_sku in ws_skus:
                new_matches += 1
                sample_matches.append(f"  Parent: {p.get('name','')} (SKU: {parent_sku}) → Variation SKU: {v_sku}")

    if checked % 10 == 0:
        print(f"  Checked {checked} variable products...")

print(f"\nChecked {checked} variable products")
print(f"Variation SKUs found: {variation_sku_count}")
print(f"NEW matches with Wholescripts: {new_matches}")
if sample_matches:
    print("\nSample new matches:")
    for m in sample_matches[:10]:
        print(m)
else:
    print("\nNo additional matches found in variations.")

# Also show what the non-leading-zero Woo SKUs look like
woo_by_sku = woo_client.build_sku_map(woo_products)
non_zero_skus = [s for s in woo_by_sku.keys() if not s.startswith('0')]
print(f"\n--- Sample non-Wholescripts-format Woo SKUs (first 20) ---")
for sku in sorted(non_zero_skus)[:20]:
    print(f"  '{sku}'  →  {woo_by_sku[sku].get('name','')}")
