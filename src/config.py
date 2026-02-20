import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


class Config:
    # Postgres
    DB_NAME = os.getenv("DB_NAME", "pos_prod")
    DB_USER = os.getenv("DB_USER", "pos_produser")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT = int(os.getenv("DB_PORT", "5432"))

    # Wholescripts
    WS_USERNAME = os.getenv("WHOLESCRIPTS_API_USERNAME", "")
    WS_PASSWORD = os.getenv("WHOLESCRIPTS_API_PASSWORD", "")
    WS_API_URL = os.getenv("WHOLESCRIPT_API_URL", "https://testservices.wholescripts.com/api")

    # WooCommerce
    WOO_API_URL = os.getenv("WOO_API_URL", "https://store.doctorsstudio.com")
    WOO_CONSUMER_KEY = os.getenv("WOO_CONSUMER_KEY", "")
    WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")
    WOO_API_VERSION = os.getenv("WOOCOMMERCE_API_VERSION", "wc/v3")

    # Meta keys
    WOO_COST_META_KEY = os.getenv("WOO_COST_META_KEY", "_op_cost_price")

    # MySQL lookup DB (remote VM via SSH tunnel)
    MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "doctorsstudio")

    # SSH tunnel to remote VM
    SSH_HOST = os.getenv("SSH_HOST", "34.148.82.199")
    SSH_USER = os.getenv("SSH_USER", "joy")
    SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", str(Path(__file__).resolve().parent.parent / "id_servicemenuserver_new"))
    SSH_LOCAL_PORT = int(os.getenv("SSH_LOCAL_PORT", "33066"))

    # Runtime
    DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

    # Lock file
    LOCK_FILE = Path(__file__).resolve().parent.parent / "wholescripts_sync.pid"

    @classmethod
    def woo_base_url(cls):
        return f"{cls.WOO_API_URL}/wp-json/{cls.WOO_API_VERSION}"

    @classmethod
    def validate(cls):
        errors = []
        if not cls.WS_USERNAME:
            errors.append("WHOLESCRIPTS_API_USERNAME is required")
        if not cls.WS_PASSWORD:
            errors.append("WHOLESCRIPTS_API_PASSWORD is required")
        if not cls.WOO_CONSUMER_KEY:
            errors.append("WOO_CONSUMER_KEY is required")
        if not cls.WOO_CONSUMER_SECRET:
            errors.append("WOO_CONSUMER_SECRET is required")
        if not cls.DB_PASSWORD:
            errors.append("DB_PASSWORD is required")
        if errors:
            raise EnvironmentError("Missing config:\n  " + "\n  ".join(errors))
