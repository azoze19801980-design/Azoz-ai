"""AZOZ AI Pro Telegram bot entry point."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import telebot
from telebot import types

from telegram_bot.ai import analyze_chart_image, chat_reply
from telegram_bot.market import (
    analyze_pair_from_message,
    format_confidence_notice,
    format_explanation,
    format_signal,
    scan_market,
)
from telegram_bot.storage import load_subscribers, save_subscribers

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("azoz_ai_pro")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("أضف TELEGRAM_BOT_TOKEN إلى Secrets لتشغيل البوت.")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8)
subscribers = load_subscribers()
subscribers_lock = threading.Lock()
state_lock = threading.Lock()
waiting_for_chart: set[int] = set()
chat_history: dict[int, list[dict[str, str]]] = {}
auto_signal_interval = 180

BTN_MARKET = "🔍 فحص السوق"
BTN_CHART = "📷 تحليل صورة شارت"
BTN_AUTO_ON = "⚡ تفعيل الاشارات المباشرة"
BTN_AUTO_OFF = "⛔ ايقاف الاشارات"
BTN_AI_CHAT = "🤖 محادثة مع الذكاء الاصطناعي"


def main_keyboard() -> types.ReplyKeyboardMarkup:
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.row(types.KeyboardButton(BTN_MARKET), types.KeyboardButton(BTN_CHART))
    keyboard.row(types.KeyboardButton(BTN_AUTO_ON), types.KeyboardButton(BTN_AUTO_OFF))
    keyboard.row(types.KeyboardButton(BTN_AI_CHAT))
    return keyboard


def _send_market_scan(chat_id: int) -> None:
    bot.send_message(chat_id, "أفحص أزواج الفوركس المتاحة الآن من Yahoo Finance…")
    signals = scan_market(limit=3)
    if not signals:
        bot.send_message(
            chat_id,
            "لم تتوفر 25 شمعة دقيقة كافية من Yahoo Finance لهذه الأزواج الآن. "
            "قد يكون السوق مغلقًا أو المصدر لا يعرض بيانات لحظية.",
        )
        return
    for signal in signals:
        bot.send_message(chat_id, format_signal(signal))
        bot.send_message(chat_id, format_explanation(signal))
    bot.send_message(chat_id, format_confidence_notice())


def _signal_scheduler() -> None:
    while True:
        time.sleep(auto_signal_interval)
        with subscribers_lock:
            recipient_ids = tuple(subscribers)
        if not recipient_ids:
            continue
        try:
            signals = scan_market(limit=6)
            actionable = [
                signal
                for signal in signals
                if signal.direction != "WAIT" and signal.strength != "ضعيفة"
            ]
            if not actionable:
                logger.info("Automatic scan: no directional setup with enough data.")
                continue
            best = actionable[0]
            for chat_id in recipient_ids:
                try:
                    bot.send_message(chat_id, format_signal(best))
                    bot.send_message(chat_id, format_explanation(best))
                except Exception as exc:
                    logger.warning("Signal delivery failed (%s)", type(exc).__name__)
        except Exception as exc:
            logger.error("Automatic market scan failed (%s)", type(exc).__name__)


def _set_subscriber(chat_id: int, enabled: bool) -> None:
    with subscribers_lock:
        if enabled:
            subscribers.add(chat_id)
        else:
            subscribers.discard(chat_id)
        save_subscribers(subscribers)


@bot.message_handler(commands=["start", "help"])
def start(message: types.Message) -> None:
    chat_id = message.chat.id
    with state_lock:
        waiting_for_chart.discard(chat_id)
        chat_history.pop(chat_id, None)
    intro = (
        "أهلًا بك في AZOZ AI Pro، مساعدك لتحليل الفوركس بإشراف المطور عزوز.\n\n"
        "اختر فحص السوق للحصول على أفضل 3 قراءات فنية عند توفر البيانات، أو أرسل "
        "صورة شارت لتحليلها عبر Groq Vision، أو ابدأ محادثة متخصصة.\n\n"
        "بيانات السوق من Yahoo Finance هي أسعار فوركس فورية استرشادية وليست "
        "أسعار Quotex OTC. نسبة الإشارة هي توافق فني وليست نسبة ربح مثبتة أو ضمانًا. "
        "التحليل تعليمي وقد تخسر رأس المال."
    )
    bot.send_message(chat_id, intro, reply_markup=main_keyboard())


@bot.message_handler(commands=["auto_on"])
def auto_on(message: types.Message) -> None:
    _set_subscriber(message.chat.id, True)
    bot.send_message(
        message.chat.id,
        "تم تفعيل الإشارات. سأرسل أفضل قراءة اتجاهية كل 3 دقائق عند توفر بيانات كافية. "
        "أوقفها عبر /auto_off.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["auto_off"])
def auto_off(message: types.Message) -> None:
    _set_subscriber(message.chat.id, False)
    bot.send_message(
        message.chat.id,
        "تم إيقاف الإشارات المباشرة.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["chat"])
def begin_chat(message: types.Message) -> None:
    with state_lock:
        chat_history.setdefault(message.chat.id, [])
    bot.send_message(
        message.chat.id,
        "أنا جاهز لمناقشة التحليل الفني والأساسي. اكتب الزوج أو سؤالك، "
        "ويمكنك إرسال صورة الشارت عبر زر التحليل.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(func=lambda message: message.text == BTN_MARKET, content_types=["text"])
def market_button(message: types.Message) -> None:
    _send_market_scan(message.chat.id)


@bot.message_handler(func=lambda message: message.text == BTN_CHART, content_types=["text"])
def chart_button(message: types.Message) -> None:
    with state_lock:
        waiting_for_chart.add(message.chat.id)
    bot.send_message(
        message.chat.id,
        "أرسل صورة واضحة للشارت الآن. سأحلل الاتجاه والشموع والمؤشرات الظاهرة "
        "والدعم والمقاومة، ولن أخمّن قيمًا غير مقروءة.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(func=lambda message: message.text == BTN_AUTO_ON, content_types=["text"])
def auto_on_button(message: types.Message) -> None:
    auto_on(message)


@bot.message_handler(func=lambda message: message.text == BTN_AUTO_OFF, content_types=["text"])
def auto_off_button(message: types.Message) -> None:
    auto_off(message)


@bot.message_handler(func=lambda message: message.text == BTN_AI_CHAT, content_types=["text"])
def ai_chat_button(message: types.Message) -> None:
    begin_chat(message)


def _format_chart_result(result: dict[str, Any]) -> tuple[str, str]:
    direction = result["decision"]
    direction_ar = {"UP": "صعود", "DOWN": "هبوط", "WAIT": "انتظار"}[direction]
    if result["strength"] == "متوسطة" or direction == "WAIT":
        icon = "🟡"
    else:
        icon = "🟢" if direction == "UP" else "🔴"
    now = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%H:%M")
    signal = (
        f"📊 الزوج: {result['pair']}\n"
        f"📈 الفرصة: {direction_ar} {icon}\n"
        f"⏰ وقت الدخول: الآن {now}\n"
        f"⏳ مدة الصفقة: 1 دقيقة\n"
        f"🎯 توافق المؤشرات: {result['confidence']}%\n"
        f"قوة الاشارة: {result['strength']}"
    )
    explanation = (
        f"🧭 الاتجاه: {result['trend']}\n"
        f"🕯️ الشموع: {result['candles']}\n"
        f"EMA 9/21: {result['ema9_21']}\n"
        f"RSI 14: {result['rsi14']}\n"
        f"الدعم والمقاومة: {result['support_resistance']}\n"
        f"التحليل: {result['rationale']}\n"
        f"ما يبطل السيناريو: {result['invalidation']}\n\n"
        "النسبة تقدير لتوافق قراءة الصورة وليست نتيجة تداول مثبتة أو ضمان ربح."
    )
    return signal, explanation


@bot.message_handler(content_types=["photo"])
def photo_message(message: types.Message) -> None:
    chat_id = message.chat.id
    with state_lock:
        waiting_for_chart.discard(chat_id)
    try:
        photo = message.photo[-1]
        if photo.file_size and photo.file_size > 12 * 1024 * 1024:
            bot.send_message(chat_id, "الصورة تتجاوز 12 ميغابايت. أرسل نسخة أصغر.")
            return
        file_info = bot.get_file(photo.file_id)
        image_bytes = bot.download_file(file_info.file_path)
        bot.send_message(chat_id, "وصلت الصورة. أحلل الشارت الآن…")
        result = analyze_chart_image(image_bytes)
        signal, explanation = _format_chart_result(result)
        bot.send_message(chat_id, signal)
        bot.send_message(chat_id, explanation)
    except ValueError as exc:
        bot.send_message(chat_id, str(exc))
    except Exception as exc:
        logger.error("Chart analysis failed (%s)", type(exc).__name__)
        bot.send_message(
            chat_id,
            "تعذّر تحليل الصورة الآن. تحقق من مفتاح Groq أو جرّب صورة أوضح بعد قليل.",
        )


@bot.message_handler(content_types=["text"])
def text_message(message: types.Message) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    chat_id = message.chat.id
    if text == BTN_MARKET or text == BTN_CHART:
        return
    try:
        signal = analyze_pair_from_message(text)
        live_context = None
        if signal is not None:
            live_context = (
                f"{signal.pair}: الاتجاه {signal.direction}، السعر {signal.price:.6g}، "
                f"EMA9={signal.ema9:.6g}، EMA21={signal.ema21:.6g}، "
                f"RSI14={signal.rsi14:.1f}، الدعم={signal.support:.6g}، "
                f"المقاومة={signal.resistance:.6g}. "
                f"البيانات من Yahoo Finance وليست أسعار OTC."
            )
        with state_lock:
            history = chat_history.setdefault(chat_id, [])
            history_copy = list(history)
        answer = chat_reply(text, history_copy, live_context)
        with state_lock:
            history = chat_history.setdefault(chat_id, [])
            history.extend(
                [
                    {"role": "user", "content": text[:4000]},
                    {"role": "assistant", "content": answer},
                ]
            )
            chat_history[chat_id] = history[-12:]
        bot.send_message(chat_id, answer, reply_markup=main_keyboard())
    except Exception as exc:
        logger.error("AI chat failed (%s)", type(exc).__name__)
        bot.send_message(
            chat_id,
            "تعذّر الوصول إلى Groq الآن. تأكد من إعداد GROQ_API_KEY وحصة الاستخدام.",
            reply_markup=main_keyboard(),
        )


def main() -> None:
    scheduler = threading.Thread(
        target=_signal_scheduler,
        name="azoz-signal-scheduler",
        daemon=True,
    )
    scheduler.start()
    logger.info("AZOZ AI Pro is starting Telegram polling.")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    main()