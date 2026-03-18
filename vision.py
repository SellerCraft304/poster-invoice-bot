import anthropic
import base64
import difflib
import os
from typing import Optional

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


EXTRACT_PROMPT = """
Ты — ассистент для ресторана. Тебе прислали фото накладной (товарная накладная / invoice).
Накладная может быть на любом языке (турецкий, английский, русский и др.).

Извлеки список товаров и ОБЯЗАТЕЛЬНО переведи названия на русский язык.

Верни ТОЛЬКО валидный JSON в таком формате (без лишнего текста):
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
- Примеры перевода: MAYONEZ → Майонез, PİRİNÇ → Рис, TOST PEYNİRİ → Тостовый сыр, KAŞAR → Сыр кашар, TEREYAĞI → Масло сливочное, DOMATES → Помидор, SOĞAN → Лук, ET → Мясо, TAVUK → Курица, YAĞ → Масло, UN → Мука, ŞEKER → Сахар, TUZ → Соль
- quantity — числовое значение количества
- unit — единица измерения на русском (кг, г, шт, л, уп, пач)
- price_per_unit — цена за единицу (если указана цена за всё — раздели на quantity)
- Если цены нет — ставь 0
- ВАЖНО для HoReCa: включай граммовку/объём в название если это важно для идентификации товара.
  Примеры: "Майонез 840г", "Рис жасмин 1кг", "Сыр тостовый 1кг", "Дрожжи 42г".
  Это поможет точно сопоставить товар со складом.
"""


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
    # Strip markdown code blocks if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])

    import json
    return json.loads(text)


def match_ingredient(name: str, ingredients: list[dict], threshold: float = 0.35) -> Optional[dict]:
    """Find best matching ingredient from Poster by name using fuzzy match + substring search."""
    if not ingredients:
        return None

    name_lower = name.lower().strip()
    ingredient_names = [ing["name"].lower() for ing in ingredients]

    # 1. Exact fuzzy match
    matches = difflib.get_close_matches(name_lower, ingredient_names, n=1, cutoff=threshold)
    if matches:
        idx = ingredient_names.index(matches[0])
        return ingredients[idx]

    # 2. Substring match — если слово из названия входит в имя ингредиента
    name_words = name_lower.split()
    best_score = 0
    best_idx = None
    for i, ing_name in enumerate(ingredient_names):
        for word in name_words:
            if len(word) >= 3 and word in ing_name:
                score = len(word) / max(len(ing_name), 1)
                if score > best_score:
                    best_score = score
                    best_idx = i
        # also check reverse
        ing_words = ing_name.split()
        for word in ing_words:
            if len(word) >= 3 and word in name_lower:
                score = len(word) / max(len(name_lower), 1)
                if score > best_score:
                    best_score = score
                    best_idx = i

    if best_idx is not None and best_score >= 0.3:
        return ingredients[best_idx]

    return None


def match_supplier(hint: Optional[str], suppliers: list[dict]) -> Optional[dict]:
    """Try to find supplier by name hint."""
    if not hint or not suppliers:
        return None
    hint_lower = hint.lower().strip()
    supplier_names = [s["name"].lower() for s in suppliers]
    matches = difflib.get_close_matches(hint_lower, supplier_names, n=1, cutoff=0.4)
    if matches:
        idx = supplier_names.index(matches[0])
        return suppliers[idx]
    return None
