"""
worker.py — Background job that finds stale Pending carts.
Run this in a second terminal while the FastAPI server runs in the first.
This step only prints to the console. Emails and AI come later.
"""
from apscheduler.schedulers.blocking import BlockingScheduler
from database import init_db, list_stale_pending_carts
# How often the scan runs.
SCAN_EVERY_SECONDS = 60
# A cart is stale if it has been Pending longer than this.
STALE_AFTER_MINUTES = 5
def scan_stale_carts():
    """Look up stale carts and print them. APScheduler calls this every 60 seconds."""
    carts = list_stale_pending_carts(minutes=STALE_AFTER_MINUTES)
    print("--- worker scan ---")
    if not carts:
        print(f"No Pending carts older than {STALE_AFTER_MINUTES} minutes.")
        return
    print(f"Found {len(carts)} Pending cart(s) older than {STALE_AFTER_MINUTES} minutes:")
    for cart in carts:
        print(
            f"  cart_id={cart['id']} | customer={cart['customer_name']} | "
            f"item={cart['product_name']} | price={cart['price']} | "
            f"status={cart['status']} | created_at={cart['created_at']}"
        )
def main():
    # Worker has its own process, so it must create tables if the API is not up yet.
    init_db()
    scheduler = BlockingScheduler()
    # "interval" means: wait 60 seconds, run, wait 60 seconds, run, forever.
    scheduler.add_job(scan_stale_carts, "interval", seconds=SCAN_EVERY_SECONDS)
    print(
        f"Worker started. Scanning every {SCAN_EVERY_SECONDS} seconds "
        f"for Pending carts older than {STALE_AFTER_MINUTES} minutes."
    )
    print("Press Ctrl+C to stop.")
    # Run once now so you do not wait a full minute to see the first result.
    scan_stale_carts()
    try:
        # start() blocks this terminal, same idea as uvicorn.
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Worker stopped.")
# Only run the scheduler when you type: python worker.py
# Importing this file from tests will not start a loop.
if __name__ == "__main__":
    main()