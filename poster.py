import os
import requests
from typing import Optional

POSTER_TOKEN = os.environ["POSTER_TOKEN"]
BASE_URL = "https://joinposter.com/api"


def _get(method: str, params: dict = None) -> dict:
    url = f"{BASE_URL}/{method}"
    p = {"token": POSTER_TOKEN}
    if params:
        p.update(params)
    r = requests.get(url, params=p, timeout=15)
    r.raise_for_status()
    return r.json()


def _post(method: str, data: dict = None) -> dict:
    url = f"{BASE_URL}/{method}"
    params = {"token": POSTER_TOKEN}
    r = requests.post(url, params=params, json=data or {}, timeout=15)
    r.raise_for_status()
    return r.json()


def get_ingredients() -> list[dict]:
    """Returns list of all ingredients with id, name, unit."""
    resp = _get("menu.getIngredients")
    items = resp.get("response", [])
    result = []
    for item in items:
        result.append({
            "id": item.get("ingredient_id"),
            "name": item.get("ingredient_name", ""),
            "unit": item.get("unit", ""),
        })
    return result


def get_suppliers() -> list[dict]:
    """Returns list of all suppliers with id, name."""
    resp = _get("storage.getSuppliers")
    items = resp.get("response", [])
    result = []
    for item in items:
        result.append({
            "id": item.get("supplier_id"),
            "name": item.get("supplier_name", ""),
        })
    return result


def get_storages() -> list[dict]:
    """Returns list of all storages with id, name."""
    resp = _get("storage.getStorages")
    items = resp.get("response", [])
    result = []
    for item in items:
        result.append({
            "id": item.get("storage_id"),
            "name": item.get("storage_name", ""),
        })
    return result


def get_products() -> list[dict]:
    """Returns list of all menu products with id and name."""
    resp = _get("menu.getProducts")
    items = resp.get("response", [])
    result = []
    for item in items:
        result.append({
            "id":   item.get("product_id"),
            "name": item.get("product_name", ""),
        })
    return result


def create_incoming_order(spot_id: int, phone: str, products: list[dict], comment: str = "") -> dict:
    """
    Create a customer incoming order in Poster (WhatsApp / delivery).
    products: list of {"product_id": int, "count": int}
    """
    payload: dict = {
        "spot_id":  spot_id,
        "phone":    phone,
        "products": [
            {
                "product_id":     p["product_id"],
                "modificator_id": p.get("modificator_id"),
                "count":          p["count"],
            }
            for p in products
        ],
    }
    if comment:
        payload["comment"] = comment
    return _post("incomingOrders.createIncomingOrder", payload)


def create_supply(supplier_id: int, storage_id: int, items: list[dict], comment: str = "") -> dict:
    """
    Create a supply (приход) in Poster.
    items: list of {"ingredient_id": int, "quantity": float, "price": float}
    price is per unit in kopecks (multiply UAH/USD by 100).
    """
    supply_ingredients = []
    for it in items:
        supply_ingredients.append({
            "ingredient_id": it["ingredient_id"],
            "quantity": it["quantity"],         # e.g. 1.5 (kg, pcs, etc)
            "price": int(it["price"] * 100),    # cost price in kopecks
        })

    payload = {
        "supplier_id": supplier_id,
        "storage_id": storage_id,
        "supply_ingredients": supply_ingredients,
    }
    if comment:
        payload["comment"] = comment

    resp = _post("storage.createSupply", payload)
    return resp
