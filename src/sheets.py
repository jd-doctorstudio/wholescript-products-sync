"""Google Sheets integration — writes sync results to a shared Google Sheet.

The sheet is cleared and rebuilt on every run so ops/QA always sees the
latest snapshot.  Layout (single worksheet, flat table):

  Row 1     : Column headers (frozen, auto-filtered)
  Row 2+    : One row per product — filterable, sortable, searchable

Column groups (color-coded in header):
  Identity (grey)  : Run ID, Product Name, SKU, Woo ID, Match Status
  Price (blue)     : WooCommerce/Wholescripts Price Prev/Now, Changed, Mismatch
  Stock (green)    : WooCommerce/Wholescripts Stock Prev/Now, Changed, Mismatch
  Cost (purple)    : WooCommerce/Wholescripts Cost Prev/Now, Changed, Mismatch
  Result (dark)    : Overall Status, Action, Error

No summary block — row 1 is the header, row 2+ is data.
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

from src.config import Config
from src.logger import setup_logger

logger = setup_logger("wholescripts_sync.sheets")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Column headers (row 10) ─────────────────────────────────────────
#    Index:  A=0  B=1  C=2  D=3  E=4
#            F=5  G=6  H=7  I=8  J=9  K=10
#            L=11 M=12 N=13 O=14 P=15 Q=16
#            R=17 S=18 T=19 U=20 V=21 W=22
#            X=23 Y=24 Z=25
HEADERS = [
    # Identity (A-E)
    "Run ID",
    "Product Name",
    "SKU",
    "Woo ID",
    "Match Status",
    # Price (F-K)
    "[WOO] Price Prev",
    "[WOO] Price Now",
    "[WS] Price Prev",
    "[WS] Price Now",
    "Price Changed",
    "Price Mismatch",
    # Stock (L-Q)
    "[WOO] Stock Prev",
    "[WOO] Stock Now",
    "[WS] Stock Prev",
    "[WS] Stock Now",
    "Stock Changed",
    "Stock Mismatch",
    # Cost (R-W)
    "[WOO] Cost Prev",
    "[WOO] Cost Now",
    "[WS] Cost Prev",
    "[WS] Cost Now",
    "Cost Changed",
    "Cost Mismatch",
    # Result (X-Z)
    "Overall Status",
    "Action",
    "Error",
]

HEADER_ROW = 1     # 1-indexed row for column headers
DATA_START = 2     # 1-indexed first data row

# Column group ranges (header row, for background coloring)
# Identity=A-E, Price=F-K, Stock=L-Q, Cost=R-W, Result=X-Z
COL_GROUPS = {
    "identity": ("A", "E"),   # dark grey
    "price":    ("F", "K"),   # blue tint
    "stock":    ("L", "Q"),   # green tint
    "cost":     ("R", "W"),   # purple tint
    "result":   ("X", "Z"),   # dark
}

# Background colors per group (for header row)
GROUP_HEADER_COLORS = {
    "identity": {"red": 0.20, "green": 0.20, "blue": 0.20},   # charcoal
    "price":    {"red": 0.10, "green": 0.27, "blue": 0.53},   # navy blue
    "stock":    {"red": 0.10, "green": 0.40, "blue": 0.20},   # forest green
    "cost":     {"red": 0.35, "green": 0.15, "blue": 0.50},   # deep purple
    "result":   {"red": 0.25, "green": 0.25, "blue": 0.25},   # dark grey
}

# Light tint for data rows per group
GROUP_DATA_COLORS = {
    "identity": {"red": 0.97, "green": 0.97, "blue": 0.97},   # near white
    "price":    {"red": 0.92, "green": 0.95, "blue": 1.00},   # light blue
    "stock":    {"red": 0.92, "green": 1.00, "blue": 0.93},   # light green
    "cost":     {"red": 0.96, "green": 0.92, "blue": 1.00},   # light purple
    "result":   {"red": 0.95, "green": 0.95, "blue": 0.95},   # light grey
}


# ── Helpers ──────────────────────────────────────────────────────────

def _detect_env_label(url: str, service: str) -> str:
    if service == "ws":
        return "Wholescripts TEST" if "testservices" in url else "Wholescripts PROD"
    return "WooCommerce KINSTA STAGING" if "kinsta" in url else "WooCommerce PROD"


def _get_client() -> gspread.Client:
    key_path = Config.GOOGLE_SERVICE_ACCOUNT_FILE
    if not Path(key_path).is_absolute():
        key_path = str(Path(__file__).resolve().parent.parent / key_path)
    creds = Credentials.from_service_account_file(key_path, scopes=SCOPES)
    return gspread.authorize(creds)


def _p(value) -> str:
    """Format price (plain number, no $)."""
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.2f}"
    except (ValueError, TypeError):
        return str(value)


def _s(value) -> str:
    """Format stock quantity."""
    if value is None or value == "":
        return ""
    try:
        return str(int(value))
    except (ValueError, TypeError):
        return str(value)


def _flag(a, b) -> str:
    """YES if a != b, NO if equal, blank if both empty."""
    if a == "" and b == "":
        return ""
    try:
        return "YES" if str(a).strip() != str(b).strip() else "NO"
    except Exception:
        return ""


def _overall_status(match_status: str, action: str,
                    price_mm: str, stock_mm: str, cost_mm: str) -> str:
    """Compute a single Overall Status for quick scanning."""
    if action == "FAILED":
        return "FAILED"
    if match_status == "NOT_IN_WOO":
        return "MISSING"
    if price_mm == "YES" or stock_mm == "YES" or cost_mm == "YES":
        return "MISMATCH"
    if action in ("UPDATED", "DRY_RUN"):
        return "SYNCED"
    return "OK"


def _ws_name(ws_by_sku: dict, sku: str) -> str:
    return ws_by_sku.get(sku, {}).get("product_name", "")


def _col_letter(index: int) -> str:
    """0-indexed column number to letter(s). 0=A, 25=Z, 26=AA."""
    result = ""
    while True:
        result = chr(ord("A") + index % 26) + result
        index = index // 26 - 1
        if index < 0:
            break
    return result


# ── Full sheet reset ─────────────────────────────────────────────────

def _full_clear(sh, ws) -> None:
    """Nuke everything: values, formatting, conditional format rules, filters."""
    sheet_id = ws._properties["sheetId"]

    # 1) Clear all cell values
    ws.clear()

    # 2) Build batch requests
    requests = []

    # Reset all cell formatting to default
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id},
            "cell": {
                "userEnteredFormat": {},
            },
            "fields": "userEnteredFormat",
        }
    })

    # Remove basic filter if present
    requests.append({
        "clearBasicFilter": {
            "sheetId": sheet_id,
        }
    })

    # Unfreeze rows and columns
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {
                    "frozenRowCount": 0,
                    "frozenColumnCount": 0,
                },
            },
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
        }
    })

    # 3) Delete all conditional format rules
    # We need to fetch existing rules first, then delete them by index (reverse order)
    try:
        sheet_meta = sh.fetch_sheet_metadata()
        sheets = sheet_meta.get("sheets", [])
        for s in sheets:
            if s["properties"]["sheetId"] == sheet_id:
                cf_rules = s.get("conditionalFormats", [])
                # Delete in reverse order so indices stay valid
                for i in range(len(cf_rules) - 1, -1, -1):
                    requests.append({
                        "deleteConditionalFormatRule": {
                            "sheetId": sheet_id,
                            "index": i,
                        }
                    })
                break
    except Exception:
        pass  # if we can't read metadata, skip CF cleanup

    if requests:
        sh.batch_update({"requests": requests})


# ── Main publish function ────────────────────────────────────────────

def publish_sync_results(
    updates: List[dict],
    skipped: List[dict],
    missing_in_woo: List[dict],
    failed: List[dict],
    summary: dict,
    ws_by_sku: Dict[str, dict],
    woo_by_id: Dict[int, dict],
    ws_prev_snapshot: Optional[Dict[str, dict]] = None,
    dry_run: bool = False,
) -> None:
    """Clear the sheet and write the latest sync results.

    Column semantics:
      [WOO] Prev  = WooCommerce value BEFORE this run
      [WOO] Now   = dry-run: same as Prev (no sync happened)
                     live: the synced value (== WS current)
      [WS]  Prev  = Wholescripts value from PREVIOUS run (snapshot)
      [WS]  Now   = Wholescripts value from current API call
    """
    sheet_id = Config.GOOGLE_SHEET_ID
    if not sheet_id:
        logger.warning("GOOGLE_SHEET_ID not set — skipping Google Sheets update")
        return

    ws_prev = ws_prev_snapshot or {}

    try:
        gc = _get_client()
        sh = gc.open_by_key(sheet_id)
        worksheet = sh.sheet1

        now_et = datetime.now(timezone(timedelta(hours=-5)))
        run_id = now_et.strftime("%Y-%m-%d %I:%M %p ET")
        ws_env = _detect_env_label(Config.WS_API_URL, "ws")
        woo_env = _detect_env_label(Config.WOO_API_URL, "woo")
        ncols = len(HEADERS)

        # Row 1: column headers
        header_row = list(HEADERS)

        # ── Product data rows ──────────────────────────────────────
        data_rows = []
        action_label = "DRY_RUN" if dry_run else "UPDATED"

        # Updated / dry-run items
        for item in updates:
            prev = item.get("prev", {})       # WOO state before sync
            new_ = item.get("new_vals", {})    # Target values (from WS)
            sku = item["sku"]
            name = item.get("ws_name", "") or _ws_name(ws_by_sku, sku)

            # WOO Prev = current WOO state
            wc_pp = _p(prev.get("regular_price"))
            wc_sp = _s(prev.get("stock_quantity"))
            wc_cp = _p(prev.get("cost_price"))

            # WOO Now = dry-run: unchanged (same as prev), live: synced value
            if dry_run:
                wc_pn, wc_sn, wc_cn = wc_pp, wc_sp, wc_cp
            else:
                wc_pn = _p(new_.get("regular_price"))
                wc_sn = _s(new_.get("stock_quantity"))
                wc_cn = _p(new_.get("cost_price"))

            # WS Prev = from snapshot (last run)
            ws_snap = ws_prev.get(sku, {})
            ws_pp = _p(ws_snap.get("retail_price")) if ws_snap else ""
            ws_sp = _s(ws_snap.get("qty")) if ws_snap else ""
            ws_cp = _p(ws_snap.get("cost_price")) if ws_snap else ""

            # WS Now = current API data
            ws_cur = ws_by_sku.get(sku, {})
            ws_pn = _p(ws_cur.get("retail_price"))
            ws_sn = _s(ws_cur.get("qty"))
            ws_cn = _p(ws_cur.get("cost_price"))

            p_chg = _flag(wc_pp, wc_pn)
            p_mm  = _flag(wc_pn, ws_pn)
            s_chg = _flag(wc_sp, wc_sn)
            s_mm  = _flag(wc_sn, ws_sn)
            c_chg = _flag(wc_cp, wc_cn)
            c_mm  = _flag(wc_cn, ws_cn)

            data_rows.append([
                run_id, name, sku, str(item["woo_product_id"]),
                "MATCHED",
                wc_pp, wc_pn, ws_pp, ws_pn, p_chg, p_mm,
                wc_sp, wc_sn, ws_sp, ws_sn, s_chg, s_mm,
                wc_cp, wc_cn, ws_cp, ws_cn, c_chg, c_mm,
                _overall_status("MATCHED", action_label, p_mm, s_mm, c_mm),
                action_label, "",
            ])

        # Failed items (sync was attempted but failed — WOO unchanged)
        for item in failed:
            prev = item.get("prev", {})
            sku = item.get("sku", "")
            name = item.get("ws_name", "") or _ws_name(ws_by_sku, sku)

            wc_pp = _p(prev.get("regular_price"))
            wc_pn = wc_pp  # failed = no change applied
            wc_sp = _s(prev.get("stock_quantity"))
            wc_sn = wc_sp
            wc_cp = _p(prev.get("cost_price"))
            wc_cn = wc_cp

            ws_snap = ws_prev.get(sku, {})
            ws_pp = _p(ws_snap.get("retail_price")) if ws_snap else ""
            ws_sp = _s(ws_snap.get("qty")) if ws_snap else ""
            ws_cp = _p(ws_snap.get("cost_price")) if ws_snap else ""

            ws_cur = ws_by_sku.get(sku, {})
            ws_pn = _p(ws_cur.get("retail_price"))
            ws_sn = _s(ws_cur.get("qty"))
            ws_cn = _p(ws_cur.get("cost_price"))

            p_mm = _flag(wc_pn, ws_pn)
            s_mm = _flag(wc_sn, ws_sn)
            c_mm = _flag(wc_cn, ws_cn)

            data_rows.append([
                run_id, name, sku, str(item.get("woo_product_id", "")),
                "MATCHED",
                wc_pp, wc_pn, ws_pp, ws_pn, "NO", p_mm,
                wc_sp, wc_sn, ws_sp, ws_sn, "NO", s_mm,
                wc_cp, wc_cn, ws_cp, ws_cn, "NO", c_mm,
                "FAILED", "FAILED", str(item.get("error", "")),
            ])

        # Skipped (no change needed — WOO already matches WS)
        for item in skipped:
            woo_id = item.get("woo_product_id", "")
            sku = item["sku"]
            woo = woo_by_id.get(woo_id, {}) if woo_id else {}
            name = woo.get("name", "") or _ws_name(ws_by_sku, sku)

            wc_p = _p(woo.get("regular_price"))
            wc_s = _s(woo.get("stock_quantity"))
            wc_c = _p(woo.get("cost_price"))

            ws_snap = ws_prev.get(sku, {})
            ws_pp = _p(ws_snap.get("retail_price")) if ws_snap else ""
            ws_sp = _s(ws_snap.get("qty")) if ws_snap else ""
            ws_cp = _p(ws_snap.get("cost_price")) if ws_snap else ""

            ws_cur = ws_by_sku.get(sku, {})
            ws_pn = _p(ws_cur.get("retail_price"))
            ws_sn = _s(ws_cur.get("qty"))
            ws_cn = _p(ws_cur.get("cost_price"))

            data_rows.append([
                run_id, name, sku, str(woo_id),
                "MATCHED",
                wc_p, wc_p, ws_pp, ws_pn, "NO", "NO",
                wc_s, wc_s, ws_sp, ws_sn, "NO", "NO",
                wc_c, wc_c, ws_cp, ws_cn, "NO", "NO",
                "OK", "SKIPPED", "",
            ])

        # Missing in WooCommerce
        for item in missing_in_woo:
            sku = item["sku"]
            wsd = ws_by_sku.get(sku, {})
            name = item.get("ws_name", "") or wsd.get("product_name", "")

            ws_snap = ws_prev.get(sku, {})
            ws_pp = _p(ws_snap.get("retail_price")) if ws_snap else ""
            ws_sp = _s(ws_snap.get("qty")) if ws_snap else ""
            ws_cp = _p(ws_snap.get("cost_price")) if ws_snap else ""

            ws_pn = _p(wsd.get("retail_price"))
            ws_sn = _s(wsd.get("qty"))
            ws_cn = _p(wsd.get("cost_price"))

            data_rows.append([
                run_id, name, sku, "",
                "NOT_IN_WOO",
                "", "", ws_pp, ws_pn, "", "",
                "", "", ws_sp, ws_sn, "", "",
                "", "", ws_cp, ws_cn, "", "",
                "MISSING", "MISSING", "",
            ])

        # ── Assemble ────────────────────────────────────────────────
        all_rows = [header_row] + data_rows

        # Stringify everything
        all_rows = [[str(c) if c is not None else "" for c in row] for row in all_rows]

        _full_clear(sh, worksheet)
        worksheet.update(values=all_rows, range_name="A1",
                         value_input_option="USER_ENTERED")

        # ── Formatting ──────────────────────────────────────────────
        last_col = _col_letter(ncols - 1)
        _apply_formatting(worksheet, total_rows=len(all_rows), ncols=ncols,
                          last_col=last_col, data_start=DATA_START)

        # Auto-filter on data table
        try:
            worksheet.set_basic_filter(
                f"A{HEADER_ROW}:{last_col}{len(all_rows)}")
        except Exception:
            pass  # non-critical

        # Conditional formatting (color rules)
        _apply_conditional_formatting(
            sh, worksheet, data_start=DATA_START,
            total_rows=len(all_rows), ncols=ncols)

        # Auto-resize all columns to fit content
        _auto_resize_columns(sh, worksheet, ncols=ncols)

        logger.info(
            "Google Sheet updated: %d product rows written to %s",
            len(data_rows), sheet_id,
        )

    except Exception as exc:
        import traceback
        logger.error("Failed to update Google Sheet: %s\n%s", exc, traceback.format_exc())


# ── Cell formatting ──────────────────────────────────────────────────

def _apply_formatting(ws, total_rows: int, ncols: int, last_col: str,
                      data_start: int) -> None:
    """Apply styling: color-grouped headers, data tints, freeze."""
    try:
        # ── Header row: color per group ─────────────────────────────
        for group, (start_col, end_col) in COL_GROUPS.items():
            rng = f"{start_col}{HEADER_ROW}:{end_col}{HEADER_ROW}"
            ws.format(rng, {
                "backgroundColor": GROUP_HEADER_COLORS[group],
                "textFormat": {"bold": True, "fontSize": 10,
                               "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "horizontalAlignment": "CENTER",
            })

        # ── Data rows ───────────────────────────────────────────────
        if total_rows >= data_start:
            end = total_rows

            # Group background tints
            for group, (start_col, end_col) in COL_GROUPS.items():
                ws.format(f"{start_col}{data_start}:{end_col}{end}", {
                    "backgroundColor": GROUP_DATA_COLORS[group],
                    "textFormat": {"fontSize": 10},
                })

            # Price columns: number format  (F-I = cols 5-8)
            for c in ["F", "G", "H", "I"]:
                ws.format(f"{c}{data_start}:{c}{end}", {
                    "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"},
                })

            # Cost columns: number format  (R-U = cols 17-20)
            for c in ["R", "S", "T", "U"]:
                ws.format(f"{c}{data_start}:{c}{end}", {
                    "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"},
                })

            # Stock columns: integer  (L-O = cols 11-14)
            for c in ["L", "M", "N", "O"]:
                ws.format(f"{c}{data_start}:{c}{end}", {
                    "numberFormat": {"type": "NUMBER", "pattern": "#,##0"},
                })

            # Flag + status columns: center  (J,K,P,Q,V,W,X)
            for c in ["J", "K", "P", "Q", "V", "W", "X"]:
                ws.format(f"{c}{data_start}:{c}{end}", {
                    "horizontalAlignment": "CENTER",
                    "textFormat": {"bold": True, "fontSize": 10},
                })

            # Action column (Y): center
            ws.format(f"Y{data_start}:Y{end}", {
                "horizontalAlignment": "CENTER",
            })

        # Freeze header row + first 5 cols (Run ID → Match Status)
        ws.freeze(rows=HEADER_ROW, cols=5)

    except Exception as fmt_exc:
        logger.debug("Sheet formatting skipped: %s", fmt_exc)


# ── Conditional formatting (Sheets API batch) ────────────────────────

def _apply_conditional_formatting(sh, ws, data_start: int,
                                  total_rows: int, ncols: int) -> None:
    """Add color rules for YES/NO flags, Overall Status, and Match Status."""
    try:
        sheet_id = ws._properties["sheetId"]
        last_row = total_rows
        # data range rows (0-indexed for API)
        start_row_idx = data_start - 1
        end_row_idx = last_row

        rules = []

        # Helper to build a boolean condition rule
        def _bool_rule(col_idx: int, value: str, bg_color: dict):
            return {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": sheet_id,
                            "startRowIndex": start_row_idx,
                            "endRowIndex": end_row_idx,
                            "startColumnIndex": col_idx,
                            "endColumnIndex": col_idx + 1,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": value}],
                            },
                            "format": {"backgroundColor": bg_color},
                        },
                    },
                    "index": 0,
                }
            }

        # Helper for full-row highlight based on a column value
        def _row_rule(col_idx: int, value: str, bg_color: dict):
            return {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": sheet_id,
                            "startRowIndex": start_row_idx,
                            "endRowIndex": end_row_idx,
                            "startColumnIndex": 0,
                            "endColumnIndex": ncols,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue":
                                    f'=${ _col_letter(col_idx) }{ data_start }="{value}"'}],
                            },
                            "format": {"backgroundColor": bg_color},
                        },
                    },
                    "index": 0,
                }
            }

        # ── Flag columns: YES = yellow, NO = light green ─────────
        # Price Changed=J(9), Price Mismatch=K(10)
        # Stock Changed=P(15), Stock Mismatch=Q(16)
        # Cost Changed=V(21), Cost Mismatch=W(22)
        yellow = {"red": 1.0, "green": 0.95, "blue": 0.6}
        red_light = {"red": 1.0, "green": 0.80, "blue": 0.80}
        green_light = {"red": 0.85, "green": 0.95, "blue": 0.85}

        # Changed = YES → yellow
        for col_idx in [9, 15, 21]:   # J, P, V
            rules.append(_bool_rule(col_idx, "YES", yellow))
            rules.append(_bool_rule(col_idx, "NO", green_light))

        # Mismatch = YES → red
        for col_idx in [10, 16, 22]:  # K, Q, W
            rules.append(_bool_rule(col_idx, "YES", red_light))
            rules.append(_bool_rule(col_idx, "NO", green_light))

        # ── Overall Status column (X = 23) ───────────────────────
        overall_col = 23
        rules.append(_bool_rule(overall_col, "SYNCED",
                                {"red": 0.72, "green": 0.88, "blue": 0.72}))    # green
        rules.append(_bool_rule(overall_col, "OK",
                                {"red": 0.85, "green": 0.95, "blue": 0.85}))    # light green
        rules.append(_bool_rule(overall_col, "MISMATCH",
                                {"red": 1.0, "green": 0.85, "blue": 0.60}))     # orange
        rules.append(_bool_rule(overall_col, "FAILED",
                                {"red": 1.0, "green": 0.70, "blue": 0.70}))     # red
        rules.append(_bool_rule(overall_col, "MISSING",
                                {"red": 1.0, "green": 0.90, "blue": 0.70}))     # orange-light

        # ── Match Status (E = 4): NOT_IN_WOO → orange row tint ──
        rules.append(_row_rule(4, "NOT_IN_WOO",
                               {"red": 1.0, "green": 0.96, "blue": 0.90}))

        # ── Cell-level mismatch: highlight when WOO Now ≠ WS Now ─────
        # Red = real mismatch (systems disagree AFTER sync).
        # Prev→Now change is expected (sync did its job), so NOT red.
        #
        # Mismatch pairs (0-indexed):
        #   Price: [WOO] Now=G(6) vs [WS] Now=I(8)
        #   Stock: [WOO] Now=M(12) vs [WS] Now=O(14)
        #   Cost:  [WOO] Now=S(18) vs [WS] Now=U(20)
        diff_red = {"red": 1.0, "green": 0.75, "blue": 0.75}

        # (woo_now_idx, ws_now_idx, all Prev+Now cols to highlight)
        mismatch_groups = [
            (6, 8, [5, 6, 7, 8]),       # Price: F,G,H,I
            (12, 14, [11, 12, 13, 14]),  # Stock: L,M,N,O
            (18, 20, [17, 18, 19, 20]),  # Cost:  R,S,T,U
        ]

        for woo_now, ws_now, highlight_cols in mismatch_groups:
            woo_col = _col_letter(woo_now)
            ws_col = _col_letter(ws_now)
            # Formula: WOO Now ≠ WS Now AND both non-empty
            formula = (f'=AND(${woo_col}{data_start}<>"", '
                       f'${ws_col}{data_start}<>"", '
                       f'${woo_col}{data_start}<>${ws_col}{data_start})')
            for target_idx in highlight_cols:
                rules.append({
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{
                                "sheetId": sheet_id,
                                "startRowIndex": start_row_idx,
                                "endRowIndex": end_row_idx,
                                "startColumnIndex": target_idx,
                                "endColumnIndex": target_idx + 1,
                            }],
                            "booleanRule": {
                                "condition": {
                                    "type": "CUSTOM_FORMULA",
                                    "values": [{"userEnteredValue": formula}],
                                },
                                "format": {
                                    "backgroundColor": diff_red,
                                    "textFormat": {"bold": True},
                                },
                            },
                        },
                        "index": 0,
                    }
                })

        # Batch apply
        if rules:
            sh.batch_update({"requests": rules})

    except Exception as cf_exc:
        logger.debug("Conditional formatting skipped: %s", cf_exc)


# ── Auto-resize columns ─────────────────────────────────────────────

def _auto_resize_columns(sh, ws, ncols: int) -> None:
    """Auto-resize columns then add padding so nothing is clipped."""
    try:
        sheet_id = ws._properties["sheetId"]
        # Step 1: auto-resize to fit content
        sh.batch_update({"requests": [{
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": ncols,
                }
            }
        }]})
        # Step 2: read current widths and add 30px padding
        meta = sh.fetch_sheet_metadata()
        for s in meta.get("sheets", []):
            if s["properties"]["sheetId"] == sheet_id:
                cols = s.get("data", [{}])[0].get("columnMetadata", [])
                pad_requests = []
                for i, col in enumerate(cols[:ncols]):
                    cur = col.get("pixelSize", 100)
                    pad_requests.append({
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": i,
                                "endIndex": i + 1,
                            },
                            "properties": {"pixelSize": cur + 50},
                            "fields": "pixelSize",
                        }
                    })
                if pad_requests:
                    sh.batch_update({"requests": pad_requests})
                break
    except Exception as ar_exc:
        logger.debug("Column auto-resize skipped: %s", ar_exc)
