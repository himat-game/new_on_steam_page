#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import time
import hashlib
import requests
from datetime import datetime, timezone
from email.utils import formatdate
from html import escape

# ===== 設定（軽量） =====
BATCH_SIZE = 300                 # 1ランで見る appid 数
LOOKBACK_SEC_FOR_NEW = 48*3600   # 初見時に「新規扱い」にする許容窓（48時間）
TIMEOUT = 12                     # HTTPタイムアウト秒
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SteamWatchLite/1.0)",
    "Accept-Language": "en-US,en;q=0.9"
}
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails?cc=us&l=en&appids={appid}"
APPLIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"

# ===== 便利関数 =====
def now_ts() -> int:
    return int(time.time())

def load_state(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                st = json.load(f)
            except Exception:
                st = {}
    else:
        st = {}
    # 既定値
    st.setdefault("cursor", 0)
    st.setdefault("seen", {})          # {appid: true}
    st.setdefault("snapshots", {})     # {appid: sha1}
    st.setdefault("events", [])        # [{ts, appid, kind, title, link, summary}]
    return st

def save_state(path: str, st: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

def get_cursor_max() -> int:
    # 最新の appid 上限（ざっくり）を取得
    try:
        r = requests.get(APPLIST_URL, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        apps = data.get("applist", {}).get("apps", [])
        if not apps:
            return max(300000, 0)  # フォールバック
        return max(a.get("appid", 0) for a in apps)
    except Exception:
        # ネットワーク不調時のフォールバック（安全に大きめ）
        return 300000

def fetch_appdetails(appid: int) -> dict | None:
    try:
        r = requests.get(APPDETAILS_URL.format(appid=appid), headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        jd = r.json()
        node = jd.get(str(appid))
        if not node or not node.get("success"):
            return None
        return node.get("data") or None
    except Exception:
        return None

def snapshot_fields(data: dict) -> dict:
    """差分検知に使う軽量フィールドのみ抽出（膨張しすぎないように）"""
    return {
        "type": data.get("type"),
        "name": data.get("name"),
        "is_free": data.get("is_free"),
        "release_date": {
            "coming_soon": data.get("release_date", {}).get("coming_soon"),
            "date": data.get("release_date", {}).get("date"),
        },
        "price_final": (data.get("price_overview", {}) or {}).get("final"),
        "supported_languages": data.get("supported_languages"),
        "metacritic": (data.get("metacritic", {}) or {}).get("score"),
        "developers": data.get("developers"),
        "publishers": data.get("publishers"),
        "genres": [g.get("description") for g in (data.get("genres") or []) if isinstance(g, dict)],
        "header_image": data.get("header_image"),
        "website": data.get("website"),
    }

def digest(obj: dict) -> str:
    s = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def make_event(appid: int, kind: str, title: str, summary: str) -> dict:
    return {
        "ts": now_ts(),
        "appid": appid,
        "kind": kind,  # "new" | "update"
        "title": title,
        "link": f"https://store.steampowered.com/app/{appid}/",
        "summary": summary,
    }

def limit_events(events: list, cap: int = 1200) -> list:
    if len(events) <= cap:
        return events
    return events[-cap:]  # 末尾を残す

def to_rss(items: list[dict], title: str, link: str, description: str) -> str:
    # items: [{title, link, ts, summary}]
    pub_now = formatdate(timeval=now_ts(), usegmt=True)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{escape(title)}</title>",
        f"<link>{escape(link)}</link>",
        f"<description>{escape(description)}</description>",
        f"<lastBuildDate>{pub_now}</lastBuildDate>",
    ]
    for it in items:
        parts += [
            "<item>",
            f"<title>{escape(it['title'])}</title>",
            f"<link>{escape(it['link'])}</link>",
            f"<pubDate>{formatdate(it['ts'], usegmt=True)}</pubDate>",
            f"<description>{escape(it.get('summary',''))}</description>",
            "</item>",
        ]
    parts += ["</channel>", "</rss>"]
    return "\n".join(parts)

# ===== メイン =====
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-path", default="state.json")
    args = parser.parse_args()

    state = load_state(args.state_path)
    cursor = int(state.get("cursor", 0))
    cursor_max = get_cursor_max()

    start = cursor
    end = min(cursor + BATCH_SIZE, cursor_max)
    appids = list(range(start, end))
    if start >= cursor_max:
        # 末尾に達していたら先頭へ
        appids = list(range(0, min(BATCH_SIZE, cursor_max)))
        start = 0
        end = len(appids)

    published_now = 0
    updates_now = 0
    checked = 0

    for appid in appids:
        checked += 1
        data = fetch_appdetails(appid)
        if not data:
            continue
        # 有効なストアページのみ
        name = data.get("name")
        if not name:
            continue

        snap = snapshot_fields(data)
        dig = digest(snap)
        prev = state["snapshots"].get(str(appid))

        if prev is None:
            # 初見
            state["snapshots"][str(appid)] = dig
            # 「新規扱い」にするか（直近48h以内に見つけたもの）
            if (now_ts() - LOOKBACK_SEC_FOR_NEW) <= now_ts():
                summary = (data.get("short_description") or "")[:400]
                ev = make_event(appid, "new", f"[NEW] {name}", summary)
                state["events"].append(ev)
                state["seen"][str(appid)] = True
                published_now += 1
        else:
            if prev != dig:
                state["snapshots"][str(appid)] = dig
                summary = "Store page updated."
                ev = make_event(appid, "update", f"[UPDATE] {name}", summary)
                state["events"].append(ev)
                updates_now += 1

    # イベントを制限
    state["events"] = limit_events(state["events"], cap=1200)

    # RSS 出力（直近の新規/更新をそれぞれ最大300件）
    new_items = [e for e in reversed(state["events"]) if e["kind"] == "new"][:300]
    upd_items = [e for e in reversed(state["events"]) if e["kind"] == "update"][:300]

    feed_new = to_rss(new_items, "New on Steam (detected)", "https://store.steampowered.com/", "Newly detected store pages")
    feed_updates = to_rss(upd_items, "Steam store updates (detected)", "https://store.steampowered.com/", "Detected store page updates")

    with open("feed_new.xml", "w", encoding="utf-8") as f:
        f.write(feed_new)
    with open("feed_updates.xml", "w", encoding="utf-8") as f:
        f.write(feed_updates)

    # カーソル更新
    state["cursor"] = end if end < cursor_max else cursor_max

    # 保存
    save_state(args.state_path, state)

    # 統計出力（ログの最後に見やすく）
    snapshots_cnt = len(state.get("snapshots", {}))
    print(f"new_ids_checked={checked} published_now={published_now} items={BATCH_SIZE} updates_now={updates_now} snapshots={snapshots_cnt} cursor={state['cursor']}/{cursor_max}")

if __name__ == "__main__":
    main()
