"""
Poster POS Invoice Bot — v2
Семантический матчинг через Claude + ручное сопоставление + кэш + поиск поставщика
WhatsApp Catalog webhook → Poster incomingOrders
"""

import asyncio
import logging
import os
from io import BytesIO

from aiohttp import web

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import cache
import poster
import vision
import whatsapp as wa_webhook

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# ─── Auth ───────────────────────────────────────────────────────────────────────
# Set ALLOWED_USERS env var as comma-separated Telegram user IDs, e.g. "123456,789012"
# If empty — bot is open to everyone (not recommended for production)
_raw_ids = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USER_IDS: set[int] = {int(x.strip()) for x in _raw_ids.split(",") if x.strip().isdigit()}


def _is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── States ────────────────────────────────────────────────────────────────────
WAITING_PHOTO     = 0
FIXING_UNMATCHED  = 1   # manually match unrecognized items
SEARCHING_ING     = 2   # user typing ingredient search
SELECTING_SUPPLIER = 3
SEARCHING_SUPPLIER = 4  # user typing supplier search
SELECTING_STORAGE  = 5
CONFIRMING         = 6

# ─── Keyboards ─────────────────────────────────────────────────────────────────

def build_supplier_keyboard(suppliers: list[dict], show_search: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    for s in suppliers[:20]:  # max 20 shown directly
        buttons.append([InlineKeyboardButton(s["name"], callback_data=f"sup:{s['id']}")])
    if show_search:
        buttons.append([InlineKeyboardButton("🔍 Поиск поставщика...", callback_data="sup:search")])
    buttons.append([InlineKeyboardButton("➕ Другой (вручную)", callback_data="sup:manual")])
    return InlineKeyboardMarkup(buttons)


def build_storage_keyboard(storages: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s["name"], callback_data=f"sto:{s['id']}")]
        for s in storages
    ])


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data="confirm:yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="confirm:no"),
    ]])


def build_fix_keyboard(item: dict, ingredients: list[dict]) -> InlineKeyboardMarkup:
    """Keyboard for manually fixing one unmatched item."""
    candidates = vision.get_top_candidates(item.get("ru_name", item["raw_name"]), ingredients, n=7)
    buttons = [
        [InlineKeyboardButton(f"✓ {c['name']}", callback_data=f"fix:{item['_idx']}:{c['id']}")]
        for c in candidates
    ]
    buttons.append([
        InlineKeyboardButton("🔍 Поиск...", callback_data=f"fix:{item['_idx']}:search"),
        InlineKeyboardButton("⏭ Пропустить", callback_data=f"fix:{item['_idx']}:skip"),
    ])
    return InlineKeyboardMarkup(buttons)

# ─── Helpers ───────────────────────────────────────────────────────────────────

def format_items_preview(matched_items: list[dict]) -> str:
    lines = []
    for it in matched_items:
        raw   = it["raw_name"]
        ru    = it.get("ru_name", raw)
        match = it.get("matched_name")
        qty   = it["quantity"]
        unit  = it["unit"]
        price = it["price_per_unit"]

        label = f"{ru} / {raw}" if ru != raw else raw

        if match:
            lines.append(f"✅ *{match}* [{label}] — {qty} {unit} × {price}")
        else:
            lines.append(f"❓ {label} — {qty} {unit} × {price} _[не найден]_")
    return "\n".join(lines)


def _unmatched_indices(items: list[dict]) -> list[int]:
    return [i for i, it in enumerate(items) if not it.get("ingredient_id")]


async def _process_photo_bytes(
    image_bytes: bytes,
    mime_type: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg,
) -> int:
    """Shared logic for photo and document handlers."""

    # 1. Extract from image
    await status_msg.edit_text("🔍 Распознаю накладную...")
    try:
        extracted = vision.extract_invoice(image_bytes, mime_type=mime_type)
    except Exception as e:
        await status_msg.edit_text(f"❌ Не удалось распознать накладную:\n{e}\n\nПопробуй ещё раз или /cancel")
        return WAITING_PHOTO

    raw_items = extracted.get("items", [])
    if not raw_items:
        await status_msg.edit_text("❌ Товары не найдены на фото. Попробуй другой ракурс или /cancel")
        return WAITING_PHOTO

    # 2. Load Poster data
    await status_msg.edit_text("📡 Загружаю данные Poster...")
    try:
        ingredients = poster.get_ingredients()
        suppliers   = poster.get_suppliers()
        storages    = poster.get_storages()
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка Poster API:\n{e}")
        return WAITING_PHOTO

    # 3. Check cache first
    matched_items = []
    items_for_semantic = []   # items NOT found in cache
    items_for_semantic_idx = []  # their indices in matched_items

    for it in raw_items:
        ru_name  = it["name"]
        orig     = it.get("original_name", ru_name)
        base_item = {
            "raw_name":      orig,
            "ru_name":       ru_name,
            "matched_name":  None,
            "ingredient_id": None,
            "quantity":      it.get("quantity", 1),
            "unit":          it.get("unit", "шт"),
            "price_per_unit": it.get("price_per_unit", 0),
        }

        cached = cache.get_cached(orig) or cache.get_cached(ru_name)
        if cached:
            # Verify the ingredient still exists
            ing_id = cached["ingredient_id"]
            ing = next((i for i in ingredients if str(i["id"]) == str(ing_id)), None)
            if ing:
                base_item["ingredient_id"] = ing["id"]
                base_item["matched_name"]  = ing["name"]
                base_item["from_cache"]    = True

        if not base_item["ingredient_id"]:
            items_for_semantic_idx.append(len(matched_items))
            items_for_semantic.append(it)

        matched_items.append(base_item)

    # 4. Semantic matching via Claude for uncached items
    if items_for_semantic:
        await status_msg.edit_text(
            f"🤖 Семантически сопоставляю {len(items_for_semantic)} позиций через Claude..."
        )
        try:
            sem_matches = vision.semantic_match_all(items_for_semantic, ingredients)
        except Exception as e:
            logger.error(f"Semantic match error: {e}")
            sem_matches = [None] * len(items_for_semantic)

        for list_idx, ing in zip(items_for_semantic_idx, sem_matches):
            if ing:
                matched_items[list_idx]["ingredient_id"] = ing["id"]
                matched_items[list_idx]["matched_name"]  = ing["name"]

    # 5. Add _idx for manual fix reference
    for i, it in enumerate(matched_items):
        it["_idx"] = i

    # 6. Store state
    context.user_data.update({
        "matched_items":     matched_items,
        "ingredients":       ingredients,
        "suppliers":         suppliers,
        "storages":          storages,
        "supplier_hint":     extracted.get("supplier_hint"),
        "invoice_date":      extracted.get("date"),
        "fix_queue":         _unmatched_indices(matched_items),
        "fix_position":      0,
    })

    # 7. Show preview
    preview   = format_items_preview(matched_items)
    found_cnt = sum(1 for it in matched_items if it.get("ingredient_id"))
    total_cnt = len(matched_items)
    cache_cnt = sum(1 for it in matched_items if it.get("from_cache"))

    cache_note = f" (из кэша: {cache_cnt})" if cache_cnt else ""
    await status_msg.edit_text(
        f"📋 Позиций: {total_cnt} | Найдено в Poster: {found_cnt}{cache_note}\n\n{preview}",
        parse_mode="Markdown",
    )

    unmatched = _unmatched_indices(matched_items)
    if unmatched:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🔧 Сопоставить ({len(unmatched)} поз.)", callback_data="fix:start"),
            InlineKeyboardButton("⏭ Пропустить все", callback_data="fix:skip_all"),
        ]])
        await update.effective_message.reply_text(
            f"⚠️ {len(unmatched)} позиций не найдено в Poster. Что делаем?",
            reply_markup=kb,
        )
        return FIXING_UNMATCHED
    else:
        await _show_supplier_step(update, context)
        return SELECTING_SUPPLIER


async def _show_supplier_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    suppliers = context.user_data["suppliers"]
    hint      = context.user_data.get("supplier_hint")
    suggested = vision.match_supplier(hint, suppliers) if hint else None

    text = "📦 Выбери поставщика:"
    if suggested:
        text = f"💡 Предлагаю: *{suggested['name']}*\n\n" + text

    await update.effective_message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=build_supplier_keyboard(suppliers),
    )


async def _show_current_fix(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    """Show fix UI for current unmatched item."""
    queue    = context.user_data["fix_queue"]
    pos      = context.user_data["fix_position"]
    items    = context.user_data["matched_items"]
    ings     = context.user_data["ingredients"]

    if pos >= len(queue):
        await _show_supplier_step(update, context)
        return SELECTING_SUPPLIER

    item_idx = queue[pos]
    item     = items[item_idx]
    ru       = item.get("ru_name", item["raw_name"])
    raw      = item["raw_name"]

    text = (
        f"❓ *Позиция {pos+1}/{len(queue)}*\n\n"
        f"Товар: *{ru}*\n"
        f"Оригинал: `{raw}`\n\n"
        f"Выбери ингредиент из Poster:"
    )
    kb = build_fix_keyboard(item, ings)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

    return FIXING_UNMATCHED

# ─── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 Привет! Я вношу накладные в Poster POS.\n\n"
        "📸 Пришли фото накладной — распознаю и создам приход.\n\n"
        "/refresh — обновить список ингредиентов из Poster\n"
        "/cancel — отмена"
    )
    return WAITING_PHOTO


async def refresh_ingredients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Force-reload ingredients from Poster and show count."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return ConversationHandler.END
    msg = await update.message.reply_text("🔄 Обновляю список ингредиентов из Poster...")
    try:
        ingredients = poster.get_ingredients()
        suppliers   = poster.get_suppliers()
        # Cache in application context for quick access (optional warm-up)
        context.bot_data["ingredients"] = ingredients
        context.bot_data["suppliers"]   = suppliers
        await msg.edit_text(
            f"✅ Обновлено!\n\n"
            f"🥦 Ингредиентов: {len(ingredients)}\n"
            f"🏭 Поставщиков: {len(suppliers)}\n\n"
            f"Пришли фото накладной."
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка Poster API:\n{e}")
    return WAITING_PHOTO


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return ConversationHandler.END
    msg = await update.message.reply_text("⏳ Обрабатываю...")
    photo = update.message.photo[-1]
    file  = await context.bot.get_file(photo.file_id)
    buf   = BytesIO()
    await file.download_to_memory(buf)
    return await _process_photo_bytes(buf.getvalue(), "image/jpeg", update, context, msg)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return ConversationHandler.END
    doc  = update.message.document
    mime = doc.mime_type or "image/jpeg"
    if "image" not in mime and "pdf" not in mime:
        await update.message.reply_text("❌ Поддерживаются только фото/изображения.")
        return WAITING_PHOTO
    msg  = await update.message.reply_text("⏳ Обрабатываю...")
    file = await context.bot.get_file(doc.file_id)
    buf  = BytesIO()
    await file.download_to_memory(buf)
    return await _process_photo_bytes(buf.getvalue(), mime, update, context, msg)


# ── Fix unmatched ──────────────────────────────────────────────────────────────

async def fix_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data  = query.data  # "fix:start" | "fix:skip_all" | "fix:{idx}:{ing_id|skip|search}"

    parts = data.split(":")

    if data == "fix:start":
        context.user_data["fix_position"] = 0
        return await _show_current_fix(update, context, edit=True)

    if data == "fix:skip_all":
        await query.edit_message_text("⏭ Нераспознанные позиции пропущены.")
        await _show_supplier_step(update, context)
        return SELECTING_SUPPLIER

    # fix:{item_idx}:{action}
    try:
        item_idx = int(parts[1])
        action   = parts[2]
    except (IndexError, ValueError):
        logger.warning(f"Malformed fix callback: {data}")
        await query.answer("Ошибка, попробуй снова.")
        return FIXING_UNMATCHED
    items    = context.user_data["matched_items"]
    ings     = context.user_data["ingredients"]

    if action == "skip":
        # advance to next
        context.user_data["fix_position"] += 1
        return await _show_current_fix(update, context, edit=True)

    if action == "search":
        context.user_data["search_item_idx"] = item_idx
        item = items[item_idx]
        ru   = item.get("ru_name", item["raw_name"])
        await query.edit_message_text(
            f"🔍 Поиск для: *{ru}*\n\nВведи название ингредиента (часть слова):",
            parse_mode="Markdown",
        )
        return SEARCHING_ING

    # action is an ingredient_id
    ing_id = action
    ing    = next((i for i in ings if str(i["id"]) == str(ing_id)), None)
    if ing:
        items[item_idx]["ingredient_id"] = ing["id"]
        items[item_idx]["matched_name"]  = ing["name"]
        # Save to cache
        cache.save_match(items[item_idx]["raw_name"], ing["id"], ing["name"])
        if items[item_idx].get("ru_name"):
            cache.save_match(items[item_idx]["ru_name"], ing["id"], ing["name"])

    context.user_data["fix_position"] += 1
    return await _show_current_fix(update, context, edit=True)


async def search_ingredient_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed a search query for ingredient."""
    query_text = update.message.text.strip()
    item_idx   = context.user_data.get("search_item_idx", 0)
    ings       = context.user_data["ingredients"]
    items      = context.user_data["matched_items"]

    results = vision.get_top_candidates(query_text, ings, n=8)
    if not results:
        await update.message.reply_text("🔍 Ничего не найдено. Попробуй другой запрос или /cancel")
        return SEARCHING_ING

    buttons = [
        [InlineKeyboardButton(r["name"], callback_data=f"fix:{item_idx}:{r['id']}")]
        for r in results
    ]
    buttons.append([
        InlineKeyboardButton("🔍 Другой запрос", callback_data=f"fix:{item_idx}:search"),
        InlineKeyboardButton("⏭ Пропустить", callback_data=f"fix:{item_idx}:skip"),
    ])

    item = items[item_idx]
    ru   = item.get("ru_name", item["raw_name"])
    await update.message.reply_text(
        f"Результаты для «{query_text}» (товар: *{ru}*):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return FIXING_UNMATCHED


# ── Supplier ───────────────────────────────────────────────────────────────────

async def select_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data  = query.data  # "sup:{id}" | "sup:search" | "sup:manual"

    if data == "sup:search":
        await query.edit_message_text("🔍 Введи название поставщика (часть слова):")
        return SEARCHING_SUPPLIER

    if data == "sup:manual":
        await query.edit_message_text("✏️ Введи название поставщика:")
        context.user_data["awaiting_manual_supplier"] = True
        return SELECTING_SUPPLIER

    sup_id    = data.split(":")[1]  # keep as string for safe comparison
    suppliers = context.user_data["suppliers"]
    supplier  = next((s for s in suppliers if str(s["id"]) == sup_id), None)
    if not supplier:
        await query.edit_message_text("❌ Поставщик не найден. Попробуй снова.")
        await _show_supplier_step(update, context)
        return SELECTING_SUPPLIER
    context.user_data["selected_supplier"] = supplier

    storages = context.user_data["storages"]
    await query.edit_message_text(
        f"✅ Поставщик: *{supplier['name']}*\n\n🏪 Выбери склад:",
        parse_mode="Markdown",
        reply_markup=build_storage_keyboard(storages),
    )
    return SELECTING_STORAGE


async def search_supplier_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query_text = update.message.text.strip()
    suppliers  = context.user_data["suppliers"]

    q_lower = query_text.lower()
    results = [s for s in suppliers if q_lower in s["name"].lower()][:10]

    if not results:
        await update.message.reply_text(
            "🔍 Поставщик не найден. Попробуй другой запрос или:",
            reply_markup=build_supplier_keyboard(suppliers, show_search=False),
        )
        return SEARCHING_SUPPLIER

    buttons = [
        [InlineKeyboardButton(s["name"], callback_data=f"sup:{s['id']}")]
        for s in results
    ]
    buttons.append([InlineKeyboardButton("🔍 Другой запрос", callback_data="sup:search")])
    await update.message.reply_text(
        f"Результаты по «{query_text}»:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return SELECTING_SUPPLIER


async def manual_supplier_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.user_data.get("awaiting_manual_supplier"):
        return SELECTING_SUPPLIER
    name = update.message.text.strip()
    context.user_data["selected_supplier"]         = {"id": None, "name": name}
    context.user_data["awaiting_manual_supplier"]  = False
    storages = context.user_data["storages"]
    await update.message.reply_text(
        f"✅ Поставщик: *{name}*\n\n🏪 Выбери склад:",
        parse_mode="Markdown",
        reply_markup=build_storage_keyboard(storages),
    )
    return SELECTING_STORAGE


# ── Storage & confirm ──────────────────────────────────────────────────────────

async def select_storage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    storage_id = query.data.split(":")[1]  # keep as string
    storages   = context.user_data["storages"]
    storage    = next((s for s in storages if str(s["id"]) == storage_id), None)
    if not storage:
        await query.answer("❌ Склад не найден, попробуй снова.", show_alert=True)
        await query.edit_message_text(
            "❌ Склад не найден. Выбери снова:",
            reply_markup=build_storage_keyboard(storages),
        )
        return SELECTING_STORAGE
    context.user_data["selected_storage"] = storage

    supplier      = context.user_data["selected_supplier"]
    matched_items = context.user_data["matched_items"]
    valid_items   = [it for it in matched_items if it.get("ingredient_id")]
    skipped_items = [it for it in matched_items if not it.get("ingredient_id")]

    lines = [
        "📋 *Подтверди приход:*\n",
        f"🏭 Поставщик: {supplier['name']}",
        f"🏪 Склад: {storage['name']}\n",
        "*Товары:*",
    ]
    for it in valid_items:
        lines.append(f"• {it['matched_name']} — {it['quantity']} {it['unit']} × {it['price_per_unit']}")

    if skipped_items:
        lines.append(f"\n⚠️ Пропущено (нет в Poster): {len(skipped_items)} шт.")
        for it in skipped_items:
            lines.append(f"  – {it.get('ru_name', it['raw_name'])}")

    if not valid_items:
        await query.edit_message_text(
            "❌ Ни одного товара не найдено в Poster. Отправить нечего.\n/cancel"
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

    if query.data == "confirm:no":
        await query.edit_message_text("❌ Отменено. Пришли фото или /cancel")
        return WAITING_PHOTO

    supplier      = context.user_data["selected_supplier"]
    storage       = context.user_data["selected_storage"]
    matched_items = context.user_data["matched_items"]
    valid_items   = [it for it in matched_items if it.get("ingredient_id")]
    suppliers     = context.user_data["suppliers"]

    supplier_id = supplier.get("id")
    if not supplier_id:
        supplier_id = suppliers[0]["id"] if suppliers else 1

    poster_items = [
        {
            "ingredient_id": it["ingredient_id"],
            "quantity":      it["quantity"],
            "price":         it["price_per_unit"],
        }
        for it in valid_items
    ]

    await query.edit_message_text("⏳ Отправляю в Poster...")
    try:
        result = poster.create_supply(supplier_id, storage["id"], poster_items)
        # Save all matched pairs to cache
        for it in valid_items:
            cache.save_match(it["raw_name"], it["ingredient_id"], it["matched_name"])
            if it.get("ru_name") and it["ru_name"] != it["raw_name"]:
                cache.save_match(it["ru_name"], it["ingredient_id"], it["matched_name"])

        await query.edit_message_text(
            f"✅ *Приход создан в Poster!*\n\n"
            f"🏭 {supplier['name']}\n"
            f"🏪 {storage['name']}\n"
            f"📦 Позиций: {len(valid_items)}\n\n"
            f"Пришли следующее фото или /cancel",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"createSupply error: {e}")
        await query.edit_message_text(f"❌ Ошибка Poster:\n{e}\n\nПопробуй снова или /cancel")

    return WAITING_PHOTO


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено. Жду фото накладной.")
    return WAITING_PHOTO


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("📸 Пришли фото накладной.")
    return WAITING_PHOTO

# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    port = int(os.environ.get("PORT", 8080))

    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        per_message=False,
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("refresh", refresh_ingredients),
            MessageHandler(filters.PHOTO, handle_photo),
            MessageHandler(filters.Document.IMAGE | filters.Document.MimeType("application/pdf"), handle_document),
        ],
        states={
            WAITING_PHOTO: [
                MessageHandler(filters.PHOTO, handle_photo),
                MessageHandler(filters.Document.IMAGE | filters.Document.MimeType("application/pdf"), handle_document),
            ],
            FIXING_UNMATCHED: [
                CallbackQueryHandler(fix_callback, pattern=r"^fix:"),
            ],
            SEARCHING_ING: [
                CallbackQueryHandler(fix_callback, pattern=r"^fix:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_ingredient_text),
            ],
            SELECTING_SUPPLIER: [
                CallbackQueryHandler(select_supplier, pattern=r"^sup:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, manual_supplier_text),
            ],
            SEARCHING_SUPPLIER: [
                CallbackQueryHandler(select_supplier, pattern=r"^sup:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_supplier_text),
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
            MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text),
        ],
    )
    tg_app.add_handler(conv)

    async def run_all():
        # ── Start aiohttp webhook server ──────────────────────────────────────
        wa_app  = wa_webhook.create_app()
        runner  = web.AppRunner(wa_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"🌐 WhatsApp webhook listening on port {port} (/whatsapp)")

        # ── Start Telegram bot ────────────────────────────────────────────────
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("🤖 Poster Invoice Bot v2 запущен...")

        # ── Run forever ───────────────────────────────────────────────────────
        try:
            await asyncio.Event().wait()
        finally:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
            await runner.cleanup()

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
