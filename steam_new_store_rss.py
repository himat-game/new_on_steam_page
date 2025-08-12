#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam 新規ストア公開RSS + ストア更新イベントRSS（画像・説明・価格・言語対応 / ローリング全件クロール）

- 新規公開RSS: steam_new_store.xml
  * 画像 (<enclosure> / media:thumbnail) / 本文HTML (content:encoded) / short_description（日本語→無ければ英語）
- 更新イベントRSS: steam_store_updates.xml
  * ストアメタの差分を検知して通知（価格・対応言語を含む）
- ローリング全件クロール: 毎回 --crawl-batch 件ずつ appdetails を巡回し、数日で全アプリを一巡
- レート配慮: 1件ごとに 0.2〜0.25秒スリープ、429検知の簡易バックオフ

初回:
  python steam_new_store_rss.py --state state.json --rss-out steam_new_store.xml --baseline-if-empty
通常:
  python steam_new_store_rss.py --state state.json --rss-out steam_new_store.xml --pending-retry 100 --max-new 200 --crawl-batch 600
"""
import argparse
import datetime as dt
import html
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

STEAM_APP_LIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

# ---- HTTP helpers ------------------------------------------------------------

def http_get_raw(url: str, params: Optional[Dict[str, str]] = None, timeout: int = 20) -> bytes:
    if params:
        url = url + ("?" + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={
        "User-Agent": "steam-new-store-rss/2.0 (+https://example.com)"
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # 簡易バックオフ（429など）
        if e.code == 429 or e.code == 503:
            time.sleep(5)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        raise

def http_get_json(url: str, params: Optional[Dict[str, str]] = None, timeout: int = 20):
    data = http_get_raw(url, params=params, timeout=timeout)
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return json.loads(data)

# ---- RSS helpers -------------------------------------------------------------

def guess_mime(url: str) -> str:
    if not url:
        return "image/jpeg"
    u = url.lower()
    if u.endswith(".png"): return "image/png"
    if u.endswith(".webp"): return "image/webp"
    if u.endswith(".jpg") or u.endswith(".jpeg"): return "image/jpeg"
    return "image/jpeg"

def rfc822(dt_utc: dt.datetime) -> str:
    return dt_utc.strftime("%a, %d %b %Y %H:%M:%S +0000")

def truncate(text: str, limit: int = 600) -> str:
    if not text: return ""
    return text if len(text) <= limit else (text[: limit - 1] + "…")

def build_rss(channel_title: str, channel_link: str, channel_desc: str, items: List[Dict], lang: str = "ja-jp") -> str:
    # lastBuildDate は最新アイテムの日時（無ければ現在）
    if items:
        last = items[0]["pubDate"]
        last_dt = dt.datetime.fromisoformat(last.replace("Z","+00:00")).astimezone(dt.timezone.utc)
    else:
        last_dt = dt.datetime.utcnow()

    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" '
              'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
              'xmlns:atom="http://www.w3.org/2005/Atom">\n')
    out.write('<channel>\n')
    out.write(f'<title>{html.escape(channel_title)}</title>\n')
    out.write(f'<link>{html.escape(channel_link)}</link>\n')
    out.write(f'<description>{html.escape(channel_desc)}</description>\n')
    out.write(f'<language>{html.escape(lang)}</language>\n')
    out.write(f'<lastBuildDate>{rfc822(last_dt)}</lastBuildDate>\n')

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
        html_block = "".join(html_parts)
        out.write('  <content:encoded><![CDATA[' + html_block + ']]></content:encoded>\n')

        out.write('</item>\n')

    out.write('</channel>\n')
    out.write('</rss>\n')
    return out.getvalue()

# ---- Storefront helpers ------------------------------------------------------

def fetch_app_list() -> List[Dict]:
    js = http_get_json(STEAM_APP_LIST_URL)
    return js.get("applist", {}).get("apps", [])

def fetch_appdetails_once(appid: int, cc: str, lang: str) -> Tuple[bool, Optional[Dict]]:
    js = http_get_json(APPDETAILS_URL, params={"appids": str(appid), "cc": cc, "l": lang})
    node = js.get(str(appid))
    if not node or not node.get("success"): return False, None
    data = node.get("data")
    if not data: return False, None
    return True, data

def fetch_appdetails(appid: int, cc_primary: str, lang_primary: str) -> Tuple[bool, Optional[Dict]]:
    ok, data = fetch_appdetails_once(appid, cc_primary, lang_primary)
    if ok: return True, data
    for cc, lang in [("jp","ja"), ("us","en"), ("de","de"), ("gb","en")]:
        if cc == cc_primary and lang == lang_primary: continue
        try:
            ok, data = fetch_appdetails_once(appid, cc, lang)
            if ok: return True, data
        except Exception:
            pass
    return False, None

# ---- Diff helpers（価格・言語など） -----------------------------------------

LANG_TAG_RE = re.compile(r"<.*?>")
SEP_RE = re.compile(r"[;,/｜|]")

def normalize_languages(s: Optional[str]) -> List[str]:
    """supported_languages は HTML入りの文字列。タグ除去→区切りで分割→整形→小文字→重複排除→ソート"""
    if not s: return []
    txt = LANG_TAG_RE.sub("", s)
    parts = [p.strip().lower() for p in SEP_RE.split(txt) if p.strip()]
    # よくある冗長ワードを削る
    cleaned = []
    for p in parts:
        p = p.replace("full audio", "").replace("interface", "").replace("subtitles","").strip()
        if not p: continue
        cleaned.append(p)
    uniq = sorted(set(cleaned))
    return uniq

def extract_snapshot(data: dict) -> dict:
    """差分比較用のスナップショットを抽出（価格・言語・主要メタ）"""
    price = (data.get("price_overview") or {}).get("final_formatted")
    langs = normalize_languages(data.get("supported_languages"))
    genres = [g.get("description","") for g in (data.get("genres") or [])]
    snap = {
        "name": data.get("name"),
        "short_description": data.get("short_description"),
        "type": data.get("type"),
        "header_image": data.get("header_image"),
        "capsule_imagev5": data.get("capsule_imagev5"),
        "is_free": data.get("is_free"),
        "price": price or ("Free" if data.get("is_free") else ""),
        "supported_languages": langs,
        "genres": sorted(set([g for g in genres if g])),
        "platforms": json.dumps(data.get("platforms", {}), sort_keys=True),
        "release": json.dumps(data.get("release_date", {}), sort_keys=True),
    }
    return snap

def diff_snap(old: dict, new: dict) -> List[Tuple[str,str,str]]:
    changes = []
    keys = set(old.keys()) | set(new.keys())
    for k in sorted(keys):
        ov, nv = old.get(k), new.get(k)
        if ov != nv:
            if isinstance(ov, list): ov = ", ".join(ov)
            if isinstance(nv, list): nv = ", ".join(nv)
            changes.append((k, str(ov) if ov is not None else "", str(nv) if nv is not None else ""))
    return changes

# ---- Item builders -----------------------------------------------------------

def get_short_description(appid: int, primary_data: dict) -> str:
    desc = primary_data.get("short_description")
    if desc: return desc
    ok, en = fetch_appdetails_once(appid, "us", "en")
    if ok and en and en.get("short_description"):
        return en["short_description"]
    return f"type={primary_data.get('type')}, appid={appid}"

def choose_image(data: dict) -> Optional[str]:
    return (data.get("header_image")
            or data.get("capsule_imagev5")
            or data.get("capsule_image")
            or (data.get("screenshots") or [{}])[0].get("path_full")
            or data.get("background"))

def build_new_item(appid: int, data: dict, now_iso: str) -> Dict:
    title = data.get("name", f"App {appid}")
    link = f"https://store.steampowered.com/app/{appid}/"
    image = choose_image(data)
    desc = get_short_description(appid, data)
    guid = f"steam-store-published-{appid}"  # 安定GUID（重複追加防止）
    return {
        "title": title, "link": link, "guid": guid, "pubDate": now_iso,
        "description": desc, "image": image,
    }

def pretty_change_label(k: str) -> str:
    return {
        "name": "タイトル",
        "short_description": "説明",
        "type": "タイプ",
        "header_image": "ヘッダー画像",
        "capsule_imagev5": "カプセル画像",
        "is_free": "無料フラグ",
        "price": "価格",
        "supported_languages": "対応言語",
        "genres": "ジャンル",
        "platforms": "対応OS",
        "release": "リリース情報",
    }.get(k, k)

def build_update_item(appid: int, data: dict, changes: List[Tuple[str,str,str]], now_iso: str) -> Dict:
    title = f"Updated: {data.get('name', f'App {appid}')}"
    link = f"https://store.steampowered.com/app/{appid}/"
    image = choose_image(data)
    # 価格＆言語は読みやすく
    parts = []
    for k, ov, nv in changes:
        label = pretty_change_label(k)
        if k == "supported_languages":
            ov = ov or "-"
            nv = nv or "-"
        parts.append(f"{label}: {ov} → {nv}")
    desc = "; ".join(parts)
    guid = f"steam-store-update-{appid}-{int(time.time())}"
    return {
        "title": title, "link": link, "guid": guid, "pubDate": now_iso,
        "description": desc, "image": image,
    }

# ---- State I/O ---------------------------------------------------------------

def load_state(path: str) -> Dict:
    if not os.path.exists(path):
        return {
            "seen": {}, "pending": [], "items": [],
            "updates": [], "snapshots": {},
            "applist": [], "crawl_cursor": 0
        }
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(path: str, state: Dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# ---- Main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Steam new-store & store-updates RSS generator (images, price, languages, rolling crawl).")
    ap.add_argument("--state", default="state.json", help="State JSON path")
    ap.add_argument("--rss-out", default="steam_new_store.xml", help="New store RSS output")
    ap.add_argument("--updates-out", default="steam_store_updates.xml", help="Store updates RSS output")
    ap.add_argument("--channel-title", default="Steam: Newly Published Store Pages", help="New store RSS channel title")
    ap.add_argument("--channel-link", default="https://store.steampowered.com/", help="RSS channel link")
    ap.add_argument("--channel-desc", default="Games whose Steam store page just went public (detected)", help="RSS channel desc")
    ap.add_argument("--updates-title", default="Steam Store 更新イベント", help="Updates RSS channel title")
    ap.add_argument("--updates-desc", default="Steamストアのメタデータ変更を検知して通知", help="Updates RSS channel desc")
    ap.add_argument("--cc", default="jp", help="Primary country code")
    ap.add_argument("--lang", default="ja", help="Primary language")
    ap.add_argument("--max-items", type=int, default=300, help="Max new-store items")
    ap.add_argument("--max-updates", type=int, default=500, help="Max update items")
    ap.add_argument("--max-new", type=int, default=200, help="Per run: max brand-new appids to check")
    ap.add_argument("--pending-retry", type=int, default=100, help="Per run: recheck pending appids")
    ap.add_argument("--crawl-batch", type=int, default=600, help="Per run: rolling crawl batch size")
    ap.add_argument("--baseline-if-empty", action="store_true", help="If state empty, baseline existing apps")
    args = ap.parse_args()

    state = load_state(args.state)
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    # 1) Get full app list
    try:
        apps = fetch_app_list()
    except Exception as e:
        print(f"[ERROR] fetch_app_list failed: {e}", file=sys.stderr)
        sys.exit(1)

    appids = [int(a["appid"]) for a in apps if "appid" in a]
    current_ids = set(appids)
    seen_ids = set(int(x) for x in state["seen"].keys())

    # 初回ベースライン
    if not state["seen"] and args.baseline_if_empty:
        for appid in current_ids:
            state["seen"][str(appid)] = {"published": False, "detected_at": None}
        state["applist"] = appids
        state["crawl_cursor"] = 0
        # 空の2本を出力
        with open(args.rss_out, "w", encoding="utf-8") as f:
            f.write(build_rss(args.channel_title, args.channel_link, args.channel_desc, []))
        with open(args.updates_out, "w", encoding="utf-8") as f:
            f.write(build_rss(args.updates_title, args.channel_link, args.updates_desc, []))
        save_state(args.state, state)
        print("Initialized baseline (no notifications). Next runs will track new appids.")
        return

    # applist を保存＆カーソル更新
    state["applist"] = appids
    if not isinstance(state.get("crawl_cursor"), int) or state["crawl_cursor"] >= len(appids):
        state["crawl_cursor"] = 0

    published_events: List[Dict] = []
    update_events: List[Dict] = []

    # 2) 新規に出現した AppID をチェック
    new_ids = list(current_ids - seen_ids)
    if new_ids:
        random.shuffle(new_ids)
        new_ids = new_ids[: args.max_new]
    for appid in new_ids:
        ok, data = False, None
        try:
            ok, data = fetch_appdetails(appid, args.cc, args.lang)
            time.sleep(0.2)
        except Exception as e:
            print(f"[WARN] appdetails error (new) {appid}: {e}")

        if ok:
            item = build_new_item(appid, data, now_iso)
            # 既に同appidのアイテムがあれば重複追加しない
            if not any(("/app/%d/" % appid) in it.get("link","") for it in state["items"]):
                published_events.append(item)
            state["seen"][str(appid)] = {"published": True, "detected_at": now_iso}
            # スナップショット更新 & 差分（初回は前回なし）
            snap = extract_snapshot(data)
            state.setdefault("snapshots", {})[str(appid)] = snap
        else:
            state["seen"][str(appid)] = {"published": False, "detected_at": None}
            state["pending"].append(appid)

    # 3) pending 再チェック
    if state["pending"]:
        random.shuffle(state["pending"])
        to_check = state["pending"][: args.pending_retry]
        remain = []
        for appid in to_check:
            ok, data = False, None
            try:
                ok, data = fetch_appdetails(appid, args.cc, args.lang)
                time.sleep(0.25)
            except Exception as e:
                print(f"[WARN] pending appdetails error {appid}: {e}")

            if ok:
                item = build_new_item(appid, data, now_iso)
                if not any(("/app/%d/" % appid) in it.get("link","") for it in state["items"]):
                    published_events.append(item)
                state["seen"][str(appid)] = {"published": True, "detected_at": now_iso}
                snap = extract_snapshot(data)
                prev = state.setdefault("snapshots", {}).get(str(appid))
                if prev:
                    changes = diff_snap(prev, snap)
                    if changes:
                        update_events.append(build_update_item(appid, data, changes, now_iso))
                state["snapshots"][str(appid)] = snap
            else:
                remain.append(appid)
        remain.extend(state["pending"][args.pending_retry:])
        state["pending"] = remain

    # 4) ローリング全件クロール（差分監視）
    n = args.crawl_batch
    if len(appids) > 0 and n > 0:
        start = state["crawl_cursor"] % len(appids)
        # wrap-around なスライス
        batch = appids[start:start+n] if start+n <= len(appids) else appids[start:] + appids[:(start+n) % len(appids)]
        processed = 0
        for appid in batch:
            processed += 1
            try:
                ok, data = fetch_appdetails(appid, args.cc, args.lang)
                time.sleep(0.2)
            except Exception as e:
                print(f"[WARN] crawl appdetails error {appid}: {e}")
                continue
            if not ok or not data:
                continue
            # スナップショット差分
            snap = extract_snapshot(data)
            prev = state.setdefault("snapshots", {}).get(str(appid))
            if prev:
                changes = diff_snap(prev, snap)
                if changes:
                    update_events.append(build_update_item(appid, data, changes, now_iso))
            state["snapshots"][str(appid)] = snap
        state["crawl_cursor"] = (start + processed) % len(appids)

    # 5) RSS items 更新
    if published_events:
        state["items"] = (published_events + state["items"])[: args.max_items]
    if update_events:
        state["updates"] = (update_events + state.get("updates", []))[: args.max_updates]

    # 6) RSS 書き出し（2本）
    rss_xml = build_rss(args.channel_title, args.channel_link, args.channel_desc, state["items"])
    with open(args.rss_out, "w", encoding="utf-8") as f:
        f.write(rss_xml)

    updates_xml = build_rss(args.updates_title, args.channel_link, args.updates_desc, state.get("updates", []))
    with open(args.updates_out, "w", encoding="utf-8") as f:
        f.write(updates_xml)

    # 7) state 保存
    save_state(args.state, state)

    print(f"new_ids_checked={len(new_ids)} published_now={len(published_events)} "
          f"pending={len(state['pending'])} items={len(state['items'])} "
          f"updates_now={len(update_events)} snapshots={len(state['snapshots'])} "
          f"cursor={state['crawl_cursor']}/{len(appids)}")

if __name__ == "__main__":
    main()
