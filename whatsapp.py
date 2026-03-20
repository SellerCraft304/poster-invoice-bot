"""
WhatsApp Business Cloud webhook handler.

GET  /whatsapp → Meta verification challenge
POST /whatsapp → WhatsApp Catalog order → Poster incomingOrders.createIncomingOrder
GET  /health   → health-check for Railway
"""
from __future__ import annotations

import json
import logging
import os

from aiohttp import web

import poster

logger = logging.getLogger(__name__)

WA_VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "sushihouse2024")
# Poster spot_id for delivery/WhatsApp orders (set POSTER_SPOT_ID in Railway env)
POSTER_SPOT_ID = int(os.environ.get("POSTER_SPOT_ID", "1"))


async def handle_get(request: web.Request) -> web.Response:
    """Meta webhook verification — respond with hub.challenge."""
    mode      = request.rel_url.query.get("hub.mode")
    token     = request.rel_url.query.get("hub.verify_token")
    challenge = request.rel_url.query.get("hub.challenge")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        logger.info("✅ WhatsApp webhook verified by Meta")
        return web.Response(text=challenge)

    logger.warning(f"⛔ Webhook verification failed: mode={mode}, token={token}")
    return web.Response(status=403, text="Forbidden")


async def handle_post(request: web.Request) -> web.Response:
    """Receive WhatsApp Catalog order and create it in Poster."""
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad JSON")

    logger.info(f"📩 WhatsApp event: {json.dumps(body)[:800]}")

    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value    = change.get("value", {})
                messages = value.get("messages", [])
                contacts = value.get("contacts", [])

                # Phone & name
                phone         = contacts[0]["wa_id"]          if contacts else "unknown"
                customer_name = contacts[0]["profile"]["name"] if contacts else ""

                for msg in messages:
                    if msg.get("type") != "order":
                        logger.debug(f"Skipping non-order message type: {msg.get('type')}")
                        continue

                    order         = msg["order"]
                    product_items = order.get("product_items", [])
                    if not product_items:
                        logger.warning("Order contains no product_items, skipping")
                        continue

                    # Build Poster products list.
                    # Convention: product_retailer_id in WhatsApp catalog == Poster product_id
                    poster_products = []
                    for item in product_items:
                        retailer_id = item.get("product_retailer_id", "")
                        quantity    = int(item.get("quantity", 1))
                        try:
                            product_id = int(retailer_id)
                        except (ValueError, TypeError):
                            logger.warning(
                                f"Cannot map retailer_id '{retailer_id}' to Poster product_id, skipping item"
                            )
                            continue
                        poster_products.append({"product_id": product_id, "count": quantity})

                    if not poster_products:
                        logger.warning("No mappable products in order, skipping")
                        continue

                    comment = f"WhatsApp: {customer_name} ({phone})"
                    result  = poster.create_incoming_order(
                        spot_id=POSTER_SPOT_ID,
                        phone=phone,
                        products=poster_products,
                        comment=comment,
                    )
                    logger.info(f"✅ Poster incoming order created: {result}")

    except Exception as e:
        logger.error(f"Error processing WhatsApp webhook: {e}", exc_info=True)

    # Always return 200 — Meta will retry indefinitely on non-200
    return web.Response(text="OK")


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/whatsapp", handle_get)
    app.router.add_post("/whatsapp", handle_post)
    app.router.add_get("/health", handle_health)
    return app
