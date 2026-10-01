"""Free public economic-calendar and official central-bank news sources."""

from __future__ import annotations

import email.utils
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MARKET_TIMEZONE = ZoneInfo("Europe/Istanbul")
FRED_TIMEZONE = ZoneInfo("America/Chicago")
FRED_CALENDAR_URL = "https://fred.stlouisfed.org/releases/calendar"
FRED_MAX_PAGES = 12
FRED_PAGE_SIZE = 50


@dataclass(frozen=True)
class CalendarEvent:
    key: str
    title: str
    currency: str
    scheduled_at: datetime
    source_url: str


@dataclass(frozen=True)
class OfficialStory:
    key: str
    source: str
    currency: str
    title: str
    summary: str
    link: str
    published_at: datetime


@dataclass(frozen=True)
class OfficialFeed:
    source: str
    currency: str
    url: str


OFFICIAL_FEEDS: tuple[OfficialFeed, ...] = (
    OfficialFeed(
        "Federal Reserve",
        "USD",
        "https://www.federalreserve.gov/feeds/press_all.xml",
    ),
    OfficialFeed("European Central Bank", "EUR", "https://www.ecb.europa.eu/rss/press.html"),
    OfficialFeed("Bank of England", "GBP", "https://www.bankofengland.co.uk/rss/news"),
    OfficialFeed(
        "Bank of Canada",
        "CAD",
        "https://www.bankofcanada.ca/feed/?content_type=media-advisories&post_type[0]=post&post_type[1]=page",
    ),
    OfficialFeed(
        "Reserve Bank of Australia",
        "AUD",
        "https://www.rba.gov.au/rss/rss-cb-media-releases.xml",
    ),
    OfficialFeed("Bank of Japan", "JPY", "https://www.boj.or.jp/en/rss/whatsnew.xml"),
)

PAIR_CURRENCIES: dict[str, tuple[str, str]] = {
    "EUR/USD": ("EUR", "USD"),
    "GBP/USD": ("GBP", "USD"),
    "USD/JPY": ("USD", "JPY"),
    "EUR/GBP": ("EUR", "GBP"),
    "AUD/CAD": ("AUD", "CAD"),
    "GBP/JPY": ("GBP", "JPY"),
}

_HIGH_IMPACT_TERMS = (
    "fomc",
    "consumer price",
    "cpi",
    "inflation",
    "employment situation",
    "nonfarm payroll",
    "payroll",
    "unemployment",
    "gross domestic product",
    "gdp",
    "personal income and outlays",
    "pce",
    "retail sales",
    "producer price",
    "industrial production",
    "key ecb interest rates",
    "harmonized indices of consumer prices",
    "harmonised indices of consumer prices",
    "national accounts - gdp",
    "monetary policy decision",
    "interest rate decision",
    "bank rate",
    "policy rate",
    "cash rate",
)

_NEWS_TERMS = (
    "fomc",
    "ecb",
    "policy decision",
    "interest rate decision",
    "interest rate",
    "rates unchanged",
    "rate unchanged",
    "policy rate",
    "monetary policy",
    "inflation",
    "consumer price",
    "employment",
    "payroll",
    "unemployment",
    "gross domestic product",
    "gdp",
    "rate decision",
    "cash rate",
    "bank rate",
    "financial stability",
    "intervention",
    "emergency",
    "trade policy",
    "tariff",
    "economic outlook",
)


def _infer_currency(title: str) -> str | None:
    normalized = title.casefold()
    if any(term in normalized for term in ("euro", "european", "eurostat", "hicp", "ecb")):
        return "EUR"
    if any(term in normalized for term in ("japan", "japanese", "boj", "bank of japan")):
        return "JPY"
    if any(
        term in normalized
        for term in ("united kingdom", "britain", "british", "bank of england", "sonia", "uk ")
    ):
        return "GBP"
    if any(term in normalized for term in ("canada", "canadian", "bank of canada", "statscan")):
        return "CAD"
    if any(term in normalized for term in ("australia", "australian", "rba")):
        return "AUD"
    if any(
        term in normalized
        for term in (
            "federal reserve",
            "fomc",
            "united states",
            "u.s.",
            "consumer price index",
            "employment situation",
            "unemployment insurance",
            "gross domestic product",
            "personal income and outlays",
            "nonfarm payroll",
            "retail sales",
            "producer price",
            "industrial production",
            "pce",
            "cpi",
        )
    ):
        return "USD"
    return None


def _is_high_impact_event(title: str) -> bool:
    normalized = title.casefold()
    if any(term in normalized for term in ("gdp by county", "gdp by industry", "gdp by state", "gdpnow")):
        return False
    return any(term in normalized for term in _HIGH_IMPACT_TERMS)


def _parse_calendar_date(value: str) -> date | None:
    normalized = " ".join(value.split()).replace(" Updated", "")
    for fmt in ("%A %B %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(normalized, fmt).date()
        except ValueError:
            pass
    return None


def _event_key(release_id: str, title: str, scheduled_at: datetime) -> str:
    timestamp = scheduled_at.astimezone(timezone.utc).isoformat()
    identity = release_id or title.casefold()
    digest = hashlib.sha256(f"{identity}|{timestamp}".encode("utf-8")).hexdigest()[:20]
    return f"fred:{digest}"


def calendar_events_from_html(
    page_html: str,
    now: datetime | None = None,
    source_url: str = FRED_CALENDAR_URL,
) -> list[CalendarEvent]:
    """Parse high-impact timed events from one public FRED calendar page."""
    now = now or datetime.now(MARKET_TIMEZONE)
    now = now.astimezone(MARKET_TIMEZONE)
    soup = BeautifulSoup(page_html, "html.parser")
    table = soup.select_one("table.table-standard-theme") or soup.find("table")
    if table is None:
        return []

    events: list[CalendarEvent] = []
    current_date: date | None = None
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        if len(cells) == 1 or cells[0].get("colspan"):
            current_date = _parse_calendar_date(cells[0].get_text(" ", strip=True))
            continue
        if current_date is None or len(cells) < 2:
            continue

        time_text = cells[0].get_text(" ", strip=True).strip()
        try:
            event_time = datetime.strptime(time_text, "%I:%M %p").time()
        except ValueError:
            continue

        release_link = cells[1].select_one('a[href*="/release?rid="]')
        if release_link is None:
            continue
        title = " ".join(release_link.get_text(" ", strip=True).split())
        currency = _infer_currency(title)
        if not title or currency is None or not _is_high_impact_event(title):
            continue

        local_source_time = datetime.combine(current_date, event_time, tzinfo=FRED_TIMEZONE)
        scheduled_at = local_source_time.astimezone(MARKET_TIMEZONE)
        if scheduled_at < now - timedelta(minutes=5) or scheduled_at > now + timedelta(days=15):
            continue

        href = release_link.get("href", "")
        release_id = parse_qs(urlparse(href).query).get("rid", [""])[0]
        events.append(
            CalendarEvent(
                key=_event_key(release_id, title, scheduled_at),
                title=title,
                currency=currency,
                scheduled_at=scheduled_at,
                source_url=urljoin(FRED_CALENDAR_URL, href) if href else source_url,
            )
        )

    unique = {event.key: event for event in events}
    return sorted(unique.values(), key=lambda event: event.scheduled_at)


def _fetch_fred_page(start: date, end: date, page_id: int) -> str:
    response = requests.get(
        FRED_CALENDAR_URL,
        params={
            "ob": "rd",
            "od": "asc",
            "vs": start.isoformat(),
            "ve": end.isoformat(),
            "pageID": page_id,
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.text


def _calendar_page_count(page_html: str) -> int:
    visible_text = " ".join(BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True).split())
    match = re.search(r"Releases\s+\d+\s*[-–]\s*\d+\s+of\s+(\d+)", visible_text, re.IGNORECASE)
    if not match:
        return 1
    total = int(match.group(1))
    return max(1, min(FRED_MAX_PAGES, (total + FRED_PAGE_SIZE - 1) // FRED_PAGE_SIZE))


def fetch_upcoming_events(now: datetime | None = None) -> list[CalendarEvent]:
    """Fetch a two-week view, paging through FRED's public release calendar."""
    now = now or datetime.now(MARKET_TIMEZONE)
    now = now.astimezone(MARKET_TIMEZONE)
    start = now.date()
    end = start + timedelta(days=14)
    first_page = _fetch_fred_page(start, end, 1)
    page_count = _calendar_page_count(first_page)
    pages: list[str] = [first_page]

    if page_count > 1:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(_fetch_fred_page, start, end, page_id): page_id
                for page_id in range(2, page_count + 1)
            }
            for future in as_completed(futures):
                try:
                    pages.append(future.result())
                except requests.RequestException as exc:
                    logger.warning(
                        "FRED calendar page %s unavailable (%s)",
                        futures[future],
                        type(exc).__name__,
                    )

    events: dict[str, CalendarEvent] = {}
    for page in pages:
        for event in calendar_events_from_html(page, now=now):
            events[event.key] = event
    return sorted(events.values(), key=lambda event: event.scheduled_at)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].casefold()


def _element_text(element: ET.Element, names: set[str]) -> str:
    for child in element.iter():
        if _local_name(child.tag) in names:
            text = " ".join("".join(child.itertext()).split())
            if text:
                return text
    return ""


def _element_link(element: ET.Element) -> str:
    for child in element.iter():
        if _local_name(child.tag) == "link":
            href = child.attrib.get("href")
            text = " ".join((child.text or "").split())
            if href:
                return href
            if text:
                return text
    return ""


def _parse_published_at(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(MARKET_TIMEZONE)


def _story_key(feed: OfficialFeed, identity: str, title: str, published_at: datetime) -> str:
    stable_identity = identity or f"{title}|{published_at.astimezone(timezone.utc).isoformat()}"
    digest = hashlib.sha256(f"{feed.currency}|{stable_identity}".encode("utf-8")).hexdigest()[:20]
    return f"official:{digest}"


def _is_relevant_story(title: str, summary: str) -> bool:
    text = f"{title} {summary}".casefold()
    return any(term in text for term in _NEWS_TERMS)


def parse_official_feed(
    feed: OfficialFeed,
    xml_content: bytes,
    now: datetime | None = None,
    lookback: timedelta = timedelta(minutes=12),
) -> list[OfficialStory]:
    now = (now or datetime.now(MARKET_TIMEZONE)).astimezone(MARKET_TIMEZONE)
    root = ET.fromstring(xml_content)
    entries = [element for element in root.iter() if _local_name(element.tag) in {"item", "entry"}]
    stories: list[OfficialStory] = []
    for entry in entries:
        title = _element_text(entry, {"title"})
        if not title:
            continue
        published_text = _element_text(entry, {"pubdate", "published", "updated", "date"})
        published_at = _parse_published_at(published_text)
        if published_at is None or published_at < now - lookback or published_at > now + timedelta(minutes=2):
            continue

        description = _element_text(entry, {"description", "summary", "encoded", "content"})
        summary = BeautifulSoup(description, "html.parser").get_text(" ", strip=True)[:700]
        if not _is_relevant_story(title, summary):
            continue

        identity = _element_text(entry, {"guid", "id"})
        link = _element_link(entry)
        stories.append(
            OfficialStory(
                key=_story_key(feed, identity or link, title, published_at),
                source=feed.source,
                currency=feed.currency,
                title=title[:300],
                summary=summary,
                link=link[:700],
                published_at=published_at,
            )
        )
    return stories


def _fetch_feed(feed: OfficialFeed, now: datetime) -> list[OfficialStory]:
    response = requests.get(feed.url, timeout=20)
    response.raise_for_status()
    return parse_official_feed(feed, response.content, now=now)


def fetch_official_news(now: datetime | None = None) -> list[OfficialStory]:
    """Read recent, market-relevant items from central-bank RSS/Atom feeds."""
    now = (now or datetime.now(MARKET_TIMEZONE)).astimezone(MARKET_TIMEZONE)
    stories: dict[str, OfficialStory] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_feed, feed, now): feed for feed in OFFICIAL_FEEDS}
        for future in as_completed(futures):
            feed = futures[future]
            try:
                for story in future.result():
                    stories[story.key] = story
            except (requests.RequestException, ET.ParseError, ValueError) as exc:
                logger.warning(
                    "%s news feed unavailable (%s)",
                    feed.source,
                    type(exc).__name__,
                )
    return sorted(stories.values(), key=lambda story: story.published_at, reverse=True)


def due_alert_window(event_at: datetime, now: datetime | None = None) -> str | None:
    """Return the alert window if the event is approximately 30 or 5 minutes away."""
    now = (now or datetime.now(MARKET_TIMEZONE)).astimezone(MARKET_TIMEZONE)
    minutes_until = (event_at.astimezone(MARKET_TIMEZONE) - now).total_seconds() / 60
    if 28 <= minutes_until <= 32:
        return "30m"
    if 3 <= minutes_until <= 7:
        return "5m"
    return None


def affected_pairs(currency: str) -> list[str]:
    return [
        pair
        for pair, (base, quote) in PAIR_CURRENCIES.items()
        if currency in {base, quote}
    ]


def pair_direction(pair: str, currency: str, currency_bias: str) -> str:
    """Map a currency-strength view to the listed pair's direction."""
    bias = currency_bias.upper()
    if bias not in {"STRONGER", "WEAKER"}:
        return "WAIT"
    currencies = PAIR_CURRENCIES.get(pair)
    if currencies is None or currency not in currencies:
        return "WAIT"
    currency_up = bias == "STRONGER"
    pair_up = currency_up if currencies[0] == currency else not currency_up
    return "UP" if pair_up else "DOWN"


def arabic_direction(direction: str) -> str:
    return {"UP": "صعود", "DOWN": "هبوط", "WAIT": "غير محسوم"}.get(direction, "غير محسوم")