from datetime import datetime
from zoneinfo import ZoneInfo

from telegram_bot.economic_news import (
    OfficialFeed,
    affected_pairs,
    calendar_events_from_html,
    due_alert_window,
    pair_direction,
    parse_official_feed,
)


def test_fred_calendar_parser_converts_source_timezone_and_filters_events():
    page = """
    <table class="table table-standard-theme">
      <tr><th>Sort by</th></tr>
      <tr><td colspan="2"><span>Friday October 02, 2026</span></td></tr>
      <tr><td>7:30 am</td><td><a href="/release?rid=50">Employment Situation</a></td></tr>
      <tr><td>7:30 am</td><td><a href="/release?rid=10">Consumer Price Index</a></td></tr>
      <tr><td>9:00 am</td><td><a href="/release?rid=18">Low-impact rate series</a></td></tr>
      <tr><td>N/A</td><td><a href="/release?rid=101">FOMC Press Release</a></td></tr>
    </table>
    """
    now = datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    events = calendar_events_from_html(page, now=now)

    assert [event.title for event in events] == ["Employment Situation", "Consumer Price Index"]
    assert {event.currency for event in events} == {"USD"}
    assert events[0].scheduled_at == datetime(
        2026, 10, 2, 15, 30, tzinfo=ZoneInfo("Europe/Istanbul")
    )
    assert "rid=50" in events[0].source_url


def test_currency_and_pair_mapping():
    assert affected_pairs("USD") == ["EUR/USD", "GBP/USD", "USD/JPY"]
    assert pair_direction("EUR/USD", "USD", "STRONGER") == "DOWN"
    assert pair_direction("USD/JPY", "USD", "STRONGER") == "UP"
    assert pair_direction("AUD/CAD", "CAD", "WEAKER") == "UP"
    assert pair_direction("GBP/JPY", "JPY", "UNCERTAIN") == "WAIT"


def test_pre_event_alert_windows():
    now = datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    assert due_alert_window(datetime(2026, 10, 1, 12, 30, tzinfo=now.tzinfo), now) == "30m"
    assert due_alert_window(datetime(2026, 10, 1, 12, 5, tzinfo=now.tzinfo), now) == "5m"
    assert due_alert_window(datetime(2026, 10, 1, 12, 15, tzinfo=now.tzinfo), now) is None


def test_official_feed_parser_uses_recent_item_and_summary():
    now = datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    feed = OfficialFeed("Example Central Bank", "USD", "https://example.org/feed.xml")
    xml = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><item>
      <title>Central bank announces interest rate decision</title>
      <link>https://example.org/decision</link>
      <guid>release-1</guid>
      <pubDate>Thu, 01 Oct 2026 11:55:00 +0300</pubDate>
      <description><![CDATA[The policy rate was held at 4 percent.]]></description>
    </item></channel></rss>"""

    stories = parse_official_feed(feed, xml, now=now)
    assert len(stories) == 1
    assert stories[0].currency == "USD"
    assert "held at 4 percent" in stories[0].summary
    assert stories[0].link == "https://example.org/decision"