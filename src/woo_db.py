"""Direct WooCommerce database access via SSH tunnel.

Reads product data from wp_postmeta to get the true values,
bypassing any WooCommerce REST API caching.
"""
import socket
import threading
import select
import pymysql
import paramiko
from typing import Dict, Optional

from src.config import Config
from src.logger import setup_logger

logger = setup_logger("wholescripts_sync.woo_db")

# Meta keys we care about
_META_KEYS = (
    "_regular_price",
    "_price",
    "_stock",
    "_stock_status",
    "_manage_stock",
    "_op_cost_price",
    "_purchase_price",
    "_atum_manage_stock",
)


class _SSHTunnel:
    """Minimal SSH tunnel using paramiko directly (avoids sshtunnel DSSKey bug)."""

    def __init__(self, ssh_host, ssh_port, ssh_user, ssh_password,
                 remote_host, remote_port, local_port):
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.ssh_password = ssh_password
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.local_port = local_port
        self._client = None
        self._server_sock = None
        self._thread = None
        self._running = False

    def start(self):
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            self.ssh_host,
            port=self.ssh_port,
            username=self.ssh_user,
            password=self.ssh_password,
            timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        transport = self._client.get_transport()

        # Open a direct-tcpip channel through the SSH connection
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("127.0.0.1", self.local_port))
        self._server_sock.listen(1)
        self._server_sock.settimeout(1)
        self._running = True

        def _accept_loop():
            while self._running:
                try:
                    client_sock, _ = self._server_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    chan = transport.open_channel(
                        "direct-tcpip",
                        (self.remote_host, self.remote_port),
                        client_sock.getpeername(),
                    )
                except Exception:
                    client_sock.close()
                    continue
                if chan is None:
                    client_sock.close()
                    continue
                # Bi-directional forwarding
                threading.Thread(
                    target=self._forward, args=(client_sock, chan), daemon=True
                ).start()

        self._thread = threading.Thread(target=_accept_loop, daemon=True)
        self._thread.start()

    @staticmethod
    def _forward(sock, chan):
        while True:
            r, _, _ = select.select([sock, chan], [], [], 1)
            if sock in r:
                data = sock.recv(4096)
                if not data:
                    break
                chan.sendall(data)
            if chan in r:
                data = chan.recv(4096)
                if not data:
                    break
                sock.sendall(data)
        sock.close()
        chan.close()

    def stop(self):
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass


def fetch_product_meta_from_db(product_id: int) -> Optional[Dict[str, str]]:
    """SSH-tunnel into the WooCommerce MariaDB and read wp_postmeta
    for the given product/variation ID.

    Returns a dict like:
        {
            "regular_price": "75.99",
            "stock_quantity": "5",
            "manage_stock": "yes",
            "stock_status": "instock",
            "_op_cost_price": "55.99",
            "purchase_price": "55.99",
        }
    or None on failure.
    """
    if not Config.WOO_SSH_HOST:
        logger.warning("WOO_IP not configured — cannot query WooCommerce DB")
        return None

    tunnel = None
    conn = None
    try:
        tunnel = _SSHTunnel(
            ssh_host=Config.WOO_SSH_HOST,
            ssh_port=Config.WOO_SSH_PORT,
            ssh_user=Config.WOO_SSH_USER,
            ssh_password=Config.WOO_SSH_PASSWORD,
            remote_host=Config.WOO_DB_HOST,
            remote_port=Config.WOO_DB_PORT,
            local_port=Config.WOO_SSH_LOCAL_PORT,
        )
        tunnel.start()
        logger.info(
            "SSH tunnel to WooCommerce DB established (local port %d)",
            Config.WOO_SSH_LOCAL_PORT,
        )

        conn = pymysql.connect(
            host="127.0.0.1",
            port=Config.WOO_SSH_LOCAL_PORT,
            user=Config.WOO_DB_USER,
            password=Config.WOO_DB_PASSWORD,
            database=Config.WOO_DB_NAME,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
        )

        placeholders = ", ".join(["%s"] * len(_META_KEYS))
        sql = f"""
            SELECT meta_key, meta_value
            FROM wp_postmeta
            WHERE post_id = %s
              AND meta_key IN ({placeholders})
        """
        params = [product_id] + list(_META_KEYS)

        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        raw = {row["meta_key"]: row["meta_value"] for row in rows}

        # Also pull canonical purchase_price from wp_atum_product_data
        atum_purchase_price = None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT purchase_price FROM wp_atum_product_data WHERE product_id = %s",
                    (product_id,),
                )
                atum_row = cur.fetchone()
                if atum_row and atum_row["purchase_price"] is not None:
                    atum_purchase_price = str(atum_row["purchase_price"])
        except Exception:
            pass

        result = {
            "regular_price": raw.get("_regular_price", "0.00"),
            "stock_quantity": raw.get("_stock", "0"),
            "manage_stock": raw.get("_manage_stock", "no"),
            "stock_status": raw.get("_stock_status", ""),
            "_op_cost_price": raw.get("_op_cost_price", "0.00"),
            "purchase_price": atum_purchase_price or raw.get("_purchase_price", "0.00"),
            "_atum_manage_stock": raw.get("_atum_manage_stock", ""),
        }

        logger.info("Fetched DB meta for product %d: %s", product_id, result)
        return result

    except Exception as exc:
        logger.error("Failed to query WooCommerce DB for product %d: %s", product_id, exc)
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        if tunnel:
            try:
                tunnel.stop()
            except Exception:
                pass
