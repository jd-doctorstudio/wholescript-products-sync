# Wholescripts → WooCommerce Nightly Product Sync

Pulls the Wholescripts product list daily at **12:00 AM Eastern** and updates WooCommerce products by SKU:

- `retailPrice` → `regular_price`
- `quantity` → `stock_quantity` (with `manage_stock: true`)
- `wholesalePrice` → `_op_cost_price` meta **and** `_atum_purchase_price` meta

## Setup

```bash
cd /var/www/wholescripts-sync
pip3 install -r requirements.txt
cp .env.example .env   # then fill in real credentials
```

## Usage

```bash
# Dry run (preview only, no Woo updates)
python3 updatescript.py --dry-run

# Normal run
python3 updatescript.py
```

## Cron (midnight Eastern, DST-safe)

```cron
TZ=America/New_York
0 0 * * * /var/www/wholescripts-sync/venv/bin/python3 /var/www/wholescripts-sync/updatescript.py >> /var/log/wholescripts_sync.log 2>&1
```

## Logging

Every run writes to two Postgres tables in `pos_prod`:

- **`wholescripts_woo_sync_runs`** — one row per run with summary counts
- **`wholescripts_woo_sync_log`** — one row per product attempt (success/fail/skip/missing)

## Project Structure

```
wholescripts-sync/
  updatescript.py          # Entry point
  requirements.txt
  .env                     # Credentials (git-ignored)
  src/
    config.py              # Env loading
    logger.py              # Logging setup
    db.py                  # Postgres tables + insert helpers
    wholescripts_client.py # Wholescripts API client
    woo_client.py          # WooCommerce REST client with retry
    mapper.py              # SKU matching + change detection
    sync.py                # Main sync orchestrator
```
# wholescript-products-sync
