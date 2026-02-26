# Changelog

All notable changes to the Wholescripts → WooCommerce sync project.

---

## [1.2.0] – 2026-02-26

### Added
- **ATUM Multi-Inventory support** — After updating a product or variation, the script now also updates the correct ATUM inventory location with `manage_stock: true` and the Wholescripts `stock_quantity`.

### Inventory selection priority
1. **Dropship** — preferred if it exists
2. **Jupiter Inventory** or **Boca Inventory** — fallback if no Dropship
3. **Main Inventory** — fallback if none of the above exist
4. If no ATUM inventories exist at all, the main WooCommerce `stock_quantity` (already updated in the product/variation payload) serves as the fallback.

### How it works
- Works for both **simple products** and **variations** — uses the same `GET /products/{id}/inventories` endpoint (variation IDs work as product IDs).
- For the selected inventory, sends `PUT /products/{id}/inventories/{inv_id}` with `meta_data: { manage_stock: true, stock_quantity: N, stock_status: instock/outofstock, purchase_price: X }`.
- Dry-run mode logs which inventory *would* be updated without making changes.

### Files Modified
- `src/woo_client.py` — added `fetch_inventories()`, `select_inventory()`, `update_inventory()` methods.
- `src/sync.py` — calls ATUM inventory update after each successful product/variation update; added dry-run inventory logging.
- `.env` / `.env.example` — removed stale `ATUM_PURCHASE_PRICE_META_KEY`.

---

## [1.1.0] – 2026-02-20

### Fixed
- **ATUM purchase price now writes correctly** — Previously used `_atum_purchase_price` as a `wp_postmeta` meta key, but ATUM stores purchase price in `wp_atum_product_data.purchase_price`, not in `wp_postmeta`. The meta key approach silently did nothing.

### Changed
- `purchase_price` is now sent as a **top-level field** in the WooCommerce REST API payload, which ATUM's API extension picks up and writes to `wp_atum_product_data`.
- Removed `_atum_purchase_price` from the `meta_data` array in update payloads.
- Removed `ATUM_PURCHASE_PRICE_META_KEY` config variable (no longer needed).

### Files Modified
- `src/mapper.py` — payload now includes `"purchase_price": <float>` at top level; removed `_atum_purchase_price` from `meta_data`.
- `src/woo_client.py` — removed `atum_meta_key` attribute.
- `src/config.py` — removed `ATUM_PURCHASE_PRICE_META_KEY`.

### Payload Before
```json
{
  "regular_price": "44.99",
  "stock_quantity": 2600,
  "meta_data": [
    {"key": "_op_cost_price", "value": "22.99"},
    {"key": "_atum_purchase_price", "value": "22.99"}
  ]
}
```

### Payload After
```json
{
  "regular_price": "44.99",
  "stock_quantity": 2600,
  "purchase_price": 22.99,
  "meta_data": [
    {"key": "_op_cost_price", "value": "22.99"}
  ]
}
```

---

## [1.0.0] – 2026-02-20

### Added
- Initial sync implementation: Wholescripts API → WooCommerce REST API.
- Dual SKU matching: lookup table (primary) + direct SKU (fallback).
- SSH tunnel to remote MySQL for SKU lookup table.
- Postgres logging: `wholescripts_woo_sync_runs` + `wholescripts_woo_sync_log`.
- Dry-run mode, retry logic, PID lock file.
- Cron job: daily at 12:00 AM Eastern (DST-safe).
- Comprehensive README and WORKFLOW docs.
