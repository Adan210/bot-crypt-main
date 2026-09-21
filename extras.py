"""Extra market context (derivatives, sentiment, news, economic calendar) used ONLY to veto entries.

None of these can create a trade or change its size. Every source fails soft: on any error the value
is None and the bot simply does not use it. They are recorded with each trade so they can be studied
later; they are NOT backtested (no clean history), so treat them as unvalidated safety filters.
"""
import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

MAX_FUNDING = 0.0005      # 0.05% per 8h: crowded longs, skip new entries
MAX_GREED = 90            # Fear & Greed index at or above this: euphoria, skip new entries
BAD_NEWS = ('hack', 'exploit', 'delist', 'insolven', 'bankrupt', 'sec sues', 'ban ', 'outage', 'halt')
NEWS_URL = 'https://www.coindesk.com/arc/outboundfeeds/rss/'
FEAR_GREED_URL = 'https://api.alternative.me/fng/?limit=1'
FUNDING_URL = 'https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}'


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={'User-Agent': 'crypto-lab/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode('utf-8')


def parse_fear_greed(text):
    return int(json.loads(text)['data'][0]['value'])


def parse_funding(text):
    return float(json.loads(text)['lastFundingRate'])


def parse_rss_titles(text):
    return [(item.findtext('title') or '') for item in ET.fromstring(text).iter('item')]


def news_risk(titles):
    """First headline containing an alarming keyword, or None. Crude on purpose: it can only block."""
    for title in titles:
        low = title.lower()
        if any(word in low for word in BAD_NEWS):
            return title
    return None


def load_events(path):
    """Economic calendar file kept by the user: [{"time": "2026-09-24T18:00:00Z", "name": "FOMC", "impact": "high"}]."""
    try:
        with open(path, encoding='utf-8') as f:
            events = json.load(f)
        return [dict(e, ms=int(datetime.fromisoformat(e['time'].replace('Z', '+00:00')).timestamp() * 1000))
                for e in events if e.get('impact') == 'high']
    except (OSError, ValueError, KeyError, TypeError):
        return []


def blackout(events, now_ms, before_h=2, after_h=1):
    """Name of a high-impact event happening within [-before_h, +after_h] of now, else None."""
    for e in events:
        if e['ms'] - before_h * 3600000 <= now_ms <= e['ms'] + after_h * 3600000:
            return e['name']
    return None


def gather(symbols, now_ms, events=(), get=http_get):
    """Best-effort snapshot. Any failing source is simply missing (None)."""
    ctx = dict(time=now_ms, fear_greed=None, funding={}, news=None, calendar=blackout(list(events), now_ms))
    for key, fn in (('fear_greed', lambda: parse_fear_greed(get(FEAR_GREED_URL))),
                    ('news', lambda: news_risk(parse_rss_titles(get(NEWS_URL))))):
        try:
            ctx[key] = fn()
        except Exception:
            pass
    for symbol in symbols:
        try:
            ctx['funding'][symbol] = parse_funding(get(FUNDING_URL.format(symbol=symbol)))
        except Exception:
            pass
    return ctx


def veto(symbol, ctx):
    """Reason to skip a new entry, or None. Missing data never vetoes."""
    if not ctx:
        return None
    if ctx.get('calendar'):
        return f"Evento económico de alto impacto: {ctx['calendar']}"
    if ctx.get('news'):
        return f"Noticia de riesgo: {ctx['news']}"
    if (ctx.get('fear_greed') or 0) >= MAX_GREED:
        return f"Euforia extrema (Fear & Greed {ctx['fear_greed']})"
    if (ctx.get('funding') or {}).get(symbol, 0) > MAX_FUNDING:
        return f"Funding muy alto en {symbol}: largos saturados"
    return None
