#!/usr/bin/env python3
"""Advanced diagnostic: reconcile 310 sync matches vs user's confirmed 246 products."""
import subprocess, pymysql, os, signal, time, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import Config
from src.wholescripts_client import WholescriptsClient
from src.woo_client import WooClient
from src.mapper import ws_sku_to_short

Config.validate()

# ─── 1. Fetch all three data sources ───
print("=" * 80)
print("ADVANCED DIAGNOSTIC: WHERE DOES 246 COME FROM?")
print("=" * 80)

# Wholescripts production API
ws_client = WholescriptsClient()
ws_products = ws_client.fetch_product_list()
ws_by_sku = ws_client.build_sku_map(ws_products)
ws_short_skus = {ws_sku_to_short(sku): sku for sku in ws_by_sku}

# SSH tunnel for lookup table
subprocess.run([
    "ssh", "-i", Config.SSH_KEY_PATH,
    "-o", "StrictHostKeyChecking=no", "-f", "-N",
    "-L", f"{Config.SSH_LOCAL_PORT}:127.0.0.1:{Config.MYSQL_PORT}",
    f"{Config.SSH_USER}@{Config.SSH_HOST}",
], capture_output=True, timeout=15)
time.sleep(1)

conn = pymysql.connect(
    host="127.0.0.1", port=Config.SSH_LOCAL_PORT,
    user=Config.MYSQL_USER, password=Config.MYSQL_PASSWORD,
    database=Config.MYSQL_DATABASE, cursorclass=pymysql.cursors.DictCursor,
)
cur = conn.cursor()
cur.execute("SELECT id, product_id, woo_sku, supplier_sku, product FROM wholescript_supplier_sku ORDER BY id")
lookup_rows = cur.fetchall()
conn.close()

# Kill tunnel
result = subprocess.run(["fuser", f"{Config.SSH_LOCAL_PORT}/tcp"], capture_output=True, text=True)
if result.stdout.strip():
    for pid in result.stdout.strip().split():
        try: os.kill(int(pid.strip()), signal.SIGTERM)
        except: pass

# Woo products
woo_client = WooClient()
woo_products = woo_client.fetch_all_products()
woo_by_id = woo_client.build_id_map(woo_products)

# Also build variation_id set from parent products
all_variation_ids = set()
parent_of_variation = {}
for p in woo_products:
    if p.get("type") == "variable":
        for vid in p.get("variations", []):
            all_variation_ids.add(vid)
            parent_of_variation[vid] = p["id"]

# ─── 2. Classify each lookup row ───
parents_na = [r for r in lookup_rows if r["woo_sku"] == "#N/A"]
sku_rows = [r for r in lookup_rows if r["woo_sku"] != "#N/A"]

# Build parent supplier_sku set
parent_supplier_skus = {r["supplier_sku"] for r in parents_na}

# Classify sku_rows into variations vs simple
variation_rows = []
simple_rows = []
for r in sku_rows:
    is_var = any(r["supplier_sku"].startswith(psku + "-") for psku in parent_supplier_skus)
    if is_var:
        variation_rows.append(r)
    else:
        simple_rows.append(r)

print(f"\n{'─'*60}")
print(f"LOOKUP TABLE STRUCTURE (wholescript_supplier_sku)")
print(f"{'─'*60}")
print(f"Total rows:              {len(lookup_rows)}")
print(f"  #N/A parent rows:      {len(parents_na)}")
print(f"  Variation rows:        {len(variation_rows)}")
print(f"  Simple product rows:   {len(simple_rows)}")
print(f"  Unique parent-level:   {len(parents_na) + len(simple_rows)}")

# ─── 3. Check which lookup SKUs exist in Wholescripts production API ───
print(f"\n{'─'*60}")
print(f"WHOLESCRIPTS API COVERAGE")
print(f"{'─'*60}")
print(f"Production API products:  {len(ws_by_sku)}")

# Match lookup SKUs to WS API
lookup_skus_in_ws = 0
lookup_skus_not_in_ws = []
for r in sku_rows:
    short = r["woo_sku"]
    if short in ws_short_skus:
        lookup_skus_in_ws += 1
    else:
        lookup_skus_not_in_ws.append(r)

print(f"Lookup SKUs found in WS API:     {lookup_skus_in_ws} / {len(sku_rows)}")
print(f"Lookup SKUs NOT in WS API:       {len(lookup_skus_not_in_ws)}")

if lookup_skus_not_in_ws:
    print(f"\n  Missing from WS API (first 15):")
    for r in lookup_skus_not_in_ws[:15]:
        print(f"    woo_sku={r['woo_sku']}  supplier_sku={r['supplier_sku']}  product={r['product']}")

# ─── 4. Check which lookup product_ids exist in WooCommerce ───
print(f"\n{'─'*60}")
print(f"WOOCOMMERCE COVERAGE")
print(f"{'─'*60}")
print(f"Woo parent products fetched:  {len(woo_products)}")

lookup_ids_in_woo_parents = 0
lookup_ids_in_woo_variations = 0
lookup_ids_not_in_woo = []
for r in sku_rows:
    pid = r["product_id"]
    if pid in woo_by_id:
        lookup_ids_in_woo_parents += 1
    elif pid in all_variation_ids:
        lookup_ids_in_woo_variations += 1
    else:
        lookup_ids_not_in_woo.append(r)

print(f"Lookup IDs → parent products:  {lookup_ids_in_woo_parents}")
print(f"Lookup IDs → variations:       {lookup_ids_in_woo_variations}")
print(f"Lookup IDs NOT in Woo at all:  {len(lookup_ids_not_in_woo)}")

if lookup_ids_not_in_woo:
    print(f"\n  Not found in Woo (first 10):")
    for r in lookup_ids_not_in_woo[:10]:
        print(f"    woo_id={r['product_id']}  woo_sku={r['woo_sku']}  product={r['product']}")

# ─── 5. Count the actual sync matches (how we get 310) ───
print(f"\n{'─'*60}")
print(f"SYNC MATCH BREAKDOWN (how we get 310)")
print(f"{'─'*60}")

matched_via_lookup_parent = 0
matched_via_lookup_variation = 0
matched_via_direct_sku = 0
unmatched = 0

woo_sku_map = woo_client.build_sku_map(woo_products)

for ws_sku, ws_data in ws_by_sku.items():
    short = ws_sku_to_short(ws_sku)
    found = False

    # Lookup match
    for r in sku_rows:
        if r["woo_sku"] == short:
            pid = r["product_id"]
            if pid in woo_by_id:
                matched_via_lookup_parent += 1
                found = True
            elif pid in all_variation_ids:
                matched_via_lookup_variation += 1
                found = True
            break

    # Direct SKU fallback
    if not found and ws_sku in woo_sku_map:
        matched_via_direct_sku += 1
        found = True

    if not found:
        unmatched += 1

total_matched = matched_via_lookup_parent + matched_via_lookup_variation + matched_via_direct_sku
print(f"Matched via lookup → parent product:   {matched_via_lookup_parent}")
print(f"Matched via lookup → variation:        {matched_via_lookup_variation}")
print(f"Matched via direct SKU (fallback):     {matched_via_direct_sku}")
print(f"TOTAL MATCHED:                         {total_matched}")
print(f"Unmatched WS products:                 {unmatched}")

# ─── 6. The 246 question ───
print(f"\n{'─'*60}")
print(f"WHERE DOES 246 COME FROM?")
print(f"{'─'*60}")

# Count unique parent-level products that actually match
matched_parent_ids = set()
matched_parent_products = set()  # track parent-level product names

for ws_sku in ws_by_sku:
    short = ws_sku_to_short(ws_sku)
    for r in sku_rows:
        if r["woo_sku"] == short:
            pid = r["product_id"]
            # Find the parent-level product
            if pid in all_variation_ids:
                # This is a variation — count its parent
                parent_id = parent_of_variation.get(pid)
                if parent_id:
                    matched_parent_ids.add(parent_id)
            else:
                # Simple product — count itself
                matched_parent_ids.add(pid)
            break

# Also check direct SKU matches
for ws_sku in ws_by_sku:
    if ws_sku in woo_sku_map:
        woo_id = woo_sku_map[ws_sku]["id"]
        if woo_id in all_variation_ids:
            parent_id = parent_of_variation.get(woo_id)
            if parent_id:
                matched_parent_ids.add(parent_id)
        else:
            matched_parent_ids.add(woo_id)

print(f"Unique parent-level Woo products matched: {len(matched_parent_ids)}")
print(f"  (This counts variable products ONCE, not per-variation)")
print(f"")
print(f"Lookup table parent-level products:       {len(parents_na) + len(simple_rows)}")
print(f"Your confirmed count:                     246")
print(f"Difference from lookup:                   {len(parents_na) + len(simple_rows) - 246}")
print(f"Difference from matched:                  {len(matched_parent_ids) - 246}")

# ─── 7. Check for potential duplicates or stale entries ───
print(f"\n{'─'*60}")
print(f"POTENTIAL ISSUES IN LOOKUP TABLE")
print(f"{'─'*60}")

# Duplicate woo_skus
from collections import Counter
sku_counts = Counter(r["woo_sku"] for r in sku_rows)
duplicates = {sku: cnt for sku, cnt in sku_counts.items() if cnt > 1}
if duplicates:
    print(f"\nDuplicate woo_skus ({len(duplicates)}):")
    for sku, cnt in sorted(duplicates.items()):
        dups = [r for r in sku_rows if r["woo_sku"] == sku]
        print(f"  woo_sku={sku} appears {cnt}x:")
        for d in dups:
            print(f"    id={d['id']} product_id={d['product_id']} supplier_sku={d['supplier_sku']} product={d['product']}")
else:
    print("No duplicate woo_skus found.")

# Duplicate product_ids
pid_counts = Counter(r["product_id"] for r in sku_rows)
pid_dups = {pid: cnt for pid, cnt in pid_counts.items() if cnt > 1}
if pid_dups:
    print(f"\nDuplicate product_ids ({len(pid_dups)}):")
    for pid, cnt in sorted(pid_dups.items()):
        dups = [r for r in sku_rows if r["product_id"] == pid]
        print(f"  product_id={pid} appears {cnt}x:")
        for d in dups:
            print(f"    woo_sku={d['woo_sku']} supplier_sku={d['supplier_sku']} product={d['product']}")
else:
    print("No duplicate product_ids found.")

# Stale lookup entries (product_id not found anywhere in Woo)
stale = [r for r in lookup_rows if r["product_id"] not in woo_by_id and r["product_id"] not in all_variation_ids]
print(f"\nStale lookup entries (product_id not in Woo): {len(stale)}")
if stale:
    for r in stale[:10]:
        print(f"  id={r['id']} product_id={r['product_id']} woo_sku={r['woo_sku']} product={r['product']}")
    if len(stale) > 10:
        print(f"  ... and {len(stale) - 10} more")

print(f"\n{'='*80}")
