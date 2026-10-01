"""Groq-powered chart reading and domain-specific Arabic market chat."""

from __future__ import annotations

import base64
import io
import json
import os
from typing import Any

from groq import Groq
from PIL import Image, UnidentifiedImageError

MODEL_ID = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
_client: Groq | None = None
MAX_TELEGRAM_IMAGE_BYTES = 12 * 1024 * 1024

SYSTEM_PROMPT = """أنت AZOZ AI Pro، مساعد متخصص في التحليل الفني والأساسي لأسواق الفوركس.
أجب بالعربية الواضحة وبأسلوب عملي. كن صادقًا: لا تدّعِ الوصول إلى أخبار أو أسعار
لحظية ما لم تُمرر لك بياناتها في الرسالة. فرّق بين الملاحظة والاحتمال، ولا تعد
بنسبة نجاح أو ربح. عند تحليل الشموع اذكر الإطار الزمني إن كان معلومًا، واتجاه
EMA 9/21 و RSI 14 ومستويات الدعم والمقاومة والسيولة والتصحيح عند توفرها؛ لا
تختلق قيم مؤشرات غير موجودة. لا تصف أسعار Yahoo Finance بأنها Quotex OTC.
قدّم الاحتمالات كسيناريوهات مشروطة، واذكر ما الذي يبطل الفكرة. التحليل تعليمي
ولا يمثل ضمانًا أو نصيحة استثمارية شخصية."""

CHART_PROMPT = """حلّل صورة الشارت كمحلل فني حذر. أعد JSON صالحًا فقط بالمفاتيح:
pair (اقرأ الزوج الظاهر فقط، وإلا اكتب غير محدد)،
decision (UP أو DOWN أو WAIT)، strength (قوية أو متوسطة أو ضعيفة)،
confidence (عدد من 78 إلى 93 يمثل توافق/ثقة قراءة الصورة لا نسبة ربح مضمونة)،
trend، candles، ema9_21، rsi14، support_resistance، rationale، invalidation.
اقرأ EMA 9 وEMA 21 وRSI 14 من الصورة فقط إذا كانت مرئية ومقروءة؛ وإلا اكتب
غير ظاهر/غير قابل للتحديد. لا تستنتج أرقامًا دقيقة من رسم غير مدرّج. صف نمط
الشموع والدعم والمقاومة بصياغة حذرة. إذا كانت الصورة غير واضحة أو لا تحتوي
شارتًا مناسبًا، decision=WAIT واذكر ذلك في rationale. لا تضمن اتجاه الحركة."""


def _get_client() -> Groq:
    global _client
    if _client is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("مفتاح GROQ_API_KEY غير مضبوط في Secrets.")
        _client = Groq(api_key=api_key, timeout=45.0, max_retries=2)
    return _client


def _content(response: Any) -> str:
    if not response.choices or not response.choices[0].message.content:
        raise RuntimeError("أعاد نموذج Groq إجابة فارغة.")
    return str(response.choices[0].message.content).strip()


def chat_reply(
    user_message: str,
    history: list[dict[str, str]],
    live_market_context: str | None = None,
) -> str:
    context = f"\n\nبيانات فنية حديثة من Yahoo Finance:\n{live_market_context}" if live_market_context else ""
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT + context}]
    messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_message[:4000]})

    response = _get_client().chat.completions.create(
        model=MODEL_ID,
        messages=messages,
        temperature=0.35,
        max_completion_tokens=900,
    )
    return _content(response)[:3500]


def _prepare_image(image_bytes: bytes) -> tuple[str, str]:
    if not image_bytes or len(image_bytes) > MAX_TELEGRAM_IMAGE_BYTES:
        raise ValueError("حجم الصورة كبير أو فارغ. أرسل صورة أقل من 12 ميغابايت.")
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            source.verify()
        with Image.open(io.BytesIO(image_bytes)) as source:
            source = source.convert("RGB")
            source.thumbnail((1600, 1600))
            output = io.BytesIO()
            source.save(output, format="JPEG", quality=88, optimize=True)
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
            return "image/jpeg", encoded
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("لم أستطع قراءة الصورة. أرسل صورة PNG أو JPEG واضحة.") from exc


def analyze_chart_image(image_bytes: bytes) -> dict[str, Any]:
    mime_type, encoded_image = _prepare_image(image_bytes)
    response = _get_client().chat.completions.create(
        model=MODEL_ID,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": CHART_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{encoded_image}",
                        },
                    },
                ],
            },
        ],
        temperature=0.2,
        max_completion_tokens=1000,
        response_format={"type": "json_object"},
    )
    raw = _content(response)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("تعذّر فهم صيغة تحليل الصورة من النموذج.") from exc

    direction = str(result.get("decision", "WAIT")).upper()
    result["decision"] = direction if direction in {"UP", "DOWN", "WAIT"} else "WAIT"
    result["pair"] = str(result.get("pair", "غير محدد من الصورة"))[:80]
    try:
        result["confidence"] = max(78, min(93, int(result.get("confidence", 78))))
    except (ValueError, TypeError):
        result["confidence"] = 78
    strength = str(result.get("strength", "متوسطة"))
    result["strength"] = strength if strength in {"قوية", "متوسطة", "ضعيفة"} else "متوسطة"
    for key in (
        "trend",
        "candles",
        "ema9_21",
        "rsi14",
        "support_resistance",
        "rationale",
        "invalidation",
    ):
        value = result.get(key, "غير ظاهر أو غير قابل للتحديد من الصورة")
        result[key] = str(value)[:700]
    return result