"""
worker.py -- Background worker (step 2 of the AI Revenue Recovery project).

This file runs SEPARATELY from main.py (the FastAPI store).
Its only job right now: every 60 seconds, look inside store.db for
carts that have been sitting in 'Pending' status for more than 5 minutes,
and print them to the console.

No emails, no AI, no workers-queue yet -- just detection. That comes next.
"""

import sqlite3
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

# --- Config -----------------------------------------------------------
DB_PATH = "store.db"          # same SQLite file main.py already uses
SCAN_INTERVAL_SECONDS = 60    # how often the job runs
STALE_AFTER_MINUTES = 5       # how old a 'Pending' cart must be to count


def ensure_carts_table():
    """
    Creates the 'carts' table if it doesn't exist yet.
    Safe to call every time the script starts -- 'IF NOT EXISTS' means
    it won't wipe or duplicate anything if the table is already there.
    """
    conn = sqlite3.connect(DB_PATH)          # open (or create) the .db file
    cursor = conn.cursor()                   # cursor = lets us run SQL commands
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS carts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,   -- unique cart id, auto-increments
            customer_name TEXT NOT NULL,            -- who owns this cart
            product_id INTEGER NOT NULL,            -- which product they were buying
            status TEXT NOT NULL DEFAULT 'Pending',  -- Pending / Completed / Abandoned
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP  -- when cart started
        )
        """
    )
    conn.commit()   # commit() saves the change permanently to the .db file
    conn.close()    # always close the connection when done


def scan_stale_carts():
    """
    This is the job APScheduler will call every 60 seconds.
    It finds every cart where:
      - status is still 'Pending'
      - created_at is older than (now - 5 minutes)
    and prints the details.
    """
    # Calculate the cutoff time: anything created BEFORE this is "stale"
    cutoff_time = datetime.now() - timedelta(minutes=STALE_AFTER_MINUTES)
    # SQLite stores timestamps as text, so we format cutoff to match that format
    cutoff_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row   # lets us access columns by name, e.g. row["id"]
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, customer_name, product_id, status, created_at
        FROM carts
        WHERE status = 'Pending'
          AND created_at <= ?
        """,
        (cutoff_str,),   # the '?' is a placeholder -- prevents SQL injection
    )
    stale_carts = cursor.fetchall()   # fetchall() = get every matching row as a list
    conn.close()

    # print() here is our "notification" for now -- step 3 will replace/extend
    # this with an actual recovery email.
    if not stale_carts:
        print(f"[{datetime.now()}] No stale carts found.")
        return

    print(f"[{datetime.now()}] Found {len(stale_carts)} stale cart(s):")
    for cart in stale_carts:
        print(
            f"  cart_id={cart['id']} | customer={cart['customer_name']} "
            f"| product_id={cart['product_id']} | pending_since={cart['created_at']}"
        )


def main():
    ensure_carts_table()   # make sure the table exists before the loop starts

    scheduler = BlockingScheduler()   # BlockingScheduler = runs in the main thread, keeps script alive
    scheduler.add_job(
        scan_stale_carts,             # the function to run
        "interval",                   # trigger type: run repeatedly at a fixed interval
        seconds=SCAN_INTERVAL_SECONDS,
        id="scan_stale_carts_job",    # a name for this job (useful if you add more jobs later)
    )

    print("Worker started. Scanning for stale carts every "
          f"{SCAN_INTERVAL_SECONDS} seconds. Press Ctrl+C to stop.")

    


if __name__ == "__main__":
    # This guard means: only run main() if this file is executed directly
    # (e.g. `python worker.py`), not if it's imported elsewhere later.
    main()