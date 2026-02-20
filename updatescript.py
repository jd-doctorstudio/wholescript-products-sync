#!/var/www/wholescripts-sync/venv/bin/python3
"""Wholescripts → WooCommerce nightly product sync.

Usage:
    python3 updatescript.py              # Normal run
    python3 updatescript.py --dry-run    # Preview without updating Woo
"""
import os
import sys
import argparse
import atexit

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import Config
from src.logger import setup_logger
from src.sync import run_sync

logger = setup_logger("wholescripts_sync.main")


def acquire_lock():
    """Create a PID lock file to prevent overlapping runs."""
    lock_path = Config.LOCK_FILE
    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text().strip())
            # Check if the old process is still running
            os.kill(old_pid, 0)
            logger.error("Another sync is already running (PID %d). Exiting.", old_pid)
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            # Process is gone or PID file is corrupt — safe to proceed
            logger.warning("Stale lock file found (PID gone). Removing.")
            lock_path.unlink(missing_ok=True)
        except PermissionError:
            logger.error("Cannot check lock PID. Exiting to be safe.")
            sys.exit(1)

    lock_path.write_text(str(os.getpid()))
    atexit.register(lambda: lock_path.unlink(missing_ok=True))
    logger.info("Lock acquired (PID %d)", os.getpid())


def main():
    parser = argparse.ArgumentParser(description="Wholescripts → WooCommerce sync")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without updating WooCommerce")
    args = parser.parse_args()

    try:
        Config.validate()
    except EnvironmentError as exc:
        logger.error(str(exc))
        sys.exit(1)

    acquire_lock()

    dry_run = args.dry_run or Config.DRY_RUN
    if dry_run:
        logger.info("*** DRY RUN MODE — no Woo updates will be made ***")

    try:
        summary = run_sync(dry_run=dry_run)
        if summary.get("failed", 0) > 0:
            logger.warning("Run completed with %d failures", summary["failed"])
            sys.exit(2)
        logger.info("Run completed successfully")
    except Exception:
        logger.exception("Sync failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
