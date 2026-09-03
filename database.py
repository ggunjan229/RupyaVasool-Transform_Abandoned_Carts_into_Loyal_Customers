"""
database.py — SQLite storage for completed purchases.
database.py — SQLite storage for completed purchases and pending carts.
SQLite is a file-based database. The whole database lives in one file
(store.db) next to this script. That is enough for local development.



# the server from a different working directory.
DB_PATH = Path(__file__).resolve().parent / "store.db"
# Worker and API can open the file at the same time. timeout waits instead of
# crashing with "database is locked".
DB_TIMEOUT_SECONDS = 10
def get_connection():
    """Open a connection to store.db and return rows as dictionaries."""
    # sqlite3.connect creates the file if it does not exist yet.
    connection = sqlite3.connect(DB_PATH)
    connection = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECONDS)
    # Default rows are tuples like (1, "Sarah", ...). Row factory makes them
    # look like dicts: {"id": 1, "customer_name": "Sarah", ...}.
    connection.row_factory = sqlite3.Row



def init_db():
    """Create the purchases table if it is missing. Safe to call on every startup."""
    """Create tables if they are missing. Safe to call on every startup."""
    connection = get_connection()
    # CREATE TABLE IF NOT EXISTS means: create it once, then do nothing later.
    # That way restarting the server never wipes existing purchases.
    # That way restarting the server never wipes existing rows.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS purchases (



        )
        """
    )
    # Pending carts are unfinished checkouts. The worker looks at this table.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS carts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            price REAL NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()
    connection.close()



    connection.close()
    # sqlite3.Row is not JSON-friendly. dict(row) converts each row for FastAPI.
    return [dict(row) for row in rows]
def create_cart(customer_name: str, product: dict, age_minutes: int = 0) -> int:
    """Insert a Pending cart. age_minutes backdates created_at for worker tests."""
    connection = get_connection()
    # datetime('now', '-6 minutes') is computed by SQLite in UTC, same clock
    # the worker uses. Mixing Python local time here would cause "always empty"
    # or "always stale" bugs.
    cursor = connection.execute(
        """
        INSERT INTO carts (
            customer_name, product_id, product_name, price, status, created_at
        )
        VALUES (?, ?, ?, ?, 'Pending', datetime('now', ?))
        """,
        (
            customer_name,
            product["id"],
            product["name"],
            product["price"],
            f"-{age_minutes} minutes",
        ),
    )
    connection.commit()
    cart_id = cursor.lastrowid
    connection.close()
    return cart_id
def list_carts() -> list[dict]:
    """Return all carts, newest first."""
    connection = get_connection()
    rows = connection.execute(
        "SELECT id, customer_name, product_id, product_name, price, status, created_at "
        "FROM carts ORDER BY id DESC"
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]
def list_stale_pending_carts(minutes: int = 5) -> list[dict]:
    """Pending carts whose created_at is older than `minutes` minutes."""
    connection = get_connection()
    # status must match exactly: 'Pending'. datetime('now', '-5 minutes') is UTC.
    rows = connection.execute(
        """
        SELECT id, customer_name, product_id, product_name, price, status, created_at
        FROM carts
        WHERE status = 'Pending'
          AND created_at <= datetime('now', ?)
        ORDER BY id ASC
        """,
        (f"-{minutes} minutes",),
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]