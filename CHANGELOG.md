# Changelog

All notable changes to the Wholescripts → WooCommerce sync project.

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
