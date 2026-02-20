import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

from src.config import Config
from src.logger import setup_logger

logger = setup_logger("wholescripts_sync.db")

# Register UUID adapter
psycopg2.extras.register_uuid()

DDL_SYNC_LOG = """
CREATE TABLE IF NOT EXISTS wholescripts_woo_sync_log (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL,
    sku TEXT,
    woo_product_id BIGINT,
    status TEXT NOT NULL,
    request_body JSONB,
    response_body JSONB,
    prev_regular_price NUMERIC,
    prev_stock_quantity INTEGER,
    prev_cost_price NUMERIC,
    new_regular_price NUMERIC,
    new_stock_quantity INTEGER,
    new_cost_price NUMERIC,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

DDL_SYNC_LOG_IDX_RUN = """
CREATE INDEX IF NOT EXISTS idx_wholescripts_woo_sync_log_run_id
    ON wholescripts_woo_sync_log(run_id);
"""

DDL_SYNC_LOG_IDX_SKU = """
CREATE INDEX IF NOT EXISTS idx_wholescripts_woo_sync_log_sku
    ON wholescripts_woo_sync_log(sku);
"""

DDL_SYNC_RUNS = """
CREATE TABLE IF NOT EXISTS wholescripts_woo_sync_runs (
    run_id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    total_ws_products INTEGER DEFAULT 0,
    total_woo_products INTEGER DEFAULT 0,
    matched INTEGER DEFAULT 0,
    updated INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    missing_in_woo INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    notes TEXT
);
"""


class SyncDB:
    def __init__(self):
        self.conn = None

    def connect(self):
        self.conn = psycopg2.connect(
            dbname=Config.DB_NAME,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            host=Config.DB_HOST,
            port=Config.DB_PORT,
        )
        self.conn.autocommit = True
        logger.info("Connected to Postgres (%s@%s:%s/%s)", Config.DB_USER, Config.DB_HOST, Config.DB_PORT, Config.DB_NAME)

    def ensure_tables(self):
        with self.conn.cursor() as cur:
            cur.execute(DDL_SYNC_LOG)
            cur.execute(DDL_SYNC_LOG_IDX_RUN)
            cur.execute(DDL_SYNC_LOG_IDX_SKU)
            cur.execute(DDL_SYNC_RUNS)
        logger.info("Sync tables ensured")

    def insert_run(self, run_id: uuid.UUID):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO wholescripts_woo_sync_runs (run_id) VALUES (%s)",
                (run_id,),
            )

    def finish_run(
        self,
        run_id: uuid.UUID,
        total_ws: int,
        total_woo: int,
        matched: int,
        updated: int,
        skipped: int,
        missing_in_woo: int,
        failed: int,
        notes: Optional[str] = None,
    ):
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE wholescripts_woo_sync_runs
                   SET finished_at = NOW(),
                       total_ws_products = %s,
                       total_woo_products = %s,
                       matched = %s,
                       updated = %s,
                       skipped = %s,
                       missing_in_woo = %s,
                       failed = %s,
                       notes = %s
                 WHERE run_id = %s""",
                (total_ws, total_woo, matched, updated, skipped, missing_in_woo, failed, notes, run_id),
            )

    def log_item(
        self,
        run_id: uuid.UUID,
        sku: Optional[str],
        woo_product_id: Optional[int],
        status: str,
        request_body: Optional[dict] = None,
        response_body: Optional[dict] = None,
        prev_regular_price: Optional[float] = None,
        prev_stock_quantity: Optional[int] = None,
        prev_cost_price: Optional[float] = None,
        new_regular_price: Optional[float] = None,
        new_stock_quantity: Optional[int] = None,
        new_cost_price: Optional[float] = None,
        error: Optional[str] = None,
    ):
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO wholescripts_woo_sync_log
                   (run_id, sku, woo_product_id, status,
                    request_body, response_body,
                    prev_regular_price, prev_stock_quantity, prev_cost_price,
                    new_regular_price, new_stock_quantity, new_cost_price,
                    error)
                   VALUES (%s,%s,%s,%s, %s,%s, %s,%s,%s, %s,%s,%s, %s)""",
                (
                    run_id, sku, woo_product_id, status,
                    json.dumps(request_body) if request_body else None,
                    json.dumps(response_body) if response_body else None,
                    prev_regular_price, prev_stock_quantity, prev_cost_price,
                    new_regular_price, new_stock_quantity, new_cost_price,
                    error,
                ),
            )

    def close(self):
        if self.conn and not self.conn.closed:
            self.conn.close()
            logger.info("Postgres connection closed")
