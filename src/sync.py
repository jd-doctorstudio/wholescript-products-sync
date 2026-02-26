import uuid
from typing import Optional

from src.config import Config
from src.logger import setup_logger
from src.db import SyncDB
from src.wholescripts_client import WholescriptsClient
from src.woo_client import WooClient
from src.mapper import compute_updates
from src.lookup import fetch_sku_lookup

logger = setup_logger("wholescripts_sync.sync")


def run_sync(dry_run: Optional[bool] = None) -> dict:
    """Execute one full sync cycle. Returns summary dict."""

    if dry_run is None:
        dry_run = Config.DRY_RUN

    run_id = uuid.uuid4()
    logger.info("=== Sync run %s started (dry_run=%s) ===", run_id, dry_run)

    db = SyncDB()
    ws_client = WholescriptsClient()
    woo_client = WooClient()

    summary = {
        "run_id": str(run_id),
        "total_ws_products": 0,
        "total_woo_products": 0,
        "matched": 0,
        "updated": 0,
        "skipped": 0,
        "missing_in_woo": 0,
        "failed": 0,
    }

    try:
        # 1. Connect to Postgres and ensure tables
        db.connect()
        db.ensure_tables()
        db.insert_run(run_id)

        # 2. Fetch Wholescripts products
        try:
            ws_products = ws_client.fetch_product_list()
        except Exception as exc:
            logger.error("Wholescripts API failed — aborting run: %s", exc)
            db.finish_run(
                run_id=run_id,
                total_ws=summary["total_ws_products"],
                total_woo=summary["total_woo_products"],
                matched=summary["matched"],
                updated=summary["updated"],
                skipped=summary["skipped"],
                missing_in_woo=summary["missing_in_woo"],
                failed=summary["failed"],
                notes=f"ABORTED: Wholescripts API error: {exc}",
            )
            raise

        ws_by_sku = ws_client.build_sku_map(ws_products)
        summary["total_ws_products"] = len(ws_products)

        # 3. Fetch SKU lookup table (SSH tunnel to remote MySQL)
        try:
            sku_lookup = fetch_sku_lookup()
        except Exception as exc:
            logger.warning("Could not load SKU lookup table: %s — falling back to direct SKU matching only", exc)
            sku_lookup = {}

        # 4. Fetch Woo products (parents)
        woo_products = woo_client.fetch_all_products()
        woo_by_sku = woo_client.build_sku_map(woo_products)
        woo_by_id = woo_client.build_id_map(woo_products)
        summary["total_woo_products"] = len(woo_products)

        # 4b. Fetch variations referenced by lookup table but missing from parent fetch
        if sku_lookup:
            needed_ids = set(sku_lookup.values()) - set(woo_by_id.keys())
            if needed_ids:
                logger.info("%d lookup IDs not in parent products — fetching variations", len(needed_ids))
                variations = woo_client.fetch_variations_for_lookup(woo_products, needed_ids)
                # Build variation_id → parent_id map for later updates
                var_to_parent = {}
                for p in woo_products:
                    if p.get("type") == "variable":
                        for vid in p.get("variations", []):
                            if vid in needed_ids:
                                var_to_parent[vid] = p["id"]
                # Add variations to woo_by_id
                for v in variations:
                    meta_data = v.get("meta_data", [])
                    cost_price = woo_client._extract_meta_value(meta_data, woo_client.cost_meta_key)
                    woo_by_id[v["id"]] = {
                        "id": v["id"],
                        "sku": (v.get("sku") or "").strip(),
                        "regular_price": v.get("regular_price", ""),
                        "stock_quantity": v.get("stock_quantity"),
                        "cost_price": cost_price,
                        "name": v.get("name", "") or v.get("sku", ""),
                        "_parent_id": var_to_parent.get(v["id"]),
                    }
                logger.info("woo_by_id now has %d entries (parents + variations)", len(woo_by_id))
            else:
                logger.info("All lookup table IDs found in parent products")

        # 5. Compute diffs (lookup table as primary, direct SKU as fallback)
        updates, skipped, missing_in_woo = compute_updates(ws_by_sku, woo_by_id, sku_lookup, woo_by_sku)
        summary["matched"] = len(updates) + len(skipped)
        summary["skipped"] = len(skipped)
        summary["missing_in_woo"] = len(missing_in_woo)

        # Log skipped items
        for item in skipped:
            db.log_item(
                run_id=run_id,
                sku=item["sku"],
                woo_product_id=item["woo_product_id"],
                status="skipped_no_change",
            )

        # Log missing-in-woo items
        for item in missing_in_woo:
            db.log_item(
                run_id=run_id,
                sku=item["sku"],
                woo_product_id=None,
                status="missing_in_woo",
            )

        # 5. Apply updates
        for item in updates:
            sku = item["sku"]
            woo_id = item["woo_product_id"]
            payload = item["payload"]
            prev = item["prev"]
            new_vals = item["new_vals"]

            if dry_run:
                logger.info(
                    "[DRY RUN] Would update SKU %s (woo_id=%d): price %s→%s, stock %s→%s, cost %s→%s",
                    sku, woo_id,
                    prev["regular_price"], new_vals["regular_price"],
                    prev["stock_quantity"], new_vals["stock_quantity"],
                    prev["cost_price"], new_vals["cost_price"],
                )
                # Show which ATUM inventory would be updated
                try:
                    inventories = woo_client.fetch_inventories(woo_id)
                    chosen = woo_client.select_inventory(inventories)
                    if chosen:
                        inv_meta = chosen.get("meta_data", {})
                        logger.info(
                            "[DRY RUN]   ATUM inventory '%s' (id=%s) — manage_stock=%s→True, qty=%s→%s",
                            chosen.get("name"), chosen["id"],
                            inv_meta.get("manage_stock"), inv_meta.get("stock_quantity"),
                            new_vals["stock_quantity"],
                        )
                    else:
                        logger.info("[DRY RUN]   No ATUM inventories found for woo_id=%d", woo_id)
                except Exception as inv_exc:
                    logger.debug("[DRY RUN]   Could not check ATUM inventories: %s", inv_exc)
                db.log_item(
                    run_id=run_id,
                    sku=sku,
                    woo_product_id=woo_id,
                    status="dry_run",
                    request_body=payload,
                    prev_regular_price=float(prev["regular_price"]),
                    prev_stock_quantity=int(prev["stock_quantity"]),
                    prev_cost_price=float(prev["cost_price"]),
                    new_regular_price=float(new_vals["regular_price"]),
                    new_stock_quantity=int(new_vals["stock_quantity"]),
                    new_cost_price=float(new_vals["cost_price"]),
                )
                summary["updated"] += 1
                continue

            try:
                # Use variation endpoint if product has a parent
                parent_id = woo_by_id.get(woo_id, {}).get("_parent_id")
                if parent_id:
                    status_code, resp_body = woo_client.update_variation(parent_id, woo_id, payload)
                else:
                    status_code, resp_body = woo_client.update_product(woo_id, payload)

                if 200 <= status_code < 300:
                    logger.info("Updated SKU %s (woo_id=%d) — %d", sku, woo_id, status_code)

                    # Update ATUM Multi-Inventory (Dropship > Jupiter/Boca > Main)
                    try:
                        inventories = woo_client.fetch_inventories(woo_id)
                        chosen = woo_client.select_inventory(inventories)
                        if chosen:
                            inv_id = chosen["id"]
                            inv_name = chosen.get("name", "?")
                            inv_status, inv_resp = woo_client.update_inventory(
                                woo_id, int(inv_id),
                                stock_quantity=int(new_vals["stock_quantity"]),
                                purchase_price=float(new_vals["cost_price"]),
                            )
                            if 200 <= inv_status < 300:
                                logger.info(
                                    "  ATUM inventory '%s' (id=%s) updated for woo_id=%d — manage_stock=true, qty=%s",
                                    inv_name, inv_id, woo_id, new_vals["stock_quantity"],
                                )
                            else:
                                logger.warning(
                                    "  ATUM inventory '%s' update failed for woo_id=%d: HTTP %d",
                                    inv_name, woo_id, inv_status,
                                )
                        else:
                            logger.debug("  No ATUM inventories found for woo_id=%d — skipping inventory update", woo_id)
                    except Exception as inv_exc:
                        logger.warning("  ATUM inventory update error for woo_id=%d: %s", woo_id, inv_exc)

                    db.log_item(
                        run_id=run_id,
                        sku=sku,
                        woo_product_id=woo_id,
                        status="success",
                        request_body=payload,
                        response_body=resp_body,
                        prev_regular_price=float(prev["regular_price"]),
                        prev_stock_quantity=int(prev["stock_quantity"]),
                        prev_cost_price=float(prev["cost_price"]),
                        new_regular_price=float(new_vals["regular_price"]),
                        new_stock_quantity=int(new_vals["stock_quantity"]),
                        new_cost_price=float(new_vals["cost_price"]),
                    )
                    summary["updated"] += 1
                else:
                    error_msg = f"HTTP {status_code}"
                    logger.error("Failed to update SKU %s (woo_id=%d): %s", sku, woo_id, error_msg)
                    db.log_item(
                        run_id=run_id,
                        sku=sku,
                        woo_product_id=woo_id,
                        status="failed",
                        request_body=payload,
                        response_body=resp_body,
                        prev_regular_price=float(prev["regular_price"]),
                        prev_stock_quantity=int(prev["stock_quantity"]),
                        prev_cost_price=float(prev["cost_price"]),
                        new_regular_price=float(new_vals["regular_price"]),
                        new_stock_quantity=int(new_vals["stock_quantity"]),
                        new_cost_price=float(new_vals["cost_price"]),
                        error=error_msg,
                    )
                    summary["failed"] += 1

            except Exception as exc:
                logger.error("Exception updating SKU %s (woo_id=%d): %s", sku, woo_id, exc)
                db.log_item(
                    run_id=run_id,
                    sku=sku,
                    woo_product_id=woo_id,
                    status="failed",
                    request_body=payload,
                    prev_regular_price=float(prev["regular_price"]),
                    prev_stock_quantity=int(prev["stock_quantity"]),
                    prev_cost_price=float(prev["cost_price"]),
                    new_regular_price=float(new_vals["regular_price"]),
                    new_stock_quantity=int(new_vals["stock_quantity"]),
                    new_cost_price=float(new_vals["cost_price"]),
                    error=str(exc),
                )
                summary["failed"] += 1

        # 6. Finalize
        notes = "DRY RUN" if dry_run else None
        db.finish_run(
            run_id=run_id,
            total_ws=summary["total_ws_products"],
            total_woo=summary["total_woo_products"],
            matched=summary["matched"],
            updated=summary["updated"],
            skipped=summary["skipped"],
            missing_in_woo=summary["missing_in_woo"],
            failed=summary["failed"],
            notes=notes,
        )

        logger.info("=== Sync run %s finished ===", run_id)
        logger.info(
            "Summary: ws=%d, woo=%d, matched=%d, updated=%d, skipped=%d, missing=%d, failed=%d",
            summary["total_ws_products"],
            summary["total_woo_products"],
            summary["matched"],
            summary["updated"],
            summary["skipped"],
            summary["missing_in_woo"],
            summary["failed"],
        )

    except Exception:
        logger.exception("Sync run %s failed with exception", run_id)
        try:
            db.finish_run(
                run_id=run_id,
                total_ws=summary["total_ws_products"],
                total_woo=summary["total_woo_products"],
                matched=summary["matched"],
                updated=summary["updated"],
                skipped=summary["skipped"],
                missing_in_woo=summary["missing_in_woo"],
                failed=summary["failed"],
                notes=f"ABORTED with exception",
            )
        except Exception:
            pass
        raise

    finally:
        db.close()

    return summary
