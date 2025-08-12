#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam 新規ストア公開 RSS（画像＋説明つき / JA→EN フォールバック）

- 新規 AppID 検出（ISteamApps/GetAppList）
- appdetails が success:true になったタイミングで「公開」判定
- RSSに <enclosure> / media:thumbnail / content:encoded を出力（画像表示）
- <description> と本文HTMLに short_description（日本語優先、無ければ英語）を入れる
- 未公開は pending キューに入れて後で再チェック
"""
import argparse
import datetime as dt
import html
import io
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

STEAM_APP_LIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

# -------- HTTP helpers --------
def http_get_json(url: str, params: Optional[Dict[str, str]] = None, timeout: int = 20):
    if params:
        url = url + ("?" + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={
        "User-Agent": "new-on-steam-rss/1.3 (+https://example.com)"
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return json.loads(data)

# -------- RSS helpers --------
def guess_mime(url: str) -> str:
    if not url:
        return "image/jpeg"
    u = url.lower()
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"

def rfc822(dt_utc: dt.datetime) -> str:
    return dt_utc.strftime("%a, %d %b %Y %H:%M:%S +0000")

def truncate(text: str, limit: int = 600) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"

def build_rss(channel_title: str, channel_link: str, channel_desc: str, items: List[Dict]) -> str:
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<rss version="2.0" '
              'xmlns:media="http://search.yahoo.com/mrss/" '
              'xmlns:content="http://purl.org/rss/1.0/modules/content/">\n')
    out.write('<channel>\n')
    out.write(f'<title>{html.escape(channel_title)}</title>\n')
    out.write(f'<link>{html.escape(channel_link)}</link>\n')
    out.write(f'<description>{html.escape(channel_desc)}</description>\n')
    out.write(f'<lastBuildDate>{rfc822(dt.datetime.utcnow())}</lastBuildDate>\n')

    for it in items:
        title = it.get("title", "(no title)")
        link = it.get("link", "")
        guid = it.get("guid", str(random.random()))
        pub = it.get("pubDate")
        pub_dt = dt.datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        desc_plain = truncate(it.get("description", ""))
        image = it.get("image")

        out.write('<item>\n')
        out.write(f'  <title>{html.escape(title)}</title>\n')
        out.write(f'  <link>{html.escape(link)}</link>\n')
        out.write(f'  <guid isPermaLink="false">{html.escape(guid)}</guid>\n')
        out.write(f'  <pubDate>{rfc822(pub_dt)}</pubDate>\n')
        if desc_plain:
            out.write(f'  <description>{html.escape(desc_plain)}</description>\n')

        if image:
            mime = guess_mime(image)
            out.write(f'  <enclosure url="{html.escape(image)}" type="{mime}" />\n')
            out.write(f'  <media:content url="{html.escape(image)}" type="{mime}" />\n')
            out.write(f'  <media:thumbnail url="{html.escape(image)}" />\n')

        html_parts = []
        if image:
            html_parts.append(f'<p><a href="{html.escape(link)}"><img src="{html.escape(image)}" alt="{html.escape(title)}" /></a></p>')
        if desc_plain:
            html_parts.append(f'<p>{html.escape(desc_plain)}</p>')
        html_parts.append(f'<p><a href="{html.escape(link)}">Steamでページを開く</a></p>')
        out.write('  <content:encoded><![CDATA[' + "".join(html_parts) + ']]></content:encoded>\n')

        out.write('</item>\n')

    out.write('</channel>\n')
    out.write('</rss>\n')
    return out.getvalue()

# -------- Core logic --------
def fetch_app_list() -> List[Dict]:
    js = http_get_json(STEAM_APP_LIST_URL)
    return js.get("applist", {}).get("apps", [])

def fetch_appdetails_once(appid: int, cc: str, lang: str) -> Tuple[bool, Optional[Dict]]:
    js = http_get_json(APPDETAILS_URL, params={"appids": str(appid), "cc": cc, "l": lang})
    node = js.get(str(appid))
    if not node:
        return False, None
    if not node.get("success"):
        return False, None
    data = node.get("data")
    if not data:
        return False, None
    return True, data

def fetch_appdetails(appid: int, cc_primary: str, lang_primary: str) -> Tuple[bool, Optional[Dict]]:
    ok, data = fetch_appdetails_once(appid, cc_primary, lang_primary)
    if ok:
        return True, data
    for cc, lang in [("jp", "ja"), ("us", "en"), ("de", "de"), ("gb", "en")]:
        if cc == cc_primary and lang == lang_primary:
            continue
        try:
            ok, data = fetch_appdetails_once(appid, cc, lang)
            if ok:
                return True, data
        except Exception:
            pass
    return False, None

def get_short_description(appid: int, data_primary: Dict) -> str:
    desc = data_primary.get("short_description")
    if desc:
        return desc
    ok, en_data = fetch_appdetails_once(appid, "us", "en")
    if ok and en_data and en_data.get("short_description"):
        return en_data["short_description"]
    return f"type={data_primary.get('type')}, appid={appid}"

# -------- State I/O --------
def load_state(path: str) -> Dict:
    if not os.path.exists(path):
        return {"seen": {}, "pending": [], "items": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(path: str, state: Dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# -------- Main process --------
def build_item(appid: int, data: Dict, now_iso: str) -> Dict:
    title = data.get("name", f"App {appid}")
    link = f"https://store.steampowered.com/app/{appid}/"
    image_url = (
        data.get("header_image")
        or data.get("capsule_imagev5")
        or data.get("capsule_image")
        or (data.get("screenshots") or [{}])[0].get("path_full")
        or data.get("background")
    )
    desc = get_short_description(appid, data)
    guid = f"steam-store-published-{appid}-{int(time.time())}"
    return {
        "title": f"{title}",
        "link": link,
        "guid": guid,
        "pubDate": now_iso,
        "description": desc,
        "image": image_url,
    }

def main():
    ap = argparse.ArgumentParser(description="Detect newly published Steam store pages and emit RSS (images + JA/EN description).")
    ap.add_argument("--state", default="state.json", help="State JSON path")
    ap.add_argument("--rss-out", default="steam_new_store.xml", help="RSS output file path")
    ap.add_argument("--channel-title", default="New on Steam（画像＋説明つき / JA→EN）", help="RSS channel title")
    ap.add_argument("--channel-link", default="https://store.steampowered.com/", help="RSS channel link")
    ap.add_argument("--channel-desc", default="Games whose Steam store page just went public (detected)", help="RSS channel description")
    ap.add_argument("--cc", default="jp", help="Primary country code for appdetails")
    ap.add_argument("--lang", default="ja", help="Primary language for appdetails")
    ap.add_argument("--max-items", type=int, default=300, help="Max RSS items to keep")
    ap.add_argument("--max-new", type=int, default=500, help="Per run: max new appids to check for appdetails")
    ap.add_argument("--pending-retry", type=int, default=200, help="Per run: max pending appids to recheck")
    ap.add_argument("--baseline-if-empty", action="store_true", help="If state not found, baseline existing app list instead of checking all")
    args = ap.parse_args()

    state = load_state(args.state)
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    try:
        apps = fetch_app_list()
    except Exception as e:
        print(f"[ERROR] fetch_app_list failed: {e}", file=sys.stderr)
        sys.exit(1)

    current_ids = {int(a["appid"]) for a in apps if "appid" in a}
    seen_ids = set(int(x) for x in state["seen"].keys())

    if not state["seen"] and args.baseline_if_empty:
        for appid in current_ids:
            state["seen"][str(appid)] = {"published": False, "detected_at": None}
        save_state(args.state, state)
        with open(args.rss_out, "w", encoding="utf-8") as f:
            f.write(build_rss(args.channel_title, args.channel_link, args.channel_desc, []))
        print("Initialized baseline (no notifications). Next runs will track new appids.")
        return

    new_ids = list(current_ids - seen_ids)
    if new_ids:
        random.shuffle(new_ids)
        new_ids = new_ids[: args.max_new]

    published_events = []

    # 2-a) 新規 AppID
    for appid in new_ids:
        ok, data = False, None
        try:
            ok, data = fetch_appdetails(appid, args.cc, args.lang)
            time.sleep(0.2)
        except urllib.error.HTTPError as e:
            print(f"[WARN] appdetails HTTPError {appid}: {e}")
        except Exception as e:
            print(f"[WARN] appdetails error {appid}: {e}")

        if ok:
            item = build_item(appid, data, now_iso)
            published_events.append(item)
            state["seen"][str(appid)] = {"published": True, "detected_at": now_iso}
        else:
            state["seen"][str(appid)] = {"published": False, "detected_at": None}
            state["pending"].append(appid)

    # 2-b) pending 再チェック
    if state["pending"]:
        random.shuffle(state["pending"])
        to_check = state["pending"][: args.pending_retry]
        remain_pending = []
        for appid in to_check:
            ok, data = False, None
            try:
                ok, data = fetch_appdetails(appid, args.cc, args.lang)
                time.sleep(0.25)
            except Exception as e:
                print(f"[WARN] pending appdetails error {appid}: {e}")

            if ok:
                item = build_item(appid, data, now_iso)
                published_events.append(item)
                state["seen"][str(appid)] = {"published": True, "detected_at": now_iso}
            else:
                remain_pending.append(appid)
        remain_pending.extend(state["pending"][args.pending_retry:])
        state["pending"] = remain_pending

    if published_events:
        new_items = published_events + state["items"]
        state["items"] = new_items[: args.max_items]

    rss_xml = build_rss(
        channel_title=args.channel_title,
        channel_link=args.channel_link,
        channel_desc=args.channel_desc,
        items=state["items"],
    )
    with open(args.rss_out, "w", encoding="utf-8") as f:
        f.write(rss_xml)

    save_state(args.state, state)

    print(f"new_ids_checked={len(new_ids)} published_now={len(published_events)} pending={len(state['pending'])} items={len(state['items'])}")

if __name__ == "__main__":
    main()
