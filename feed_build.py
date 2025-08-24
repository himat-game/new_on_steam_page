import os
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape
from snaplite import iter_new_to_old

MAX_ITEMS = 300

CHANNELS = {
    "new": {
        "title": "Steam: Newly Published Store Pages",
        "link":  "https://store.steampowered.com/",
        "desc":  "Newly published Steam store pages detected by crawler.",
        "lang":  "en-us",
        "ttl":   30,
        "outfile": "feed_new.xml",
    },
    "updates": {
        "title": "Steam: Updates (Japanese added / Price changed)",
        "link":  "https://store.steampowered.com/",
        "desc":  "Steam store updates: language additions and price changes.",
        "lang":  "en-us",
        "ttl":   30,
        "outfile": "feed_updates.xml",
    },
}

def _fmt_rfc2822(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)

def _fmt_pubdate_from_ts(ts):
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)
    return _fmt_rfc2822(dt)

def _build_item_xml(obj, kind):
    appid = obj.get("appid")
    title = obj.get("title") or f"AppID {appid}"
    link  = obj.get("url") or (f"https://store.steampowered.com/app/{appid}" if appid else "")
    guid  = f"{kind}:appid:{appid}:{obj.get('event','new')}"
    pubdate = _fmt_pubdate_from_ts(obj.get("ts"))

    # 説明文（updatesは内容を少し詳しく）
    if kind == "updates":
        ev = obj.get("event")
        if ev == "lang_added_ja":
            desc = "Japanese language was added."
        elif ev == "price_change":
            oldp = obj.get("old")
            newp = obj.get("new")
            cur  = obj.get("currency") or ""
            desc = f"Price changed: {oldp} -> {newp} {cur}"
        else:
            desc = obj.get("description") or ""
    else:
        desc = obj.get("description") or ""

    description_xml = f"<description>{escape(desc)}</description>" if desc else ""

    parts = [
        "    <item>",
        f"      <title>{escape(title)}</title>",
        f"      <link>{escape(link)}</link>" if link else "",
        f"      <guid isPermaLink=\"false\">{escape(guid)}</guid>",
        f"      <pubDate>{pubdate}</pubDate>",
        f"      {description_xml}" if description_xml else "",
        "    </item>",
    ]
    return "\n".join(x for x in parts if x)

def write_feed(kind):
    assert kind in CHANNELS, f"unknown kind: {kind}"
    ch = CHANNELS[kind]

    # 最新MAX_ITEMS件だけ（appid重複は後勝ち）
    out, seen = [], set()
    for obj in iter_new_to_old(kind):
        appid = obj.get("appid")
        key = (appid, obj.get("event") if kind == "updates" else None)
        if key in seen:
            continue
        seen.add(key)
        out.append(obj)
        if len(out) >= MAX_ITEMS:
            break
    out.reverse()  # 古→新

    now = _fmt_rfc2822(datetime.utcnow().replace(tzinfo=timezone.utc))
    head = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{escape(ch["title"])}</title>
    <link>{escape(ch["link"])}</link>
    <description>{escape(ch["desc"])}</description>
    <language>{ch["lang"]}</language>
    <ttl>{ch["ttl"]}</ttl>
    <lastBuildDate>{now}</lastBuildDate>
"""
    body = "\n".join(_build_item_xml(obj, kind) for obj in out)
    tail = """
  </channel>
</rss>
"""
    xml = head + body + tail
    with open(ch["outfile"], "w", encoding="utf-8", newline="\n") as f:
        f.write(xml)
