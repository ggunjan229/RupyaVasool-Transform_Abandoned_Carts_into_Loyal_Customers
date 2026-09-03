"""
main.py — Mock store API (step 1 of the AI Revenue Recovery project).
This file only sells 3 products and records purchases.
Recovery emails, workers, and AI come in later steps.
This file sells 3 products, records completed purchases, and creates Pending carts.
The background worker (worker.py) scans those carts. Emails and AI come later.
"""
from contextlib import asynccontextmanager



from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from database import create_purchase, init_db, list_purchases
from database import create_cart, create_purchase, init_db, list_carts, list_purchases
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
class CartRequest(BaseModel):
    """JSON body for an unfinished checkout (status will be Pending)."""
    customer_name: str = Field(..., min_length=1)
    product_id: int = Field(..., ge=1)
    # 0 = created now (worker waits 5 minutes). 6 = already stale, for a quick test.
    age_minutes: int = Field(0, ge=0)
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the server starts, before any request is accepted.



        "docs": "/docs",
        "products": "/products",
        "purchases": "/purchases",
        "carts": "/carts",
    }



    return PRODUCTS
@app.post("/purchases")
def make_purchase(body: PurchaseRequest):
    """Record a completed purchase. This is a successful checkout, not a failed one."""
    product = next((item for item in PRODUCTS if item["id"] == body.product_id), None)
def _get_product_or_404(product_id: int) -> dict:
    # Shared lookup so /purchases and /carts use the same 404 behavior.
    product = next((item for item in PRODUCTS if item["id"] == product_id), None)
    if product is None:
        # 404 tells the client the product_id does not exist.
        raise HTTPException(status_code=404, detail="Product not found")
    return product
@app.post("/purchases")
def make_purchase(body: PurchaseRequest):
    """Record a completed purchase. This is a successful checkout, not a failed one."""
    product = _get_product_or_404(body.product_id)
    purchase_id = create_purchase(body.customer_name, product)
    return {
        "id": purchase_id,



def get_purchases():
    """Show every purchase stored in SQLite."""
    return list_purchases()
@app.post("/carts")
def make_cart(body: CartRequest):
    """Create a Pending cart (shopper started checkout but did not finish)."""
    product = _get_product_or_404(body.product_id)
    cart_id = create_cart(body.customer_name, product, age_minutes=body.age_minutes)
    return {
        "id": cart_id,
        "status": "Pending",
        "customer_name": body.customer_name,
        "product": product,
        "age_minutes": body.age_minutes,
    }
@app.get("/carts")
def get_carts():
    """Show every cart stored in SQLite."""
    return list_carts()