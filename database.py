"""
database.py — SQLite storage for completed purchases.

SQLite is a file-based database. The whole database lives in one file
(store.db) next to this script. That is enough for local development.
Later we can swap this file for PostgreSQL without changing the API much.
"""

import sqlite3
from pathlib import Path

# Path(__file__) is THIS file (database.py).
# .parent is the project folder. / "store.db" builds store.db in that folder.
# Using a path based on this file avoids "file not found" bugs if you start
# the server from a different working directory.
DB_PATH = Path(__file__).resolve().parent / "store.db"


def get_connection():
    """Open a connection to store.db and return rows as dictionaries."""
    # sqlite3.connect creates the file if it does not exist yet.
    connection = sqlite3.connect(DB_PATH)
    # Default rows are tuples like (1, "Sarah", ...). Row factory makes them
    # look like dicts: {"id": 1, "customer_name": "Sarah", ...}.
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    """Create the purchases table if it is missing. Safe to call on every startup."""
    connection = get_connection()
    # CREATE TABLE IF NOT EXISTS means: create it once, then do nothing later.
    # That way restarting the server never wipes existing purchases.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            price REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()
    connection.close()


def create_purchase(customer_name: str, product: dict) -> int:
    """Insert one purchase and return its new id."""
    connection = get_connection()
    # ? placeholders prevent SQL injection. Never put user text directly into SQL.
    cursor = connection.execute(
        """
        INSERT INTO purchases (customer_name, product_id, product_name, price)
        VALUES (?, ?, ?, ?)
        """,
        (
            customer_name,
            product["id"],
            product["name"],
            product["price"],
        ),
    )
    connection.commit()
    purchase_id = cursor.lastrowid
    connection.close()
    return purchase_id


def list_purchases() -> list[dict]:
    """Return all purchases, newest first."""
    connection = get_connection()
    rows = connection.execute(
        "SELECT id, customer_name, product_id, product_name, price, created_at "
        "FROM purchases ORDER BY id DESC"
    ).fetchall()
    connection.close()
    # sqlite3.Row is not JSON-friendly. dict(row) converts each row for FastAPI.
    return [dict(row) for row in rows]
