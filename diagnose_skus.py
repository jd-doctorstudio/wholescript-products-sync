#!/usr/bin/env python3
"""Quick SKU mismatch diagnosis."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import Config
from src.wholescripts_client import WholescriptsClient
from src.woo_client import WooClient

Config.validate()

ws_client = WholescriptsClient()
woo_client = WooClient()

# Fetch both sides
ws_products = ws_client.fetch_product_list()
ws_by_sku = ws_client.build_sku_map(ws_products)

woo_products = woo_client.fetch_all_products()
woo_by_sku = woo_client.build_sku_map(woo_products)

# --- Diagnosis ---
print("\n" + "="*80)
print("SKU MISMATCH DIAGNOSIS")
print("="*80)

print(f"\nWholescripts SKU count: {len(ws_by_sku)}")
print(f"WooCommerce SKU count:  {len(woo_by_sku)}")

# Empty SKU count
empty_sku_woo = sum(1 for p in woo_products if not (p.get('sku') or '').strip())
print(f"Woo products with EMPTY SKU: {empty_sku_woo} / {len(woo_products)}")

# Variable products (SKU might be on variations)
variable_count = sum(1 for p in woo_products if p.get('type') == 'variable')
simple_count = sum(1 for p in woo_products if p.get('type') == 'simple')
other_types = {}
for p in woo_products:
    t = p.get('type', 'unknown')
    other_types[t] = other_types.get(t, 0) + 1
print(f"\nWoo product types: {other_types}")
print(f"Variable products (may have SKU on variations): {variable_count}")

# Unmatched sets
ws_skus = set(ws_by_sku.keys())
woo_skus = set(woo_by_sku.keys())
matched = ws_skus & woo_skus
ws_only = ws_skus - woo_skus
woo_only = woo_skus - ws_skus

print(f"\nMatched SKUs: {len(matched)}")
print(f"WS only (not in Woo): {len(ws_only)}")
print(f"Woo only (not in WS): {len(woo_only)}")

# Sample unmatched from each side
print("\n--- Sample WS SKUs NOT in Woo (first 15) ---")
for sku in sorted(ws_only)[:15]:
    print(f"  WS: '{sku}'  (name: {ws_by_sku[sku].get('product_name','')})")

print("\n--- Sample Woo SKUs NOT in WS (first 15) ---")
for sku in sorted(woo_only)[:15]:
    print(f"  Woo: '{sku}'  (name: {woo_by_sku[sku].get('name','')})")

print("\n--- Sample MATCHED SKUs (first 10) ---")
for sku in sorted(matched)[:10]:
    print(f"  '{sku}'")

# Check leading-zero pattern
print("\n--- Leading zero analysis ---")
ws_leading_zero = sum(1 for s in ws_skus if s.startswith('0'))
woo_leading_zero = sum(1 for s in woo_skus if s.startswith('0'))
print(f"WS SKUs starting with '0': {ws_leading_zero}/{len(ws_skus)}")
print(f"Woo SKUs starting with '0': {woo_leading_zero}/{len(woo_skus)}")

# Try stripping leading zeros and re-matching
ws_stripped = {s.lstrip('0'): s for s in ws_skus}
woo_stripped = {s.lstrip('0'): s for s in woo_skus}
stripped_matched = set(ws_stripped.keys()) & set(woo_stripped.keys())
print(f"\nIf we strip leading zeros: {len(stripped_matched)} matches (was {len(matched)})")

# Try case-insensitive
ws_lower = {s.lower(): s for s in ws_skus}
woo_lower = {s.lower(): s for s in woo_skus}
lower_matched = set(ws_lower.keys()) & set(woo_lower.keys())
print(f"If we lowercase: {len(lower_matched)} matches (was {len(matched)})")

# Check if variable products have variations with SKUs
print("\n--- Variable product variation check ---")
variable_with_sku = [p for p in woo_products if p.get('type') == 'variable' and (p.get('sku') or '').strip()]
variable_without_sku = [p for p in woo_products if p.get('type') == 'variable' and not (p.get('sku') or '').strip()]
print(f"Variable products WITH parent SKU: {len(variable_with_sku)}")
print(f"Variable products WITHOUT parent SKU: {len(variable_without_sku)}")
if variable_without_sku:
    print(f"  (These may have SKUs on their variations — not currently fetched)")
    for p in variable_without_sku[:5]:
        print(f"    id={p['id']} name='{p.get('name','')}'  variations={p.get('variations', [])[:3]}")

print("\n" + "="*80)
