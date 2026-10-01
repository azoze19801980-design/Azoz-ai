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

from telegram_bot.ai import analyze_chart_image, analyze_currency_impact, chat_reply
from telegram_bot.economic_news import (
    CalendarEvent,
    OfficialStory,
    affected_pairs,
    arabic_direction,
    due_alert_window,
    fetch_official_news,
    fetch_upcoming_events,
    pair_direction,
)
from telegram_bot.market import (
    analyze_pair_from_message,
    format_confidence_notice,
    format_explanation,
    format_signal,
    scan_market,
)
from telegram_bot.storage import (
    load_news_state,
    load_news_subscribers,
    load_subscribers,
    save_news_state,
    save_news_subscribers,
    save_subscribers,
)

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
news_subscribers = load_news_subscribers()
news_subscribers_lock = threading.Lock()
news_state = load_news_state()
news_state_lock = threading.RLock()
state_lock = threading.Lock()
waiting_for_chart: set[int] = set()
chat_history: dict[int, list[dict[str, str]]] = {}
auto_signal_interval = 180
news_check_interval = 30

BTN_MARKET = "🔍 فحص السوق"
BTN_CHART = "📷 تحليل صورة شارت"
BTN_AUTO_ON = "⚡ تفعيل الاشارات المباشرة"
BTN_AUTO_OFF = "⛔ ايقاف الاشارات"
BTN_NEWS_ON = "🗞️ تفعيل تنبيهات الأخبار"
BTN_NEWS_OFF = "🔕 إيقاف تنبيهات الأخبار"
BTN_AI_CHAT = "🤖 محادثة مع الذكاء الاصطناعي"


def main_keyboard() -> types.ReplyKeyboardMarkup:
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.row(types.KeyboardButton(BTN_MARKET), types.KeyboardButton(BTN_CHART))
    keyboard.row(types.KeyboardButton(BTN_AUTO_ON), types.KeyboardButton(BTN_AUTO_OFF))
    keyboard.row(types.KeyboardButton(BTN_NEWS_ON), types.KeyboardButton(BTN_NEWS_OFF))
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


def _impact_for_message(
    key: str,
    currency: str,
    title: str,
    details: str,
    scheduled_only: bool,
) -> dict[str, str]:
    with news_state_lock:
        cached = news_state["outlooks"].get(key)
        if isinstance(cached, dict):
            return {str(name): str(value) for name, value in cached.items()}

    try:
        impact = analyze_currency_impact(
            currency=currency,
            title=title,
            details=details,
            scheduled_only=scheduled_only,
        )
    except Exception as exc:
        logger.warning("Economic-news analysis unavailable (%s)", type(exc).__name__)
        impact = {
            "currency_bias": "UNCERTAIN",
            "rationale": "لم تتوفر قراءة آلية موثوقة؛ لا يمكن تحديد اتجاه واحد.",
            "supportive_scenario": "إذا جاءت القراءة أقوى من توقعات السوق فقد تقوى العملة.",
            "negative_scenario": "إذا جاءت القراءة أضعف من التوقعات فقد تضعف العملة.",
        }

    with news_state_lock:
        outlooks = news_state["outlooks"]
        if isinstance(outlooks, dict):
            existing = outlooks.setdefault(key, impact)
            impact = {str(name): str(value) for name, value in existing.items()}
            save_news_state(news_state)
    return impact


def _format_currency_outlook(
    currency: str,
    pairs: list[str],
    impact: dict[str, str],
    scheduled_only: bool,
) -> str:
    bias = impact.get("currency_bias", "UNCERTAIN").upper()
    bias_ar = {
        "STRONGER": f"ميل أولي إلى قوة {currency}",
        "WEAKER": f"ميل أولي إلى ضعف {currency}",
        "UNCERTAIN": "غير محسوم قبل صدور البيانات",
    }.get(bias, "غير محسوم قبل صدور البيانات")
    lines = [f"🤖 التقدير الاحتمالي: {bias_ar}."]
    if pairs:
        if bias in {"STRONGER", "WEAKER"}:
            lines.append(
                "اتجاه الأزواج المتأثرة: "
                + "، ".join(
                    f"{pair} {arabic_direction(pair_direction(pair, currency, bias))}"
                    for pair in pairs
                )
                + "."
            )
        else:
            scenarios: list[str] = []
            for pair in pairs:
                stronger = arabic_direction(pair_direction(pair, currency, "STRONGER"))
                weaker = arabic_direction(pair_direction(pair, currency, "WEAKER"))
                scenarios.append(f"{pair}: {stronger} إذا قويت {currency} / {weaker} إذا ضعفت")
            lines.append("سيناريوهات الزوج: " + "؛ ".join(scenarios) + ".")

    rationale = impact.get("rationale", "").strip()
    if rationale:
        lines.append(f"السبب: {rationale}")
    supportive = impact.get("supportive_scenario", "").strip()
    negative = impact.get("negative_scenario", "").strip()
    if bias == "UNCERTAIN" and (supportive or negative):
        if supportive:
            lines.append(f"سيناريو داعم: {supportive}")
        if negative:
            lines.append(f"سيناريو معاكس: {negative}")
    if scheduled_only:
        lines.append("المصدر المجاني يعرض الموعد فقط، ولا يوفر إجماع السوق أو قراءة فعلية مسبقة.")
    else:
        lines.append("قد لا يتضمن الموجز مقارنة بتوقعات السوق؛ مفاجأة الخبر هي ما يحرك السعر غالبًا.")
    return "\n".join(lines)


def _format_event_alert(event: CalendarEvent, window: str, impact: dict[str, str]) -> str:
    minutes = "30 دقيقة" if window == "30m" else "5 دقائق"
    pairs = affected_pairs(event.currency)
    return (
        f"📅 تنبيه قبل خبر اقتصادي — قبل {minutes}\n"
        f"الحدث: {event.title}\n"
        f"العملة: {event.currency}\n"
        f"الوقت بتوقيت إسطنبول: {event.scheduled_at:%Y-%m-%d %H:%M}\n"
        f"الأزواج المحتمل تأثرها: {', '.join(pairs) or 'غير محددة'}\n\n"
        f"{_format_currency_outlook(event.currency, pairs, impact, scheduled_only=True)}\n\n"
        f"المصدر والجدول: {event.source_url}\n"
        "تنبيه تعليمي فقط؛ لا يضمن اتجاه السوق أو نتيجة صفقة."
    )


def _format_story_alert(story: OfficialStory, impact: dict[str, str]) -> str:
    pairs = affected_pairs(story.currency)
    summary = f"\nالخلاصة الرسمية: {story.summary}" if story.summary else ""
    link = f"\nالرابط: {story.link}" if story.link else ""
    return (
        f"📰 خبر اقتصادي رسمي مؤثر\n"
        f"المصدر: {story.source}\n"
        f"العنوان: {story.title}"
        f"{summary}\n"
        f"العملة: {story.currency} | الأزواج: {', '.join(pairs) or 'غير محددة'}\n\n"
        f"{_format_currency_outlook(story.currency, pairs, impact, scheduled_only=False)}"
        f"{link}\n"
        "تحليل احتمالي تعليمي، وليس توصية تداول أو ضمانًا."
    )


def _send_news_message(chat_id: int, delivery_key: str, text: str) -> None:
    stored_key = f"{chat_id}:{delivery_key}"
    with news_state_lock:
        delivered = news_state["delivered"]
        if not isinstance(delivered, set) or stored_key in delivered:
            return
    try:
        bot.send_message(chat_id, text, disable_web_page_preview=True)
    except Exception as exc:
        logger.warning("Economic-news delivery failed (%s)", type(exc).__name__)
        return
    with news_state_lock:
        delivered = news_state["delivered"]
        if isinstance(delivered, set):
            delivered.add(stored_key)
            save_news_state(news_state)


def _broadcast_event(event: CalendarEvent, window: str, recipients: tuple[int, ...]) -> None:
    impact = _impact_for_message(
        key=event.key,
        currency=event.currency,
        title=event.title,
        details="",
        scheduled_only=True,
    )
    message = _format_event_alert(event, window, impact)
    for chat_id in recipients:
        _send_news_message(chat_id, f"{event.key}:{window}", message)


def _broadcast_story(story: OfficialStory, recipients: tuple[int, ...]) -> None:
    impact = _impact_for_message(
        key=story.key,
        currency=story.currency,
        title=story.title,
        details=story.summary,
        scheduled_only=False,
    )
    message = _format_story_alert(story, impact)
    for chat_id in recipients:
        _send_news_message(chat_id, story.key, message)


def _economic_news_scheduler() -> None:
    cached_events: list[CalendarEvent] = []
    next_calendar_fetch = 0.0
    next_news_fetch = 0.0
    while True:
        with news_subscribers_lock:
            recipient_ids = tuple(news_subscribers)
        if not recipient_ids:
            time.sleep(news_check_interval)
            continue

        now = datetime.now(ZoneInfo("Europe/Istanbul"))
        monotonic_now = time.monotonic()
        if monotonic_now >= next_calendar_fetch:
            try:
                cached_events = fetch_upcoming_events(now=now)
                logger.info("Economic calendar refreshed (%s events).", len(cached_events))
                next_calendar_fetch = monotonic_now + 900
            except Exception as exc:
                logger.warning("Economic calendar refresh failed (%s)", type(exc).__name__)
                next_calendar_fetch = monotonic_now + 120

        if monotonic_now >= next_news_fetch:
            try:
                stories = fetch_official_news(now=now)
                for story in stories:
                    _broadcast_story(story, recipient_ids)
                next_news_fetch = monotonic_now + 180
            except Exception as exc:
                logger.warning("Official news refresh failed (%s)", type(exc).__name__)
                next_news_fetch = monotonic_now + 120

        for event in cached_events:
            window = due_alert_window(event.scheduled_at, now=now)
            if window:
                _broadcast_event(event, window, recipient_ids)
        time.sleep(news_check_interval)


def _set_subscriber(chat_id: int, enabled: bool) -> None:
    with subscribers_lock:
        if enabled:
            subscribers.add(chat_id)
        else:
            subscribers.discard(chat_id)
        save_subscribers(subscribers)


def _set_news_subscriber(chat_id: int, enabled: bool) -> None:
    with news_subscribers_lock:
        if enabled:
            news_subscribers.add(chat_id)
        else:
            news_subscribers.discard(chat_id)
        save_news_subscribers(news_subscribers)


@bot.message_handler(commands=["start", "help"])
def start(message: types.Message) -> None:
    chat_id = message.chat.id
    with state_lock:
        waiting_for_chart.discard(chat_id)
        chat_history.pop(chat_id, None)
    intro = (
        "أهلًا بك في AZOZ AI Pro، مساعدك لتحليل الفوركس بإشراف المطور عزوز.\n\n"
        "اختر فحص السوق للحصول على أفضل 3 قراءات فنية عند توفر البيانات، أو أرسل "
        "صورة شارت لتحليلها عبر Groq Vision، أو ابدأ محادثة متخصصة. "
        "يمكنك تفعيل تنبيهات الأخبار الاقتصادية عبر /news_on.\n\n"
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


@bot.message_handler(commands=["news_on"])
def news_on(message: types.Message) -> None:
    _set_news_subscriber(message.chat.id, True)
    bot.send_message(
        message.chat.id,
        "تم تفعيل تنبيهات الأخبار الرسمية. سأرسل إشعارًا قبل بعض الإصدارات "
        "الاقتصادية المهمة بنحو 30 و5 دقائق، إضافة إلى الأخبار الرسمية المؤثرة "
        "عند نشرها. التقدير احتمالي ومشروط؛ المصادر المجانية لا توفر إجماع السوق "
        "لكل حدث، لذلك قد يكون الاتجاه «غير محسوم». أوقفها عبر /news_off.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["news_off"])
def news_off(message: types.Message) -> None:
    _set_news_subscriber(message.chat.id, False)
    bot.send_message(
        message.chat.id,
        "تم إيقاف تنبيهات الأخبار الاقتصادية.",
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


@bot.message_handler(func=lambda message: message.text == BTN_NEWS_ON, content_types=["text"])
def news_on_button(message: types.Message) -> None:
    news_on(message)


@bot.message_handler(func=lambda message: message.text == BTN_NEWS_OFF, content_types=["text"])
def news_off_button(message: types.Message) -> None:
    news_off(message)


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
    if text in {BTN_MARKET, BTN_CHART, BTN_AUTO_ON, BTN_AUTO_OFF, BTN_NEWS_ON, BTN_NEWS_OFF, BTN_AI_CHAT}:
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
    news_scheduler = threading.Thread(
        target=_economic_news_scheduler,
        name="azoz-economic-news-scheduler",
        daemon=True,
    )
    news_scheduler.start()
    logger.info("AZOZ AI Pro is starting Telegram polling.")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    main()