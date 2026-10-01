"""Yahoo Finance spot-FX data and transparent, rule-based technical signals."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

MIN_CANDLES = 25
MARKET_TIMEZONE = ZoneInfo("Europe/Istanbul")

# Yahoo Finance FX symbols are indicative spot rates, not Quotex OTC prices.
PAIRS: tuple[tuple[str, str], ...] = (
    ("EUR/USD", "EURUSD=X"),
    ("GBP/USD", "GBPUSD=X"),
    ("USD/JPY", "JPY=X"),
    ("EUR/GBP", "EURGBP=X"),
    ("AUD/CAD", "AUDCAD=X"),
    ("GBP/JPY", "GBPJPY=X"),
)


@dataclass(frozen=True)
class MarketSignal:
    pair: str
    direction: str
    strength: str
    score: int
    confidence: int
    price: float
    ema9: float
    ema21: float
    rsi14: float
    support: float
    resistance: float
    candle_pattern: str
    rationale: str
    observed_at: datetime


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    change = series.diff()
    gains = change.clip(lower=0)
    losses = -change.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    average_loss = losses.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    relative_strength = average_gain / average_loss.replace(0, float("nan"))
    result = 100 - (100 / (1 + relative_strength))
    # A run of gains with no losses has RSI 100, not an unknown value.
    result = result.mask((average_loss == 0) & (average_gain > 0), 100)
    return result.mask((average_loss == 0) & (average_gain == 0), 50)


def _candle_pattern(candles: pd.DataFrame) -> tuple[str, int]:
    previous = candles.iloc[-2]
    current = candles.iloc[-1]
    previous_open, previous_close = float(previous["Open"]), float(previous["Close"])
    current_open, current_close = float(current["Open"]), float(current["Close"])

    if (
        previous_close < previous_open
        and current_close > current_open
        and current_open <= previous_close
        and current_close >= previous_open
    ):
        return "ابتلاع شرائي", 1
    if (
        previous_close > previous_open
        and current_close < current_open
        and current_open >= previous_close
        and current_close <= previous_open
    ):
        return "ابتلاع بيعي", -1

    candle_range = max(float(current["High"]) - float(current["Low"]), 1e-12)
    body = abs(current_close - current_open)
    lower_wick = min(current_open, current_close) - float(current["Low"])
    upper_wick = float(current["High"]) - max(current_open, current_close)
    if lower_wick > body * 1.8 and lower_wick > upper_wick * 1.3:
        return "ذيل سفلي طويل", 1
    if upper_wick > body * 1.8 and upper_wick > lower_wick * 1.3:
        return "ذيل علوي طويل", -1
    if current_close > current_open and body / candle_range > 0.55:
        return "شمعة صاعدة قوية", 1
    if current_close < current_open and body / candle_range > 0.55:
        return "شمعة هابطة قوية", -1
    return "لا يظهر نمط شموع حاسم", 0


def _analyze_history(pair: str, history: pd.DataFrame) -> MarketSignal | None:
    required = {"Open", "High", "Low", "Close"}
    if history is None or history.empty or not required.issubset(history.columns):
        return None

    candles = history.dropna(subset=list(required)).tail(MIN_CANDLES)
    if len(candles) < MIN_CANDLES:
        return None
    last_index = candles.index[-1]
    if isinstance(last_index, pd.Timestamp) and not pd.isna(last_index):
        last_time = last_index.to_pydatetime()
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=MARKET_TIMEZONE)
        current_time = datetime.now(last_time.tzinfo)
        age_seconds = (current_time - last_time).total_seconds()
        if age_seconds < -120 or age_seconds > 15 * 60:
            return None

    closes = candles["Close"].astype(float)
    ema9_series = closes.ewm(span=9, adjust=False).mean()
    ema21_series = closes.ewm(span=21, adjust=False).mean()
    rsi_series = _rsi(closes)
    price = float(closes.iloc[-1])
    ema9 = float(ema9_series.iloc[-1])
    ema21 = float(ema21_series.iloc[-1])
    rsi14 = float(rsi_series.iloc[-1])
    if not all(pd.notna(value) for value in (price, ema9, ema21, rsi14)):
        return None

    support = float(candles["Low"].astype(float).tail(MIN_CANDLES).min())
    resistance = float(candles["High"].astype(float).tail(MIN_CANDLES).max())
    pattern, candle_bias = _candle_pattern(candles)
    up_score = 0
    down_score = 0
    reasons: list[str] = []

    if ema9 > ema21:
        up_score += 2
        reasons.append("EMA 9 أعلى من EMA 21")
    elif ema9 < ema21:
        down_score += 2
        reasons.append("EMA 9 أدنى من EMA 21")

    if price > ema9:
        up_score += 1
        reasons.append("السعر أعلى EMA 9")
    elif price < ema9:
        down_score += 1
        reasons.append("السعر أدنى EMA 9")

    if 50 <= rsi14 < 70:
        up_score += 1
        reasons.append("RSI يميل للإيجابية دون تشبع شرائي")
    elif 30 < rsi14 < 50:
        down_score += 1
        reasons.append("RSI يميل للسلبية دون تشبع بيعي")
    elif rsi14 >= 70:
        down_score += 1
        reasons.append("RSI في منطقة تشبع شرائي")
    elif rsi14 <= 30:
        up_score += 1
        reasons.append("RSI في منطقة تشبع بيعي")

    if candle_bias > 0:
        up_score += 1
        reasons.append(pattern)
    elif candle_bias < 0:
        down_score += 1
        reasons.append(pattern)

    proximity = max(abs(price) * 0.0015, 1e-8)
    if price - support <= proximity:
        up_score += 1
        reasons.append("السعر قريب من دعم آخر 25 شمعة")
    if resistance - price <= proximity:
        down_score += 1
        reasons.append("السعر قريب من مقاومة آخر 25 شمعة")

    difference = up_score - down_score
    if difference > 0:
        direction = "UP"
        alignment = up_score
    elif difference < 0:
        direction = "DOWN"
        alignment = down_score
    else:
        direction = "WAIT"
        alignment = max(up_score, down_score)

    strength = "قوية" if abs(difference) >= 4 else "متوسطة" if abs(difference) >= 2 else "ضعيفة"
    # This is a bounded indicator-confluence score, not a measured win probability.
    confidence = max(78, min(93, 78 + min(alignment, 5) * 3))
    if not reasons:
        reasons.append("المؤشرات لا تعطي اتجاهًا واضحًا")

    return MarketSignal(
        pair=pair,
        direction=direction,
        strength=strength,
        score=abs(difference),
        confidence=confidence,
        price=price,
        ema9=ema9,
        ema21=ema21,
        rsi14=rsi14,
        support=support,
        resistance=resistance,
        candle_pattern=pattern,
        rationale="، ".join(dict.fromkeys(reasons)),
        observed_at=datetime.now(MARKET_TIMEZONE),
    )


def _fetch_pair(pair: str, ticker_symbol: str) -> MarketSignal | None:
    try:
        history = yf.Ticker(ticker_symbol).history(
            period="1d",
            interval="1m",
            auto_adjust=False,
            prepost=False,
            timeout=12,
        )
        # Yahoo can return fewer rows than requested. A minimum of 25 is
        # intentional; requiring 100 would reject valid short histories.
        return _analyze_history(pair, history)
    except Exception as exc:
        # Avoid logging provider error details, which may include request data.
        import logging

        logging.getLogger(__name__).warning(
            "Yahoo Finance request failed for %s (%s)", pair, type(exc).__name__
        )
        return None


def scan_market(limit: int = 3) -> list[MarketSignal]:
    results: list[MarketSignal] = []
    with ThreadPoolExecutor(max_workers=len(PAIRS)) as executor:
        futures = [executor.submit(_fetch_pair, pair, ticker) for pair, ticker in PAIRS]
        for future in as_completed(futures):
            signal = future.result()
            if signal is not None:
                results.append(signal)

    results = [signal for signal in results if signal.direction != "WAIT"]
    results.sort(
        key=lambda item: (
            item.score,
            item.confidence,
        ),
        reverse=True,
    )
    return results[: max(1, min(limit, len(results)))]


def analyze_pair_from_message(message: str) -> MarketSignal | None:
    normalized = message.upper().replace(" ", "")
    for pair, ticker in PAIRS:
        compact_pair = pair.replace("/", "")
        if pair.upper() in message.upper() or compact_pair in normalized:
            return _fetch_pair(pair, ticker)
    return None


def format_signal(signal: MarketSignal) -> str:
    direction_ar = {"UP": "صعود", "DOWN": "هبوط", "WAIT": "انتظار"}[signal.direction]
    if signal.strength == "متوسطة" or signal.direction == "WAIT":
        icon = "🟡"
    else:
        icon = "🟢" if signal.direction == "UP" else "🔴"
    return (
        f"📊 الزوج: {signal.pair}\n"
        f"📈 الفرصة: {direction_ar} {icon}\n"
        f"⏰ وقت الدخول: الآن {signal.observed_at:%H:%M}\n"
        f"⏳ مدة الصفقة: 1 دقيقة\n"
        f"🎯 توافق المؤشرات: {signal.confidence}%\n"
        f"قوة الاشارة: {signal.strength}"
    )


def format_explanation(signal: MarketSignal) -> str:
    digits = 3 if "JPY" in signal.pair else 5
    return (
        f"🔎 {signal.pair} — السعر {signal.price:.{digits}f}\n"
        f"EMA 9: {signal.ema9:.{digits}f} | EMA 21: {signal.ema21:.{digits}f}\n"
        f"RSI 14: {signal.rsi14:.1f} | الدعم: {signal.support:.{digits}f} "
        f"| المقاومة: {signal.resistance:.{digits}f}\n"
        f"الشمعة: {signal.candle_pattern}\n"
        f"السبب: {signal.rationale}"
    )


def format_confidence_notice() -> str:
    return (
        "تنبيه: النسبة المعروضة درجة توافق للمؤشرات وليست نسبة نجاح محسوبة "
        "من سجل صفقات تاريخي أو ضمانًا للربح. بيانات Yahoo Finance أسعار "
        "فوركس فورية استرشادية، وليست أسعار Quotex OTC."
    )


def signal_to_dict(signal: MarketSignal) -> dict[str, Any]:
    """Return only non-sensitive signal data for optional integrations."""
    return {
        "pair": signal.pair,
        "direction": signal.direction,
        "strength": signal.strength,
        "confidence": signal.confidence,
        "price": signal.price,
    }