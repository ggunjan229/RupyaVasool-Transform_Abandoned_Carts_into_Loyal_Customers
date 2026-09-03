"""
main.py — Mock store API (step 1 of the AI Revenue Recovery project).

This file only sells 3 products and records purchases.
Recovery emails, workers, and AI come in later steps.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from database import create_purchase, init_db, list_purchases

# Catalog lives in memory for now. Purchases go to SQLite.
# Three products keep this demo small and easy to explain.
PRODUCTS = [
    {"id": 1, "name": "Premium Leather Jacket", "price": 200.00},
    {"id": 2, "name": "Winter Scarf", "price": 40.00},
    {"id": 3, "name": "Wool Gloves", "price": 25.00},
]


class PurchaseRequest(BaseModel):
    """JSON body the client must send when buying an item."""

    # Field(...) means required. min_length=1 rejects empty names like "".
    customer_name: str = Field(..., min_length=1)
    product_id: int = Field(..., ge=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the server starts, before any request is accepted.
    init_db()
    # yield hands control to FastAPI. Code after yield would run on shutdown.
    yield


# title shows up in the automatic docs at /docs
app = FastAPI(title="AI Revenue Recovery — Mock Store", lifespan=lifespan)


@app.get("/")
def home():
    """Health check: if this returns JSON, the server is up."""
    return {
        "message": "Mock store is running",
        "docs": "/docs",
        "products": "/products",
        "purchases": "/purchases",
    }


@app.get("/products")
def get_products():
    """List the 3 products a shopper can buy."""
    return PRODUCTS


@app.post("/purchases")
def make_purchase(body: PurchaseRequest):
    """Record a completed purchase. This is a successful checkout, not a failed one."""
    product = next((item for item in PRODUCTS if item["id"] == body.product_id), None)
    if product is None:
        # 404 tells the client the product_id does not exist.
        raise HTTPException(status_code=404, detail="Product not found")

    purchase_id = create_purchase(body.customer_name, product)
    return {
        "id": purchase_id,
        "status": "completed",
        "customer_name": body.customer_name,
        "product": product,
    }


@app.get("/purchases")
def get_purchases():
    """Show every purchase stored in SQLite."""
    return list_purchases()
