import subprocess
import time
import os
import signal
from typing import Dict, Optional

import pymysql

from src.config import Config
from src.logger import setup_logger

logger = setup_logger("wholescripts_sync.lookup")


def _start_ssh_tunnel() -> Optional[subprocess.Popen]:
    """Start an SSH tunnel in the background. Returns the Popen object."""
    local_port = Config.SSH_LOCAL_PORT
    cmd = [
        "ssh",
        "-i", Config.SSH_KEY_PATH,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ExitOnForwardFailure=yes",
        "-f", "-N",
        "-L", f"{local_port}:127.0.0.1:{Config.MYSQL_PORT}",
        f"{Config.SSH_USER}@{Config.SSH_HOST}",
    ]
    logger.info("Starting SSH tunnel to %s (local port %d)", Config.SSH_HOST, local_port)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise ConnectionError(f"SSH tunnel failed: {proc.stderr.strip()}")
    # Give tunnel a moment to establish
    time.sleep(1)
    logger.info("SSH tunnel established on local port %d", local_port)
    return None  # ssh -f forks itself


def _kill_ssh_tunnel():
    """Kill the SSH tunnel by finding the process on the local port."""
    try:
        result = subprocess.run(
            ["fuser", f"{Config.SSH_LOCAL_PORT}/tcp"],
            capture_output=True, text=True,
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split()
            for pid in pids:
                try:
                    os.kill(int(pid.strip()), signal.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass
            logger.info("SSH tunnel process(es) terminated")
    except FileNotFoundError:
        # fuser not available, try lsof
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{Config.SSH_LOCAL_PORT}"],
                capture_output=True, text=True,
            )
            if result.stdout.strip():
                for pid in result.stdout.strip().split("\n"):
                    try:
                        os.kill(int(pid.strip()), signal.SIGTERM)
                    except (ProcessLookupError, ValueError):
                        pass
        except FileNotFoundError:
            pass


def fetch_sku_lookup() -> Dict[str, int]:
    """Fetch the wholescript_supplier_sku table via SSH tunnel.

    Returns:
        {woo_sku: product_id} mapping (only rows where woo_sku is valid).
        woo_sku is the 9-digit short format (e.g. '300000087').
    """
    tunnel_started = False
    try:
        # Check if tunnel is already up
        try:
            test_conn = pymysql.connect(
                host="127.0.0.1", port=Config.SSH_LOCAL_PORT,
                user=Config.MYSQL_USER, password=Config.MYSQL_PASSWORD,
                database=Config.MYSQL_DATABASE, connect_timeout=3,
            )
            test_conn.close()
            logger.info("SSH tunnel already active on port %d", Config.SSH_LOCAL_PORT)
        except Exception:
            _start_ssh_tunnel()
            tunnel_started = True

        conn = pymysql.connect(
            host="127.0.0.1",
            port=Config.SSH_LOCAL_PORT,
            user=Config.MYSQL_USER,
            password=Config.MYSQL_PASSWORD,
            database=Config.MYSQL_DATABASE,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
        )

        lookup: Dict[str, int] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT product_id, woo_sku FROM wholescript_supplier_sku WHERE woo_sku != '#N/A'"
            )
            for row in cur.fetchall():
                woo_sku = str(row["woo_sku"]).strip()
                product_id = int(row["product_id"])
                if woo_sku:
                    lookup[woo_sku] = product_id

        conn.close()
        logger.info("Loaded %d SKU→product_id mappings from lookup table", len(lookup))
        return lookup

    finally:
        if tunnel_started:
            _kill_ssh_tunnel()
