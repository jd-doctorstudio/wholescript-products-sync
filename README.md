# Wholescripts → WooCommerce Nightly Product Sync

---

## Table of Contents

1. [What Is This?](#what-is-this)
2. [Why Does This Exist?](#why-does-this-exist)
3. [The Big Picture](#the-big-picture)
4. [How It Works (Step by Step)](#how-it-works-step-by-step)
5. [The SKU Matching Problem (and How We Solved It)](#the-sku-matching-problem-and-how-we-solved-it)
6. [Project Structure (Every File Explained)](#project-structure-every-file-explained)
7. [How the Files Connect](#how-the-files-connect)
8. [Setup Guide](#setup-guide)
9. [How to Run](#how-to-run)
10. [The Cron Job (Automatic Nightly Runs)](#the-cron-job-automatic-nightly-runs)
11. [Database Logging (How We Track Everything)](#database-logging-how-we-track-everything)
12. [How to Check Logs](#how-to-check-logs)
13. [Diagnostic Tools](#diagnostic-tools)
14. [Switching Environments](#switching-environments)
15. [Environment Variables](#environment-variables)
16. [Troubleshooting](#troubleshooting)
17. [Numbers at a Glance](#numbers-at-a-glance)

---

## What Is This?

This is a Python script that runs **every night at midnight Eastern Time**. It talks to two systems:

1. **Wholescripts** — our supplement supplier. They have an API that tells us the current price, stock, and cost of every product they sell.
2. **WooCommerce** — our online store at `store.doctorsstudio.com`. This is where customers buy products.

The script pulls the latest data from Wholescripts and updates our WooCommerce store so that prices, stock levels, and cost prices are always up to date.

---

## Why Does This Exist?

Without this script, someone would have to **manually** check Wholescripts for price or stock changes and then go into WooCommerce and update each product by hand. We sell ~246 Wholescripts products, many with multiple size options (like "60 Capsules" and "120 Capsules"). That is hundreds of items to check every day.

This script does all of that automatically, every night, in about 6 minutes.

**What exactly gets updated:**

| Wholescripts Field | WooCommerce Field | Where It Lives | What It Means |
|---|---|---|---|
| `RetailPrice` | `regular_price` | `wp_posts` (standard WooCommerce) | The price the customer sees and pays |
| `Quantity` | `stock_quantity` | `wp_posts` (standard WooCommerce) | How many units are available to sell |
| `WholesalePrice` | `_op_cost_price` | `wp_postmeta` (meta key) | Our cost — what we pay the supplier |
| | `purchase_price` | `wp_atum_product_data` (ATUM table) | Same cost, written to ATUM's own table |

> **Why two cost fields?** `_op_cost_price` is a standard WooCommerce meta key stored in `wp_postmeta`. But we also use the **ATUM Inventory Management** plugin, which stores its own `purchase_price` in a separate table called `wp_atum_product_data`. Both need the same value so that ATUM reports and WooCommerce reports both show the correct cost. The sync writes to both in one API call.

---

## The Big Picture

Here is the simplest way to think about what happens every night:

```
  WHOLESCRIPTS API                    THIS SCRIPT                    WOOCOMMERCE STORE
  (supplier data)                    (runs at midnight)              (store.doctorsstudio.com)
                                    
  "Product X costs $44.99,     →     Compares old vs new      →     Updates Product X:
   we have 2,600 in stock,           values for each product         price=$44.99
   your cost is $22.99"                                              stock=2,600
                                                                     cost=$22.99
                                          ↓
                                    
                                     Logs everything to
                                     Postgres database
                                     (what changed, what failed,
                                      what was skipped)
```

---

## How It Works (Step by Step)

Every night at 12:00 AM Eastern, the cron job triggers `updatescript.py`. Here is what happens inside:

### Step 1 — Lock the Door

The script creates a lock file (`/tmp/wholescripts_sync.pid`) so that if another copy accidentally starts at the same time, it won't run twice and cause conflicts.

### Step 2 — Call the Wholescripts API

```
GET https://api.wholescripts.com/api/Orders/ProductList
```

This returns a big list of ~1,059 products. Each product has a SKU (unique ID), retail price, stock quantity, and wholesale (cost) price.

### Step 3 — Get the SKU Lookup Table

This is the secret sauce. The script opens an **SSH tunnel** to a remote MySQL database and reads a mapping table called `wholescript_supplier_sku`. This table tells us: *"Wholescripts SKU `300104424` belongs to WooCommerce product ID `4586`."*

Why do we need this? Because Wholescripts and WooCommerce store SKUs differently (see [The SKU Matching Problem](#the-sku-matching-problem-and-how-we-solved-it) below).

### Step 4 — Get All WooCommerce Products

```
GET https://store.doctorsstudio.com/wp-json/wc/v3/products?per_page=100&page=1,2,3...
```

The script pages through all ~1,284 products in our store. For each product, it grabs the current price, stock, and cost so it can compare against what Wholescripts says.

### Step 5 — Fetch Variations

Some products in our store are **variable products** — for example, "Adrenal Manager Capsules" comes in 60 Capsules and 120 Capsules. Each size is a separate "variation" with its own price, stock, and cost.

The lookup table tells us which WooCommerce IDs are variations. The script fetches those variations from their parent products so they can be matched and updated too.

### Step 6 — Match Wholescripts Products to WooCommerce Products

For each Wholescripts product, the script tries to find the matching WooCommerce product using two methods:

1. **Lookup table** (primary) — strip leading zeros from the Wholescripts SKU, look it up in the mapping table to get the WooCommerce product ID
2. **Direct SKU match** (fallback) — if not in the lookup table, try matching the full SKU directly

This finds ~310 SKU-level matches.

### Step 7 — Compare and Decide

For each matched product, the script compares:

- Is the **price** different?
- Is the **stock quantity** different?
- Is the **cost price** different?

If **nothing changed** → skip it (don't waste an API call).
If **anything is different** → prepare an update.

### Step 8 — Update WooCommerce

For each product that needs updating, the script sends a PUT request:

**Simple products:**
```
PUT /wp-json/wc/v3/products/4586
```

**Variations (different sizes):**
```
PUT /wp-json/wc/v3/products/745/variations/6460
```

The payload looks like:
```json
{
  "regular_price": "44.99",
  "manage_stock": true,
  "stock_quantity": 2600,
  "purchase_price": 22.99,
  "meta_data": [
    { "key": "_op_cost_price", "value": "22.99" }
  ]
}
```

**About the two cost fields:**
- `_op_cost_price` → stored in `wp_postmeta` (standard WooCommerce meta)
- `purchase_price` → stored in `wp_atum_product_data` (ATUM plugin's own table)

ATUM does **not** read from `wp_postmeta` for purchase price — it has its own table:
```sql
SELECT purchase_price FROM wp_atum_product_data WHERE product_id = 4586;
```
`purchase_price` is a **top-level field** in the WooCommerce REST API (added by ATUM's API extension), not a meta key. That's why it sits outside `meta_data` in the payload.

If WooCommerce returns an error (like 429 rate limit or 500 server error), the script **retries** up to 3 times with increasing wait times (1 second, then 2 seconds, then 4 seconds).

### Step 9 — Log Everything

Every single action is logged to a **Postgres database** — what was updated, what was skipped, what failed, and what was missing. This gives us a complete audit trail. See [Database Logging](#database-logging-how-we-track-everything) below.

### Step 10 — Unlock the Door

The lock file is removed. The script exits. Summary is printed to the log file.

---

## The SKU Matching Problem (and How We Solved It)

This is the most important concept to understand if you are going to work on this code.

### The Problem

Wholescripts stores SKUs with **leading zeros**:
```
000000000300104424
```

WooCommerce stores the same product with a **shorter SKU**:
```
300104424
```

They are the same product, but the SKUs don't match if you compare them directly. On top of that, many of our Wholescripts products in the store are **variations** (like different capsule counts), and WooCommerce stores variations separately from their parent product.

### The Solution — The Lookup Table

We have a MySQL table called `wholescript_supplier_sku` on a remote server (`34.148.82.199`). This table was built to map short SKUs to WooCommerce product IDs:

| woo_sku (short) | product_id (WooCommerce) | product name |
|---|---|---|
| `300104424` | `4586` | Acetyl L-Carnitine 500mg Capsules |
| `300000002` | `6460` | Adrenal Manager Capsules - 120 Capsules |
| `#N/A` | `745` | Adrenal Manager Capsules (parent — has no direct SKU) |

The table has **404 rows** total:
- **81 rows** are `#N/A` — these are parent variable products (they don't have their own SKU)
- **323 rows** have valid SKUs — these are simple products or individual variations

### The Matching Strategy

The script uses a **two-step matching approach**:

1. **Strip leading zeros** from the Wholescripts SKU: `000000000300104424` → `300104424`
2. **Look up** `300104424` in the mapping table → get WooCommerce product ID `4586`
3. If not in the lookup table, **fall back** to matching the full SKU directly against WooCommerce

This gets us ~310 SKU-level matches, which represent ~246 unique parent-level products (since some products have multiple variations that each get matched separately).

### Why Variations Matter

Take "Adrenal Manager Capsules" as an example:

- **Parent product** (WooCommerce ID `745`) — this is the product page, but you can't buy it directly
- **60 Capsules variation** (WooCommerce ID `6461`, SKU `300000092`) — this has its own price and stock
- **120 Capsules variation** (WooCommerce ID `6460`, SKU `300000002`) — this has its own price and stock

The script updates **each variation separately** because they have different prices and stock quantities. That is why our sync matches ~310 SKUs even though there are only ~246 products — the extra ~64 are individual variations.

When updating a variation, the script uses a different API endpoint:
```
PUT /products/745/variations/6460    ← needs the parent ID in the URL
```
instead of:
```
PUT /products/6460                   ← this would return 404
```

The code tracks which product IDs are variations and stores their parent ID so it uses the correct endpoint.

---

## Project Structure (Every File Explained)

```
/var/www/wholescripts-sync/
│
├── updatescript.py                  # The entry point — this is what cron calls
├── test_single_product.py           # Interactive single-product sync/test tool
├── analyze_kinsta_wholescripts.py   # Cross-catalog analysis (name-match WooCommerce ↔ Wholescripts)
├── requirements.txt                 # Python packages this project needs
├── .env                             # Credentials and config (git-ignored, never commit this)
├── .env.example                     # Template showing what goes in .env
├── WORKFLOW.md                      # Quick-reference workflow documentation
├── CHANGELOG.md                     # Change log for notable updates
├── README.md                        # This file
│
├── src/                             # All the actual code lives here
│   ├── __init__.py                  # Makes src/ a Python package (empty file)
│   ├── config.py                    # Loads environment variables from .env
│   ├── logger.py                    # Sets up logging (console + file)
│   ├── db.py                        # Postgres database: creates tables, logs data
│   ├── wholescripts_client.py       # Talks to the Wholescripts API
│   ├── woo_client.py                # Talks to the WooCommerce REST API
│   ├── woo_db.py                    # Direct WooCommerce MariaDB queries via SSH tunnel
│   ├── lookup.py                    # SSH tunnel + MySQL lookup table
│   ├── mapper.py                    # SKU matching + change detection
│   └── sync.py                      # The main orchestrator (ties everything together)
│
├── diagnose_advanced.py             # Deep diagnostic: reconciles match counts
├── diagnose_skus.py                 # Basic SKU format analysis tool
├── diagnose_variations.py           # Analyzes which lookup IDs are variations
└── wholescript_supplier_sku_*.md    # Raw export of the MySQL lookup table
```

### What Each File Does

#### `updatescript.py` — The Entry Point

This is the file that cron calls every night. Think of it as the "front door" of the application. It:

1. Loads the `.env` file
2. Validates that all required config is present
3. Creates a lock file so two runs can't overlap
4. Calls `run_sync()` (the main function in `sync.py`)
5. Removes the lock file when done
6. Supports `--dry-run` flag to preview without making changes

**When to edit:** If you need to change how the script starts up, how locking works, or add new command-line flags.

#### `src/config.py` — Configuration Loader

Reads environment variables from `.env` and makes them available as `Config.VARIABLE_NAME` throughout the code. Every other file imports from here instead of reading `.env` directly.

**When to edit:** If you add a new environment variable (like a new API key), add it here so other files can access it.

#### `src/logger.py` — Logging Setup

Configures Python's logging system so that:
- **Console** shows INFO-level messages (you see progress while running)
- **File** (`/var/log/wholescripts_sync.log`) captures everything for later review

**When to edit:** If you want to change log format, add a new log file, or change log levels.

#### `src/db.py` — Database Logging

Handles everything related to Postgres. It:
- **Creates tables** on first run (safe to run multiple times — uses `IF NOT EXISTS`)
- **Logs each run** summary (how many matched, updated, failed, etc.)
- **Logs each product** attempt (what was sent, what came back, old vs new values)

Contains the `SyncDB` class with these methods:
- `connect()` — opens a Postgres connection
- `ensure_tables()` — creates tables and indexes if they don't exist
- `insert_run()` — creates a new run record
- `finish_run()` — updates the run record with final counts
- `log_item()` — logs one product's sync attempt
- `close()` — closes the connection

**When to edit:** If you need to add new columns to the log tables or change what gets logged.

#### `src/wholescripts_client.py` — Wholescripts API Client

Talks to the Wholescripts supplier API using HTTP Basic Authentication.

- `fetch_product_list()` — calls `GET /api/Orders/ProductList` and returns all products
- `build_sku_map()` — converts the raw product list into a dictionary keyed by SKU, like:
  ```python
  {"000000000300104424": {"retail_price": 44.99, "qty": 2600, "cost_price": 22.99, "product_name": "..."}}
  ```

**When to edit:** If the Wholescripts API changes (new URL, new fields, new auth method).

#### `src/woo_client.py` — WooCommerce REST API Client

Talks to our WooCommerce store using consumer key/secret OAuth authentication. This is the biggest file because WooCommerce has a lot of moving parts.

Key methods:
- `fetch_all_products()` — pages through all products (100 per page, ~13 pages)
- `build_sku_map()` — index products by SKU for direct matching
- `build_id_map()` — index products by ID for lookup-table matching
- `fetch_variations_for_lookup()` — fetches variation products that the lookup table points to
- `update_product()` — `PUT /products/<id>` to update a simple product
- `update_variation()` — `PUT /products/<parent>/variations/<id>` to update a variation
- Built-in **retry logic**: retries on 429/500/502/503/504 errors with exponential backoff

**When to edit:** If you need to update additional WooCommerce fields, change the retry behavior, or handle a new product type.

#### `src/lookup.py` — SSH Tunnel + MySQL SKU Lookup

This file handles the connection to the remote MySQL database that holds the SKU mapping table.

- `fetch_sku_lookup()` — the main function. It:
  1. Opens an SSH tunnel to `34.148.82.199` (user: `joy`)
  2. Connects to MySQL through the tunnel (port `33066`)
  3. Reads the `wholescript_supplier_sku` table
  4. Filters out `#N/A` rows (parent products with no direct SKU)
  5. Returns a dictionary: `{"300104424": 4586, "300000002": 6460, ...}`
  6. Closes the SSH tunnel

If the tunnel or MySQL connection fails, the script does **not** crash — it falls back to direct SKU matching only (fewer matches, but still works).

**When to edit:** If the remote server changes, the SSH key moves, or the lookup table structure changes.

#### `src/mapper.py` — SKU Matching + Change Detection

This is where the actual matching logic lives. Given the data from Wholescripts, WooCommerce, and the lookup table, it figures out which products match and what needs updating.

Key functions:
- `ws_sku_to_short()` — strips leading zeros: `000000000300104424` → `300104424`
- `compute_updates()` — the main matching function. For each Wholescripts product:
  1. Try lookup table match (strip zeros → look up product ID)
  2. Try direct SKU match (fallback)
  3. If matched, compare old vs new values
  4. Return lists of: updates needed, skipped (no change), missing (not in WooCommerce)

**When to edit:** If you need to change how SKUs are matched, add new fields to compare, or change when a product is considered "changed."

#### `src/sync.py` — The Main Orchestrator

This is the conductor of the orchestra. It calls all the other modules in the right order and handles the overall flow. The `run_sync()` function is the heart of the application — it runs the 10 steps described in [How It Works](#how-it-works-step-by-step).

**When to edit:** If you need to change the order of operations, add a new step to the sync process, or change how errors are handled at the top level.

#### `src/woo_db.py` — Direct WooCommerce MariaDB Access

Connects directly to the WooCommerce MariaDB database via SSH tunnel (`67.225.164.73`). Used by `test_single_product.py` to query raw database values (e.g. `wp_postmeta`, `wp_atum_product_data`) when verifying that API updates actually landed in the database.

**When to edit:** If the WooCommerce server credentials change, the SSH tunnel setup changes, or you need to query additional database tables.

#### `test_single_product.py` — Interactive Single-Product Sync Tool

An interactive CLI tool for testing the sync on **one product at a time**. Uses the same core logic as the nightly sync (`WooClient`, `WholescriptsClient`) but adds a step-by-step UI with ASCII art, progress indicators, and user prompts. You can:

1. Search for a product by name or SKU
2. View its current WooCommerce values vs Wholescripts values
3. Choose to apply Wholescripts values or enter manual values
4. See ATUM inventory details and update them
5. Verify changes by querying the database directly

```bash
/var/www/wholescripts-sync/venv/bin/python3 test_single_product.py
```

**When to edit:** If you add new fields to the sync or change the update payload structure.

#### `analyze_kinsta_wholescripts.py` — Cross-Catalog Analysis

Compares product catalogs between WooCommerce and Wholescripts using **product name matching** (not SKU, since SKUs differ in format between the two systems). Outputs a console summary showing:

- How many products exist in each catalog
- Name matches (same product in both systems)
- Products only in WooCommerce or only in Wholescripts
- Products with the same name but different SKUs (indicates outdated SKU data)

The script auto-detects which environment you are pointing at based on the URLs in `.env`:
- `testservices.wholescripts.com` → Wholescripts **Test**
- `api.wholescripts.com` → Wholescripts **Production**
- `dsstore.kinsta.cloud` → WooCommerce **Kinsta staging**
- `store.doctorsstudio.com` → WooCommerce **Production**

```bash
/var/www/wholescripts-sync/venv/bin/python3 analyze_kinsta_wholescripts.py
```

**When to edit:** If you want to add additional comparison fields or change the matching logic.

---

## How the Files Connect

Here is how data flows through the system when the script runs:

```
updatescript.py
  │
  ├─ loads .env via config.py
  │
  └─ calls sync.py → run_sync()
       │
       ├─ db.py                → connect to Postgres, create tables
       │
       ├─ wholescripts_client.py → fetch products from Wholescripts API
       │                           returns: {sku: {price, stock, cost}}
       │
       ├─ lookup.py             → SSH tunnel → MySQL → read lookup table
       │                           returns: {short_sku: woo_product_id}
       │
       ├─ woo_client.py         → fetch all products from WooCommerce API
       │                         → fetch variations for variable products
       │                           returns: {id: {price, stock, cost}}
       │
       ├─ mapper.py             → match SKUs, compare values, find changes
       │                           returns: updates[], skipped[], missing[]
       │
       ├─ woo_client.py         → PUT updates to WooCommerce for each change
       │
       └─ db.py                 → log every action (success/fail/skip/missing)
```

### The Import Chain

```
updatescript.py
  └── imports: src.sync (run_sync)
                 └── imports: src.config (Config)
                              src.logger (setup_logger)
                              src.db (SyncDB)
                              src.wholescripts_client (WholescriptsClient)
                              src.woo_client (WooClient)
                              src.lookup (fetch_sku_lookup)
                              src.mapper (compute_updates)
```

Every file imports `config.py` and `logger.py` for configuration and logging. The heavy lifting is done by the four main modules: `wholescripts_client.py`, `woo_client.py`, `lookup.py`, and `mapper.py`. The `sync.py` file ties them all together.

---

## Setup Guide

### Prerequisites

- **Python 3.10+** (check with `python3 --version`)
- **Postgres** running locally on port 5432 with the `pos_prod` database
- **SSH key** for the remote MySQL server at `/var/www/wholescripts-sync/id_servicemenuserver_new`
- Access to the WooCommerce REST API (consumer key + secret)
- Access to the Wholescripts API (username + password)

### Installation

```bash
# 1. Go to the project directory
cd /var/www/wholescripts-sync

# 2. Create a Python virtual environment (if not already done)
python3 -m venv venv

# 3. Activate it
source venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Create your .env file from the template
cp .env.example .env

# 6. Edit .env and fill in the real credentials
nano .env
```

### Verify It Works

```bash
# Run a dry-run (safe — does not update the store)
/var/www/wholescripts-sync/venv/bin/python3 updatescript.py --dry-run
```

You should see output like:
```
Fetched 1064 products from Wholescripts
Loaded 321 SKU→product_id mappings from lookup table
Fetched ~1284 total Woo products
Summary: ws=1064, woo=1284, matched=310, updated=~300, skipped=~10, missing=~754, failed=0
```

---

## How to Run

### Dry Run (Preview Only — Safe)

This shows what **would** change without actually updating the store. Always run this first if you have made code changes.

```bash
/var/www/wholescripts-sync/venv/bin/python3 /var/www/wholescripts-sync/updatescript.py --dry-run
```

### Real Run (Updates the Store)

This actually sends updates to WooCommerce. Products on the store will change.

```bash
/var/www/wholescripts-sync/venv/bin/python3 /var/www/wholescripts-sync/updatescript.py
```

### What the Output Means

```
Summary: ws=1064, woo=1284, matched=310, updated=~300, skipped=~10, missing=~754, failed=0
```

| Field | Meaning |
|---|---|
| `ws=1064` | Wholescripts API returned 1,064 products |
| `woo=1284` | WooCommerce has 1,284 products total |
| `matched=310` | 310 Wholescripts SKUs matched to WooCommerce products |
| `updated=302` | 302 products had changes and were updated |
| `skipped=8` | 8 products matched but nothing changed (already in sync) |
| `missing=749` | 749 Wholescripts products are not in our store (expected — we only sell ~246 of them) |
| `failed=0` | 0 updates failed (this should always be 0) |

---

## The Cron Job (Automatic Nightly Runs)

The script is scheduled to run automatically every night. Here is the cron entry:

```cron
TZ=America/New_York
0 0 * * * /var/www/wholescripts-sync/venv/bin/python3 /var/www/wholescripts-sync/updatescript.py >> /var/log/wholescripts_sync.log 2>&1
```

### What This Means

| Part | Meaning |
|---|---|
| `TZ=America/New_York` | Use Eastern Time (automatically handles daylight saving) |
| `0 0 * * *` | Run at minute 0 of hour 0 = **12:00 AM** every day |
| `/var/www/.../venv/bin/python3` | Use the project's virtual environment Python |
| `/var/www/.../updatescript.py` | The script to run |
| `>> /var/log/wholescripts_sync.log` | Append output to the log file |
| `2>&1` | Also capture error messages in the same log file |

### How to View or Edit the Cron Job

```bash
# View current cron jobs
crontab -l

# Edit cron jobs
crontab -e
```

### How to Check If It Ran Last Night

```bash
# Check the log file for the latest run
tail -50 /var/log/wholescripts_sync.log

# Or check the database (more reliable)
PGPASSWORD='(your database password)' psql -U pos_produser -h 127.0.0.1 -d pos_prod \
  -c "SELECT run_id, started_at, finished_at, matched, updated, failed, notes
      FROM wholescripts_woo_sync_runs ORDER BY started_at DESC LIMIT 5;"
```

---

## Database Logging (How We Track Everything)

Every run is logged to two tables in the Postgres database `pos_prod`.

### Table 1: `wholescripts_woo_sync_runs`

**One row per nightly run.** This is the summary — did the run succeed? How many products were updated?

| Column | Type | What It Stores |
|---|---|---|
| `run_id` | UUID | Unique identifier for this run |
| `started_at` | Timestamp | When the run started |
| `finished_at` | Timestamp | When the run finished |
| `total_ws_products` | Integer | How many products Wholescripts returned |
| `total_woo_products` | Integer | How many products WooCommerce has |
| `matched` | Integer | How many SKUs matched between the two |
| `updated` | Integer | How many products were actually updated |
| `skipped` | Integer | How many matched but had no changes |
| `missing_in_woo` | Integer | How many Wholescripts products aren't in our store |
| `failed` | Integer | How many updates failed |
| `notes` | Text | "DRY RUN" if dry-run, or error messages |

### Table 2: `wholescripts_woo_sync_log`

**One row per product per run.** This is the detail — exactly what happened to each product.

| Column | Type | What It Stores |
|---|---|---|
| `id` | Bigserial | Auto-incrementing row ID |
| `run_id` | UUID | Links to which run this belongs to |
| `sku` | Text | The Wholescripts SKU |
| `woo_product_id` | Bigint | The WooCommerce product/variation ID |
| `status` | Text | `success`, `failed`, `skipped_no_change`, `missing_in_woo`, or `dry_run` |
| `request_body` | JSONB | The exact JSON payload sent to WooCommerce |
| `response_body` | JSONB | The exact JSON response from WooCommerce |
| `prev_regular_price` | Numeric | Price **before** the update |
| `prev_stock_quantity` | Integer | Stock **before** the update |
| `prev_cost_price` | Numeric | Cost **before** the update |
| `new_regular_price` | Numeric | Price **after** the update |
| `new_stock_quantity` | Integer | Stock **after** the update |
| `new_cost_price` | Numeric | Cost **after** the update |
| `error` | Text | Error message if the update failed |
| `created_at` | Timestamp | When this row was logged |

---

## How to Check Logs

### See the Last 5 Runs

```sql
SELECT run_id, started_at, finished_at, total_ws_products, total_woo_products,
       matched, updated, skipped, missing_in_woo, failed, notes
FROM wholescripts_woo_sync_runs
ORDER BY started_at DESC LIMIT 5;
```

### See What Changed in a Specific Run

```sql
SELECT sku, woo_product_id,
       prev_regular_price, new_regular_price,
       prev_stock_quantity, new_stock_quantity,
       prev_cost_price, new_cost_price
FROM wholescripts_woo_sync_log
WHERE run_id = '<paste-run-id-here>'
  AND status = 'success'
ORDER BY created_at;
```

### See Failures

```sql
SELECT sku, woo_product_id, error
FROM wholescripts_woo_sync_log
WHERE run_id = '<paste-run-id-here>'
  AND status = 'failed';
```

### See Products Missing from WooCommerce

```sql
SELECT sku
FROM wholescripts_woo_sync_log
WHERE run_id = '<paste-run-id-here>'
  AND status = 'missing_in_woo';
```

### Quick One-Liner (Connect and Query)

```bash
PGPASSWORD='(your database password)' psql -U pos_produser -h 127.0.0.1 -d pos_prod \
  -c "SELECT * FROM wholescripts_woo_sync_runs ORDER BY started_at DESC LIMIT 5;"
```

---

## Diagnostic Tools

These scripts are not part of the nightly sync — they are standalone tools for debugging and analysis.

### `diagnose_advanced.py` — Full Reconciliation Report

The most useful diagnostic. Run this to understand the match numbers and find issues:

```bash
/var/www/wholescripts-sync/venv/bin/python3 diagnose_advanced.py
```

What it tells you:
- **Lookup table breakdown** — how many rows are parents, variations, or simple products
- **Wholescripts API coverage** — which lookup SKUs exist in the current API
- **WooCommerce coverage** — which lookup product IDs exist in the store
- **Match source breakdown** — how many matched via lookup table vs direct SKU
- **The 246 question** — how many unique parent-level products matched (should be close to 246)
- **Duplicate SKUs** — lookup table entries that map the same SKU to multiple products
- **Stale entries** — lookup table entries pointing to products that no longer exist in WooCommerce

### `diagnose_skus.py` — Basic SKU Format Analysis

Compares how SKUs look in Wholescripts vs WooCommerce to understand format differences:

```bash
/var/www/wholescripts-sync/venv/bin/python3 diagnose_skus.py
```

### `diagnose_variations.py` — Variation Analysis

Identifies which lookup table product IDs are variations vs parent products vs simple products:

```bash
/var/www/wholescripts-sync/venv/bin/python3 diagnose_variations.py
```

### `wholescript_supplier_sku_202602201924.md`

A raw export of the MySQL lookup table captured on Feb 20, 2026. Useful as a reference — you can see all 404 rows with their product names, SKUs, and WooCommerce IDs without needing to connect to the remote database.

---

## Switching Environments

The `.env` file is organized into **switchable blocks**. Each service (Wholescripts and WooCommerce) has two credential blocks — one for production and one for test/staging. Only **one block per service** should be uncommented at a time.

### How to Switch

Edit `.env` and comment/uncomment the appropriate blocks:

```bash
# ── Wholescripts API ─────────────────────────────────────
# Uncomment ONE block at a time.

# Wholescripts (test)
#WHOLESCRIPTS_API_USERNAME=295849_API
#WHOLESCRIPTS_API_PASSWORD=...
#WHOLESCRIPT_API_URL=https://testservices.wholescripts.com/api

# Wholescripts (production)
WHOLESCRIPTS_API_USERNAME=295849_API
WHOLESCRIPTS_API_PASSWORD=...
WHOLESCRIPT_API_URL=https://api.wholescripts.com/api

# ── WooCommerce REST API ─────────────────────────────────
# Uncomment ONE block at a time.

# WooCommerce (production)
WOO_API_URL=https://store.doctorsstudio.com
WOO_CONSUMER_KEY=ck_...
WOO_CONSUMER_SECRET=cs_...
WOOCOMMERCE_API_VERSION=wc/v3

# WooCommerce (Kinsta staging)
#WOO_API_URL=https://dsstore.kinsta.cloud
#WOO_CONSUMER_KEY=ck_...
#WOO_CONSUMER_SECRET=cs_...
#WOOCOMMERCE_API_VERSION=wc/v3
```

### How the Scripts Know Which Environment Is Active

The scripts detect the environment automatically from the URLs — no extra flags needed:

| URL contains | Detected as |
|---|---|
| `testservices.wholescripts.com` | Wholescripts **Test** |
| `api.wholescripts.com` | Wholescripts **Production** |
| `dsstore.kinsta.cloud` | WooCommerce **Kinsta Staging** |
| `store.doctorsstudio.com` | WooCommerce **Production** |

This is used by `analyze_kinsta_wholescripts.py` and `test_single_product.py` to label output correctly. The nightly sync (`updatescript.py`) always runs against whatever is uncommented.

---

## Environment Variables

All credentials and configuration are stored in `.env` (git-ignored). Use `.env.example` as a template.

### Postgres (Logging Database)

| Variable | Purpose | Example |
|---|---|---|
| `DB_NAME` | Database name | `pos_prod` |
| `DB_USER` | Database user | `pos_produser` |
| `DB_PASSWORD` | Database password | `(your password)` |
| `DB_HOST` | Database host | `127.0.0.1` |
| `DB_PORT` | Database port | `5432` |

### Wholescripts API

| Variable | Purpose | Example |
|---|---|---|
| `WHOLESCRIPTS_API_USERNAME` | API username | `295849_API` |
| `WHOLESCRIPTS_API_PASSWORD` | API password | `(your password)` |
| `WHOLESCRIPT_API_URL` | API base URL | `https://api.wholescripts.com/api` |

> **Important:** The URL must end with `/api`. The script appends `/Orders/ProductList` to it.

> **Switching environments:** The `.env` file has two commented blocks — one for **production** (`api.wholescripts.com`) and one for **test** (`testservices.wholescripts.com`). Uncomment whichever block you need and comment the other. The scripts auto-detect which environment is active based on the URL.

### WooCommerce REST API

| Variable | Purpose | Example |
|---|---|---|
| `WOO_API_URL` | Store URL | `https://store.doctorsstudio.com` |
| `WOO_CONSUMER_KEY` | OAuth consumer key | `ck_...` |
| `WOO_CONSUMER_SECRET` | OAuth consumer secret | `cs_...` |
| `WOOCOMMERCE_API_VERSION` | API version | `wc/v3` |
| `WOO_COST_META_KEY` | Meta key for cost price in `wp_postmeta` | `_op_cost_price` |

> **Note:** ATUM `purchase_price` is sent as a top-level API field (not a meta key) and writes to `wp_atum_product_data.purchase_price`. No env var needed — the field name is part of ATUM's REST API.

> **Switching environments:** The `.env` file has two commented blocks — one for **production** (`store.doctorsstudio.com`) and one for **Kinsta staging** (`dsstore.kinsta.cloud`). Uncomment whichever block you need and comment the other. Each block has its own `WOO_API_URL`, `WOO_CONSUMER_KEY`, and `WOO_CONSUMER_SECRET`.

### WooCommerce Database (Direct MariaDB Access)

| Variable | Purpose | Example |
|---|---|---|
| `WOO_IP` | WooCommerce server IP | `67.225.164.73` |
| `WOO_USER_NAME` | SSH username | `root` |
| `WOO_PASSWORD` | SSH password | `(your password)` |
| `WOO_IP_PORT` | SSH port | `22` |
| `WOO_DB_NAME` | MariaDB database name | `dsstore2` |
| `WOO_DB_USER` | MariaDB username | `apiserver` |
| `WOO_DB_PASSWORD` | MariaDB password | `(your password)` |
| `WOO_DB_HOST` | MariaDB host (on remote) | `localhost` |
| `WOO_DB_PORT` | MariaDB port (on remote) | `3306` |

> Used by `test_single_product.py` to verify updates by querying `wp_postmeta` and `wp_atum_product_data` directly.

### SSH Tunnel + MySQL (SKU Lookup Table)

| Variable | Purpose | Example |
|---|---|---|
| `MYSQL_HOST` | MySQL host (through tunnel) | `127.0.0.1` |
| `MYSQL_PORT` | MySQL port on remote server | `3306` |
| `MYSQL_USER` | MySQL username | `root` |
| `MYSQL_PASSWORD` | MySQL password | `(your password)` |
| `MYSQL_DATABASE` | Database name | `doctorsstudio` |
| `SSH_HOST` | Remote server IP | `34.148.82.199` |
| `SSH_USER` | SSH username | `joy` |
| `SSH_KEY_PATH` | Path to SSH private key | `/var/www/wholescripts-sync/id_servicemenuserver_new` |
| `SSH_LOCAL_PORT` | Local port for SSH tunnel | `33066` |

---

## Troubleshooting

### "Wholescripts API failed — 404"

The API URL is wrong. Make sure `WHOLESCRIPT_API_URL` in `.env` ends with `/api`:
```
WHOLESCRIPT_API_URL=https://api.wholescripts.com/api     ← correct
WHOLESCRIPT_API_URL=https://api.wholescripts.com         ← wrong (404)
```

### "Could not load SKU lookup table"

The SSH tunnel to the remote MySQL server failed. Check:
- Is the SSH key file present at the path in `SSH_KEY_PATH`?
- Can you manually SSH? `ssh -i /var/www/wholescripts-sync/id_servicemenuserver_new joy@34.148.82.199`
- Is port `33066` already in use? Try: `fuser 33066/tcp`

The script will continue with direct SKU matching only (fewer matches, but still works).

### "Lock file exists"

The script found `/tmp/wholescripts_sync.pid` from a previous run. This means either:
- Another instance is already running (wait for it to finish)
- A previous run crashed without cleaning up. Delete the lock file: `rm /tmp/wholescripts_sync.pid`

### "0 matched" or Very Low Match Count

- Check if the Wholescripts API returned products: look for `ws=0` in the summary
- Check if the lookup table loaded: look for "Loaded 321 SKU→product_id mappings"
- If both are fine, the WooCommerce product SKUs may have changed — run `diagnose_advanced.py`

### WooCommerce Returns 401/403

The consumer key or secret is wrong or expired. Check `WOO_CONSUMER_KEY` and `WOO_CONSUMER_SECRET` in `.env`. You can generate new keys in WooCommerce → Settings → Advanced → REST API.

### High Number of Failures

Check the log table for error details:
```sql
SELECT sku, woo_product_id, error
FROM wholescripts_woo_sync_log
WHERE status = 'failed'
ORDER BY created_at DESC LIMIT 20;
```

Common causes: WooCommerce rate limiting (429), server errors (500), or products that were deleted.

---

## Numbers at a Glance

*As of February 26, 2026:*

| Metric | Value |
|---|---|
| Wholescripts products in their API | ~1,064 |
| Products in our WooCommerce store | ~1,284 |
| Wholescripts products in our store | ~246 (parent-level) |
| SKU mappings in lookup table | 321 (+ 81 parent rows) |
| SKU-level matches per sync run | ~310 |
| Typical updates per run | ~300 |
| Script run time | ~6 minutes |
| Cron schedule | 12:00 AM Eastern, every day |
