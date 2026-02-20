# Wholescripts → WooCommerce Nightly Sync — How It Works

## What This Script Does

Every night at **12:00 AM Eastern Time**, this script automatically updates ~246 Wholescripts products on `store.doctorsstudio.com` with the latest **prices**, **stock quantities**, and **cost prices** from the Wholescripts supplier API.

---

## The Workflow (Step by Step)

### Step 1 — Cron Triggers the Script

A cron job runs the script every night at midnight Eastern Time (automatically adjusts for daylight saving):

```
TZ=America/New_York
0 0 * * * /var/www/wholescripts-sync/venv/bin/python3 /var/www/wholescripts-sync/updatescript.py
```

Output is appended to `/var/log/wholescripts_sync.log`.

---

### Step 2 — Get Products from Wholescripts API

The script calls the Wholescripts production API:

```
GET https://api.wholescripts.com/api/Orders/ProductList
```

This returns ~1,059 products with their current:
- **RetailPrice** (what customers pay)
- **Quantity** (stock available)
- **WholesalePrice** (our cost price)
- **SKU** (unique product identifier, e.g. `000000000300104424`)

---

### Step 3 — Get the SKU Lookup Table

The script connects to the remote MySQL database (via SSH tunnel) and reads the `wholescript_supplier_sku` table. This table maps Wholescripts short SKUs to WooCommerce product IDs.

**Why we need this:** Wholescripts SKUs have leading zeros (e.g. `000000000300104424`) and WooCommerce stores them differently (e.g. `300104424`). The lookup table bridges this gap. It also tells us which WooCommerce product ID each SKU belongs to — including **variation IDs** (like different capsule counts of the same product).

The lookup table has:
- **252 parent-level products** (171 simple + 81 variable)
- **323 individual SKU mappings** (simple products + variations)

---

### Step 4 — Get Products from WooCommerce

The script fetches all products from the WooCommerce REST API:

```
GET https://store.doctorsstudio.com/wp-json/wc/v3/products?per_page=100&page=1,2,3...
```

This returns ~1,284 products (all products in the store, not just Wholescripts ones).

For **variable products** (products with size/count options like "60 Capsules" vs "120 Capsules"), the script also fetches their individual variations because each variation has its own price, stock, and cost.

---

### Step 5 — Match Wholescripts Products to WooCommerce Products

The script uses **two matching strategies**:

1. **Lookup table match (primary):** Strip leading zeros from the Wholescripts SKU → look up the WooCommerce product ID in the mapping table
2. **Direct SKU match (fallback):** If not in the lookup table, try to match the full SKU directly against WooCommerce product SKUs

This finds ~310 SKU-level matches (which represent ~234 unique parent-level products).

---

### Step 6 — Compare and Detect Changes

For each matched product, the script compares:

| Wholescripts Field | WooCommerce Field | What It Is |
|---|---|---|
| `RetailPrice` | `regular_price` | The price shown to customers |
| `Quantity` | `stock_quantity` | How many are in stock |
| `WholesalePrice` | `_op_cost_price` (meta) | Our cost/purchase price |

If **nothing changed** → skip (don't make unnecessary API calls).
If **any value differs** → prepare an update.

---

### Step 7 — Update WooCommerce Products

For each product that needs updating, the script sends:

**For simple products:**
```
PUT /wp-json/wc/v3/products/<product_id>
```

**For variations (different sizes of the same product):**
```
PUT /wp-json/wc/v3/products/<parent_id>/variations/<variation_id>
```

The update payload looks like:
```json
{
  "regular_price": "80.99",
  "manage_stock": true,
  "stock_quantity": 1159,
  "meta_data": [
    { "key": "_op_cost_price", "value": "43.99" }
  ]
}
```

If WooCommerce returns an error (500, 429, etc.), the script retries up to 3 times with increasing wait times.

---

### Step 8 — Log Everything to the Database

Every single action is logged to two Postgres tables in the `pos_prod` database:

**Table: `wholescripts_woo_sync_runs`** — One row per nightly run:
- Run ID, start/end time
- Total products from Wholescripts and WooCommerce
- How many matched, updated, skipped, missing, or failed

**Table: `wholescripts_woo_sync_log`** — One row per product per run:
- SKU and WooCommerce product ID
- Status: `success`, `failed`, `skipped_no_change`, `missing_in_woo`
- The full request body sent to WooCommerce
- The full response body from WooCommerce
- Previous retail price, stock quantity, and cost price
- New retail price, stock quantity, and cost price
- Error message (if any)
- Timestamp

---

## Quick Reference

| Item | Value |
|---|---|
| **Script location** | `/var/www/wholescripts-sync/updatescript.py` |
| **Schedule** | Every day at 12:00 AM Eastern (DST-safe) |
| **Log file** | `/var/log/wholescripts_sync.log` |
| **Database** | `pos_prod` on `127.0.0.1:5432` |
| **Log tables** | `wholescripts_woo_sync_runs`, `wholescripts_woo_sync_log` |
| **Wholescripts API** | `https://api.wholescripts.com/api/Orders/ProductList` |
| **WooCommerce API** | `https://store.doctorsstudio.com/wp-json/wc/v3/products` |
| **SKU lookup DB** | MySQL on `34.148.82.199` → `doctorsstudio.wholescript_supplier_sku` |
| **Environment file** | `/var/www/wholescripts-sync/.env` |

---

## How to Run Manually

**Dry run** (see what would change, no actual updates):
```bash
/var/www/wholescripts-sync/venv/bin/python3 /var/www/wholescripts-sync/updatescript.py --dry-run
```

**Real run** (apply updates to the store):
```bash
/var/www/wholescripts-sync/venv/bin/python3 /var/www/wholescripts-sync/updatescript.py
```

---

## How to Check Logs

**Check latest run summary:**
```sql
SELECT * FROM wholescripts_woo_sync_runs ORDER BY started_at DESC LIMIT 5;
```

**Check what changed for a specific run:**
```sql
SELECT sku, woo_product_id, status, prev_regular_price, new_regular_price,
       prev_stock_quantity, new_stock_quantity, prev_cost_price, new_cost_price
FROM wholescripts_woo_sync_log
WHERE run_id = '<run_id>' AND status = 'success'
ORDER BY created_at;
```

**Check failures:**
```sql
SELECT sku, woo_product_id, error
FROM wholescripts_woo_sync_log
WHERE run_id = '<run_id>' AND status = 'failed';
```

---

## File Structure

```
/var/www/wholescripts-sync/
├── updatescript.py          # Entry point (called by cron)
├── .env                     # API credentials and config
├── requirements.txt         # Python dependencies
├── src/
│   ├── config.py            # Loads environment variables
│   ├── logger.py            # Logging setup
│   ├── sync.py              # Main orchestration (the workflow above)
│   ├── wholescripts_client.py  # Talks to Wholescripts API
│   ├── woo_client.py        # Talks to WooCommerce REST API
│   ├── mapper.py            # SKU matching + change detection
│   ├── lookup.py            # SSH tunnel + MySQL lookup table
│   └── db.py                # Postgres logging
└── venv/                    # Python virtual environment
```

---

## Numbers at a Glance (as of Feb 20, 2026)

- **Wholescripts products in API:** ~1,059
- **WooCommerce products in store:** ~1,284
- **Wholescripts products in our store:** ~246 (parent-level)
- **SKU-level matches per run:** ~310 (variations counted individually)
- **Typical updates per run:** ~300 (price, stock, or cost changed)
- **Run time:** ~6 minutes
