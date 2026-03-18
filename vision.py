from __future__ import annotations

import anthropic
import base64
import difflib
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ─── Prompt: extract invoice items ────────────────────────────────────────────

EXTRACT_PROMPT = """
Ты — ассистент для ресторана. Тебе прислали фото накладной (товарная накладная / invoice).
Накладная может быть на любом языке (турецкий, английский, русский и др.).

Извлеки список товаров и ОБЯЗАТЕЛЬНО переведи названия на русский язык.

Верни ТОЛЬКО валидный JSON (без лишнего текста):
{
  "items": [
    {"name": "название на русском", "original_name": "оригинальное название из накладной", "quantity": 1.5, "unit": "кг", "price_per_unit": 250.0},
    ...
  ],
  "supplier_hint": "название поставщика если видно на накладной, иначе null",
  "date": "дата накладной если видна (YYYY-MM-DD), иначе null"
}

Важно:
- name — ВСЕГДА на русском языке (переводи с турецкого/английского/любого другого)
- original_name — оригинальное название как написано в накладной
- Примеры перевода: MAYONEZ→Майонез, PİRİNÇ→Рис, TOST PEYNİRİ→Тостовый сыр,
  KAŞAR→Сыр кашар, TEREYAĞI→Масло сливочное, DOMATES→Помидор, SOĞAN→Лук,
  ET→Мясо, TAVUK→Курица, YAĞ→Масло, UN→Мука, ŞEKER→Сахар, TUZ→Соль,
  MAYA→Дрожжи, PEYNIR→Сыр, SÜT→Молоко, YUMURTA→Яйцо
- quantity — числовое значение количества
- unit — единица измерения на русском (кг, г, шт, л, уп, пач)
- price_per_unit — цена за единицу (если указана цена за всё — раздели на quantity)
- Если цены нет — ставь 0
- Включай граммовку в название только если она важна для идентификации: "Майонез 840г", "Дрожжи 42г"
"""


# ─── Prompt: semantic matching ────────────────────────────────────────────────

def build_match_prompt(items: list[dict], ingredients: list[dict]) -> str:
    ing_lines = "\n".join(f"{ing['id']}: {ing['name']}" for ing in ingredients)
    items_lines = "\n".join(
        f"{i}. {it['name']}" + (f" (ориг: {it.get('original_name','')})" if it.get('original_name') and it['original_name'] != it['name'] else "")
        for i, it in enumerate(items)
    )
    return f"""Ты — эксперт по складскому учёту ресторана.
Твоя задача: сопоставить товары из накладной с ингредиентами в системе Poster.

ТОВАРЫ ИЗ НАКЛАДНОЙ:
{items_lines}

ИНГРЕДИЕНТЫ В POSTER (формат "id: название"):
{ing_lines}

Верни ТОЛЬКО JSON массив (без лишнего текста):
[
  {{"idx": 0, "ingredient_id": 123}},
  {{"idx": 1, "ingredient_id": null}},
  ...
]

СТРОГИЕ ПРАВИЛА:

1. СОПОСТАВЛЯЙ ТОЛЬКО ОДИНАКОВЫЕ ПРОДУКТЫ:
   - "Майонез 640г Bolbol" → "Майонез" ✅  (один и тот же продукт, бренд/граммовка не важны)
   - "Рис жасмин 1кг" → "Рис обычный" ✅  (это рис, сорт жасмин — вариация риса)
   - "Дрожжи 4×42г" → "Дрожжи" ✅
   - "Тростовый сыр" → "Сыр для суши" ✅  (похожий вид сыра)
   - "Сыр кашар" → "Сыр для суши" или "Сыр Чеддер" ✅ если подходит по типу

2. ОБЯЗАТЕЛЬНО null ЕСЛИ:
   - Продукт принципиально другого типа (пакет, мешок, упаковочный материал — не еда)
   - В Poster буквально нет ничего похожего по смыслу
   - ТЫ НЕ УВЕРЕН — лучше null, чем неправильный ID

3. ЗАПРЕЩЕНО:
   - Рис → Салями ❌  (разные продукты)
   - Сыр → Соевый соус ❌  (разные категории)
   - Пакет/мешок → Палочки для суши ❌  (не еда → еда)
   - НЕЛЬЗЯ придумывать id которых нет в списке выше

4. ГЛАВНОЕ ПРАВИЛО: ingredient_id в ответе ДОЛЖЕН существовать в списке выше.
   Неправильный id хуже чем null.
"""


# ─── Main functions ────────────────────────────────────────────────────────────

def extract_invoice(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Send invoice image to Claude Vision and extract structured data."""
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": EXTRACT_PROMPT},
                ],
            }
        ],
    )

    text = message.content[0].text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])

    return json.loads(text)


def semantic_match_all(items: list[dict], ingredients: list[dict]) -> list[Optional[dict]]:
    """
    Use Claude to semantically match all invoice items to Poster ingredients.
    Returns a list of matched ingredient dicts (or None) — one per item.
    This is the PRIMARY matching method: accurate, context-aware, handles synonyms.
    """
    if not items or not ingredients:
        return [None] * len(items)

    prompt = build_match_prompt(items, ingredients)

    try:
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",  # sonnet: надёжнее haiku для 167 ингредиентов
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        text = message.content[0].text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])

        results = json.loads(text)

    except Exception as e:
        logger.error(f"Semantic match failed: {e}")
        # Fall back to fuzzy if Claude fails
        return [match_ingredient_fuzzy(it["name"], ingredients) for it in items]

    # Build lookup: id → ingredient (handle both int and str ids)
    ing_by_id = {}
    for ing in ingredients:
        ing_by_id[ing["id"]] = ing
        ing_by_id[str(ing["id"])] = ing

    matched = []
    result_by_idx = {r["idx"]: r for r in results}
    for i in range(len(items)):
        r = result_by_idx.get(i, {})
        ing_id = r.get("ingredient_id")
        if ing_id is not None:
            found = ing_by_id.get(ing_id) or ing_by_id.get(str(ing_id))
            matched.append(found)
        else:
            matched.append(None)

    return matched


def get_top_candidates(name: str, ingredients: list[dict], n: int = 8) -> list[dict]:
    """
    Get top N candidate ingredients for manual selection UI.
    Uses fuzzy scoring for ranking the list shown to user.
    """
    name_lower = name.lower().strip()
    scored = []
    for ing in ingredients:
        ing_lower = ing["name"].lower()
        ratio = difflib.SequenceMatcher(None, name_lower, ing_lower).ratio()
        # Boost for substring word matches
        for word in name_lower.split():
            if len(word) >= 3 and word in ing_lower:
                ratio = max(ratio, 0.5)
        for word in ing_lower.split():
            if len(word) >= 3 and word in name_lower:
                ratio = max(ratio, 0.45)
        scored.append((ratio, ing))

    scored.sort(key=lambda x: -x[0])
    return [ing for _, ing in scored[:n]]


def match_ingredient_fuzzy(name: str, ingredients: list[dict], threshold: float = 0.35) -> Optional[dict]:
    """Fuzzy fallback matcher (used only when Claude semantic match fails)."""
    if not ingredients:
        return None

    name_lower = name.lower().strip()
    ingredient_names = [ing["name"].lower() for ing in ingredients]

    matches = difflib.get_close_matches(name_lower, ingredient_names, n=1, cutoff=threshold)
    if matches:
        return ingredients[ingredient_names.index(matches[0])]
    return None


def match_supplier(hint: Optional[str], suppliers: list[dict]) -> Optional[dict]:
    """Try to find supplier by name hint using fuzzy match."""
    if not hint or not suppliers:
        return None
    hint_lower = hint.lower().strip()
    supplier_names = [s["name"].lower() for s in suppliers]
    matches = difflib.get_close_matches(hint_lower, supplier_names, n=1, cutoff=0.4)
    if matches:
        return suppliers[supplier_names.index(matches[0])]
    return None
