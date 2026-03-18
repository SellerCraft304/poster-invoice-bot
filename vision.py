import anthropic
import base64
import difflib
import os
from typing import Optional

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


EXTRACT_PROMPT = """
Ты — ассистент для ресторана. Тебе прислали фото накладной (товарная накладная / invoice).
Извлеки список товаров из накладной.

Верни ТОЛЬКО валидный JSON в таком формате (без лишнего текста):
{
  "items": [
    {"name": "название товара", "quantity": 1.5, "unit": "кг", "price_per_unit": 250.0},
    ...
  ],
  "supplier_hint": "название поставщика если видно на накладной, иначе null",
  "date": "дата накладной если видна (YYYY-MM-DD), иначе null"
}

Важно:
- quantity — числовое значение количества
- price_per_unit — цена за единицу (если указана цена за всё — раздели на quantity)
- Если цены нет — ставь 0
- Названия оставляй как есть в накладной
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


def match_ingredient(name: str, ingredients: list[dict], threshold: float = 0.4) -> Optional[dict]:
    """Find best matching ingredient from Poster by name using fuzzy match."""
    if not ingredients:
        return None

    name_lower = name.lower().strip()
    ingredient_names = [ing["name"].lower() for ing in ingredients]

    matches = difflib.get_close_matches(name_lower, ingredient_names, n=1, cutoff=threshold)
    if matches:
        idx = ingredient_names.index(matches[0])
        return ingredients[idx]
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
