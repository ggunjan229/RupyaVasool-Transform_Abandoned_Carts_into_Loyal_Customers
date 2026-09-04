from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from database import create_cart, create_purchase, init_db, list_carts, list_purchases

PRODUCTS = [
    {"id": 1, "name": "Premium Leather Jacket", "price": 200.00},
    {"id": 2, "name": "Winter Scarf", "price": 40.00},
    {"id": 3, "name": "Wool Gloves", "price": 25.00},
]

class PurchaseRequest(BaseModel):
    customer_name: str = Field(..., min_length=1)
    product_id: int = Field(..., ge=1)

class CartRequest(BaseModel):
    customer_name: str = Field(..., min_length=1)
    product_id: int = Field(..., ge=1)
    age_minutes: int = Field(0, ge=0)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="AI Revenue Recovery — Mock Store", lifespan=lifespan)

@app.get("/")
def home():
    return {
        "message": "Mock store is running",
        "docs": "/docs",
        "products": "/products",
        "purchases": "/purchases",
        "carts": "/carts",
    }

@app.get("/products")
def get_products():
    return PRODUCTS

def _get_product_or_404(product_id: int) -> dict:
    product = next((item for item in PRODUCTS if item["id"] == product_id), None)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product

@app.post("/purchases")
def make_purchase(body: PurchaseRequest):
    product = _get_product_or_404(body.product_id)
    try:
        purchase_id = create_purchase(body.customer_name, product["id"])
    except TypeError:
        purchase_id = create_purchase(body.customer_name, product)
    return {
        "id": purchase_id,
        "status": "completed",
        "customer_name": body.customer_name,
        "product": product,
    }

@app.get("/purchases")
def get_purchases():
    return list_purchases()

@app.post("/carts")
def make_cart(body: CartRequest):
    product = _get_product_or_404(body.product_id)
    try:
        cart_id = create_cart(body.customer_name, product["id"], age_minutes=body.age_minutes)
    except TypeError:
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
    return list_carts()
