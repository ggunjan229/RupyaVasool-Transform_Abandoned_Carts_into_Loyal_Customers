# database.py — SQLite storage for completed purchases and pending carts.

import sqlite3
from pathlib import Path
from typing import List

DB_PATH = Path(__file__).resolve().parent / "store.db"
DB_TIMEOUT_SECONDS = 10

def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECONDS, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection

def init_db() -> None:
    connection = get_connection()
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS purchases (
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

def create_purchase(customer_name: str, product, **kwargs) -> int:
    if isinstance(product, dict):
        product_id = product.get("id")
        product_name = product.get("name")
        price = product.get("price")
    else:
        product_id = int(product)
        product_name = f"product-{product_id}"
        price = 0.0

    connection = get_connection()
    cursor = connection.execute(
        """
        INSERT INTO purchases (customer_name, product_id, product_name, price, status)
        VALUES (?, ?, ?, ?, 'Completed')
        """,
        (customer_name, product_id, product_name, price),
    )
    connection.commit()
    purchase_id = cursor.lastrowid
    connection.close()
    return purchase_id

def list_purchases() -> List[dict]:
    connection = get_connection()
    rows = connection.execute(
        "SELECT id, customer_name, product_id, product_name, price, status, created_at "
        "FROM purchases ORDER BY id DESC"
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]

def create_cart(customer_name: str, product: dict, age_minutes: int = 0) -> int:
    connection = get_connection()
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

def list_carts() -> List[dict]:
    connection = get_connection()
    rows = connection.execute(
        "SELECT id, customer_name, product_id, product_name, price, status, created_at "
        "FROM carts ORDER BY id DESC"
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]

def list_stale_pending_carts(minutes: int = 5) -> List[dict]:
    connection = get_connection()
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
