"""
Poster POS Invoice Bot
Телеграм-бот для загрузки накладных в Poster POS через фото.
"""

import json
import logging
import os
from io import BytesIO

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import poster
import vision

# ─── Config ────────────────────────────────────────────────────────────────────
import os
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── States ────────────────────────────────────────────────────────────────────
WAITING_PHOTO = 0
SELECTING_SUPPLIER = 1
SELECTING_STORAGE = 2
CONFIRMING = 3

# ─── Helpers ───────────────────────────────────────────────────────────────────

def format_items_preview(matched_items: list[dict]) -> str:
    """Human-readable preview of matched invoice items."""
    lines = []
    for it in matched_items:
        raw = it["raw_name"]
        ru = it.get("ru_name", raw)
        matched = it.get("matched_name", "❓ не найден")
        qty = it["quantity"]
        unit = it["unit"]
        price = it["price_per_unit"]
        if it.get("ingredient_id"):
            if ru != raw:
                lines.append(f"✅ {matched} [{ru} / {raw}] — {qty} {unit} × {price}")
            else:
                lines.append(f"✅ {matched} ({raw}) — {qty} {unit} × {price}")
        else:
            if ru != raw:
                lines.append(f"❓ {ru} / {raw} — {qty} {unit} × {price} [не найден в Poster]")
            else:
                lines.append(f"❓ {raw} — {qty} {unit} × {price} [не найден в Poster]")
    return "\n".join(lines)


def build_supplier_keyboard(suppliers: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for s in suppliers:
        buttons.append([InlineKeyboardButton(s["name"], callback_data=f"sup:{s['id']}")])
    buttons.append([InlineKeyboardButton("➕ Другой (ввести вручную)", callback_data="sup:manual")])
    return InlineKeyboardMarkup(buttons)


def build_storage_keyboard(storages: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for s in storages:
        buttons.append([InlineKeyboardButton(s["name"], callback_data=f"sto:{s['id']}")])
    return InlineKeyboardMarkup(buttons)


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data="confirm:yes"),
            InlineKeyboardButton("❌ Отмена", callback_data="confirm:no"),
        ]
    ])

# ─── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👋 Привет! Я загружаю накладные в Poster POS.\n\n"
        "📸 Пришли фото накладной — распознаю товары и внесу приход.\n\n"
        "Команды:\n"
        "/start — начать\n"
        "/cancel — отмена"
    )
    return WAITING_PHOTO


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = await update.message.reply_text("⏳ Анализирую накладную...")

    # Download photo (highest resolution)
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    image_bytes = buf.getvalue()

    try:
        extracted = vision.extract_invoice(image_bytes)
    except Exception as e:
        logger.error(f"Vision error: {e}")
        await msg.edit_text(f"❌ Не удалось распознать накладную: {e}\n\nПопробуй ещё раз или /cancel")
        return WAITING_PHOTO

    # Load Poster data
    try:
        ingredients = poster.get_ingredients()
        suppliers = poster.get_suppliers()
        storages = poster.get_storages()
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка Poster API: {e}")
        return WAITING_PHOTO

    # Match ingredients
    raw_items = extracted.get("items", [])
    matched_items = []
    for it in raw_items:
        ru_name = it["name"]  # already translated to Russian by Claude
        original_name = it.get("original_name", ru_name)
        match = vision.match_ingredient(ru_name, ingredients)
        matched_items.append({
            "raw_name": original_name,
            "ru_name": ru_name,
            "matched_name": match["name"] if match else None,
            "ingredient_id": match["id"] if match else None,
            "quantity": it.get("quantity", 1),
            "unit": it.get("unit", ""),
            "price_per_unit": it.get("price_per_unit", 0),
        })

    context.user_data["matched_items"] = matched_items
    context.user_data["suppliers"] = suppliers
    context.user_data["storages"] = storages
    context.user_data["supplier_hint"] = extracted.get("supplier_hint")
    context.user_data["invoice_date"] = extracted.get("date")

    # Show extracted items
    preview = format_items_preview(matched_items)
    found_count = sum(1 for it in matched_items if it["ingredient_id"])
    total_count = len(matched_items)

    await msg.edit_text(
        f"📋 Распознано позиций: {total_count} (из них найдено в Poster: {found_count})\n\n"
        f"{preview}\n\n"
        f"📦 Выбери поставщика:"
    )

    # Pre-select supplier if hint found
    hint = extracted.get("supplier_hint")
    suggested = vision.match_supplier(hint, suppliers) if hint else None
    if suggested:
        context.user_data["suggested_supplier"] = suggested

    await update.message.reply_text(
        f"{'💡 Предлагаю поставщика: ' + suggested['name'] if suggested else ''}",
        reply_markup=build_supplier_keyboard(suppliers),
    )
    return SELECTING_SUPPLIER


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle PDF or image sent as document."""
    doc = update.message.document
    if not doc:
        return WAITING_PHOTO

    mime = doc.mime_type or "image/jpeg"
    if "image" not in mime and "pdf" not in mime:
        await update.message.reply_text("❌ Поддерживаются только фото и изображения. Пришли фото накладной.")
        return WAITING_PHOTO

    msg = await update.message.reply_text("⏳ Анализирую накладную...")
    file = await context.bot.get_file(doc.file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    image_bytes = buf.getvalue()

    try:
        extracted = vision.extract_invoice(image_bytes, mime_type=mime)
    except Exception as e:
        await msg.edit_text(f"❌ Не удалось распознать: {e}")
        return WAITING_PHOTO

    # Same logic as photo handler
    ingredients = poster.get_ingredients()
    suppliers = poster.get_suppliers()
    storages = poster.get_storages()

    raw_items = extracted.get("items", [])
    matched_items = []
    for it in raw_items:
        ru_name = it["name"]
        original_name = it.get("original_name", ru_name)
        match = vision.match_ingredient(ru_name, ingredients)
        matched_items.append({
            "raw_name": original_name,
            "ru_name": ru_name,
            "matched_name": match["name"] if match else None,
            "ingredient_id": match["id"] if match else None,
            "quantity": it.get("quantity", 1),
            "unit": it.get("unit", ""),
            "price_per_unit": it.get("price_per_unit", 0),
        })

    context.user_data["matched_items"] = matched_items
    context.user_data["suppliers"] = suppliers
    context.user_data["storages"] = storages
    context.user_data["supplier_hint"] = extracted.get("supplier_hint")
    context.user_data["invoice_date"] = extracted.get("date")

    preview = format_items_preview(matched_items)
    found_count = sum(1 for it in matched_items if it["ingredient_id"])
    total_count = len(matched_items)

    await msg.edit_text(
        f"📋 Распознано позиций: {total_count} (найдено в Poster: {found_count})\n\n"
        f"{preview}"
    )
    await update.message.reply_text("📦 Выбери поставщика:", reply_markup=build_supplier_keyboard(suppliers))
    return SELECTING_SUPPLIER


async def select_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data  # "sup:123" or "sup:manual"

    if data == "sup:manual":
        await query.edit_message_text("✏️ Введи название поставщика текстом:")
        context.user_data["awaiting_manual_supplier"] = True
        return SELECTING_SUPPLIER

    supplier_id = int(data.split(":")[1])
    suppliers = context.user_data["suppliers"]
    supplier = next((s for s in suppliers if s["id"] == supplier_id), None)
    context.user_data["selected_supplier"] = supplier

    storages = context.user_data["storages"]
    await query.edit_message_text(
        f"✅ Поставщик: {supplier['name']}\n\n🏪 Выбери склад:",
        reply_markup=build_storage_keyboard(storages),
    )
    return SELECTING_STORAGE


async def manual_supplier_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle manual supplier name input."""
    if not context.user_data.get("awaiting_manual_supplier"):
        return SELECTING_SUPPLIER

    name = update.message.text.strip()
    context.user_data["selected_supplier"] = {"id": None, "name": name}
    context.user_data["awaiting_manual_supplier"] = False

    storages = context.user_data["storages"]
    await update.message.reply_text(
        f"✅ Поставщик: {name}\n\n🏪 Выбери склад:",
        reply_markup=build_storage_keyboard(storages),
    )
    return SELECTING_STORAGE


async def select_storage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data  # "sto:1"

    storage_id = int(data.split(":")[1])
    storages = context.user_data["storages"]
    storage = next((s for s in storages if s["id"] == storage_id), None)
    context.user_data["selected_storage"] = storage

    # Build final confirmation
    supplier = context.user_data["selected_supplier"]
    matched_items = context.user_data["matched_items"]
    valid_items = [it for it in matched_items if it["ingredient_id"]]
    skipped_items = [it for it in matched_items if not it["ingredient_id"]]

    lines = ["📋 *Подтверди приход:*\n"]
    lines.append(f"🏭 Поставщик: {supplier['name']}")
    lines.append(f"🏪 Склад: {storage['name']}\n")
    lines.append("*Товары:*")
    for it in valid_items:
        lines.append(f"• {it['matched_name']} — {it['quantity']} {it['unit']} × {it['price_per_unit']}")

    if skipped_items:
        lines.append(f"\n⚠️ Пропущено (не найдено в Poster): {len(skipped_items)} шт.")
        for it in skipped_items:
            lines.append(f"  – {it['raw_name']}")

    if not valid_items:
        await query.edit_message_text(
            "❌ Ни один товар не найден в Poster. Приход не может быть создан.\n"
            "Пришли новое фото или /cancel"
        )
        return WAITING_PHOTO

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=build_confirm_keyboard(),
    )
    return CONFIRMING


async def confirm_supply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "confirm:no":
        await query.edit_message_text("❌ Отменено. Пришли новое фото или /cancel")
        return WAITING_PHOTO

    # Send to Poster
    supplier = context.user_data["selected_supplier"]
    storage = context.user_data["selected_storage"]
    matched_items = context.user_data["matched_items"]
    valid_items = [it for it in matched_items if it["ingredient_id"]]

    if not supplier.get("id"):
        await query.edit_message_text(
            "⚠️ Поставщик введён вручную — он не сохранён в Poster.\n"
            "Для корректного учёта добавь поставщика в Poster вручную.\n\n"
            "Создаю приход без поставщика..."
        )
        # Use first supplier as fallback or skip
        suppliers = context.user_data["suppliers"]
        supplier_id = suppliers[0]["id"] if suppliers else 1
    else:
        supplier_id = supplier["id"]

    storage_id = storage["id"]

    poster_items = []
    for it in valid_items:
        poster_items.append({
            "ingredient_id": it["ingredient_id"],
            "quantity": it["quantity"],
            "price": it["price_per_unit"],
        })

    await query.edit_message_text("⏳ Отправляю в Poster...")

    try:
        result = poster.create_supply(supplier_id, storage_id, poster_items)
        supply_id = result.get("response", {})
        await query.edit_message_text(
            f"✅ Приход успешно создан в Poster!\n\n"
            f"🏭 Поставщик: {supplier['name']}\n"
            f"🏪 Склад: {storage['name']}\n"
            f"📦 Позиций: {len(valid_items)}\n\n"
            f"Пришли следующее фото или /cancel"
        )
    except Exception as e:
        logger.error(f"Poster createSupply error: {e}")
        await query.edit_message_text(
            f"❌ Ошибка при создании прихода в Poster:\n{e}\n\n"
            f"Попробуй снова или обратись к администратору."
        )

    return WAITING_PHOTO


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено. Пришли фото накладной когда будешь готов.")
    return WAITING_PHOTO


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("📸 Пришли фото накладной.")
    return WAITING_PHOTO

# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        per_message=False,
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.PHOTO, handle_photo),
            MessageHandler(filters.Document.IMAGE | filters.Document.MimeType("application/pdf"), handle_document),
        ],
        states={
            WAITING_PHOTO: [
                MessageHandler(filters.PHOTO, handle_photo),
                MessageHandler(filters.Document.IMAGE | filters.Document.MimeType("application/pdf"), handle_document),
            ],
            SELECTING_SUPPLIER: [
                CallbackQueryHandler(select_supplier, pattern=r"^sup:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, manual_supplier_text),
            ],
            SELECTING_STORAGE: [
                CallbackQueryHandler(select_storage, pattern=r"^sto:"),
            ],
            CONFIRMING: [
                CallbackQueryHandler(confirm_supply, pattern=r"^confirm:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.TEXT & ~filters.COMMAND, unknown),
        ],
    )

    app.add_handler(conv)

    logger.info("🤖 Poster Invoice Bot запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
