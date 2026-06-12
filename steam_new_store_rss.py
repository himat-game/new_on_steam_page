#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam 新規ストア公開RSS + ストア更新イベントRSS
（画像・説明・価格・言語対応 / ローリング全件クロール / 変更点はタイトル要約 / 新規は「（新規追加）」）
＋ 429/502/503/504 に強い HTTP 再試行（指数バックオフ＆スローモード）
＋ クロール時間上限（--crawl-seconds）で長時間実行を回避

初回:
  python steam_new_store_rss.py --state state.json --rss-out steam_new_store.xml --baseline-if-empty
通常:
  python steam_new_store_rss.py --state state.json --rss-out steam_new_store.xml --pending-retry 100 --max-new 200 --crawl-batch 400 --crawl-seconds 1500
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
from urllib.error import HTTPError, URLError
from typing import Dict, List, Optional, Tuple

# ここを新しいエンドポイントに変更
STEAM_APP_LIST_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

# =========================
# HTTP helpers（堅牢版）
# =========================

# 通常時の最小間隔 / 429後のスロー間隔 / スローモード継続時間（秒）
RATE_MIN_SEC = 0.30
RATE_SLOW_SEC = 0.80
SLOW_MODE_SECONDS = 180  # ← 3分（短め）

_last_request_ts = 0.0
_slow_mode_until = 0.0

# --- state I/O helpers: gzip対応 & 自動ダイエット（薄く保持） ---
import gzip

# 最大保持件数（RSS肥大防止）
MAX_ITEMS    = 300     # 新規RSS
MAX_UPDATES  = 500     # 更新RSS
MAX_PENDING  = 5000    # pendingキュー

def _minimize_state(d: dict) -> dict:
    """必要最小限を薄く保持。更新検出に必要なスナップショットは最小項目のみ保持。"""
    # 1) seen はキー存在だけ使うので値を1に潰す
    seen_src = d.get("seen", {})
    seen = {str(k): 1 for k in seen_src.keys()} if isinstance(seen_src, dict) else {}

    # 2) カーソルは 'crawl_cursor' を採用（無ければ cursor→0）
    crawl_cursor = int(d.get("crawl_cursor", d.get("cursor", 0) or 0))

    # 3) items / updates はRSSに必要なので中身は残しつつ件数を上限でスライス（先頭が最新想定）
    items = d.get("items", [])
    if not isinstance(items, list): items = []
    items = items[:MAX_ITEMS]

    updates = d.get("updates", [])
    if not isinstance(updates, list): updates = []
    updates = updates[:MAX_UPDATES]

    # 4) pending はそのまま（件数上限のみ）
    pending = d.get("pending", [])
    if not isinstance(pending, list): pending = []
    pending = pending[:MAX_PENDING]

    # 5) snapshots は更新検出に必要なキーだけ残す（価格/言語）
    snaps_src = d.get("snapshots", {})
    snaps = {}
    if isinstance(snaps_src, dict):
        for k, v in snaps_src.items():
            if isinstance(v, dict):
                price = v.get("price") or ("Free" if v.get("is_free") else "")
                langs = v.get("supported_languages") or []
                if isinstance(langs, str):
                    langs = [x.strip() for x in langs.split(",") if x.strip()]
                if not isinstance(langs, list):
                    langs = []
                snaps[str(k)] = {
                    "price": price,
                    "supported_languages": sorted(set(langs)),
                }

    return {
        "seen": seen,
        "pending": pending,
        "items": items,
        "updates": updates,
        "snapshots": snaps,
        "applist": d.get("applist", []),  # 無くても動くが残しても軽い
        "crawl_cursor": crawl_cursor,
    }

def load_state(path: str) -> dict:
    """BOM混入耐性・.gz対応・読込時にも最小化"""
    p = path
    # 初回移行: .gzが無いが同名.jsonがある場合は .json を読む
    if p.endswith(".gz") and not os.path.exists(p) and os.path.exists(p[:-3]):
        p = p[:-3]
    if not os.path.exists(p):
        return _minimize_state({})
    if p.endswith(".gz"):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return _minimize_state(json.load(f))
    else:
        # BOM対策
        with open(p, "r", encoding="utf-8-sig") as f:
            return _minimize_state(json.load(f))

def save_state(path: str, state: dict) -> None:
    """保存前に毎回ダイエットし、.gzなら圧縮保存"""
    d = _minimize_state(state)
    text = json.dumps(d, ensure_ascii=False, separators=(',',':'))
    if path.endswith(".gz"):
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(text)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
# --- /helpers ---

def _polite_sleep():
    """直前から一定時間を空ける（429検知後はスローモード）"""
    global _last_request_ts, _slow_mode_until
    now = time.time()
    min_gap = RATE_SLOW_SEC if now < _slow_mode_until else RATE_MIN_SEC
    wait = (_last_request_ts + min_gap) - now
    if wait > 0:
        time.sleep(wait)

def http_get_raw(url: str, params: Optional[Dict[str, str]] = None, timeout: int = 20) -> bytes:
    """429/5xxに強い取得：指数バックオフ＋ジッター＋一時スローモード"""
    global _last_request_ts, _slow_mode_until
    if params:
        url = url + ("?" + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={
        "User-Agent": "steam-new-store-rss/2.4 (+https://example.com)"
    })

    max_retries = 4           # ← 少し控えめに
    base_sleep = 1.5          # 秒
    for attempt in range(max_retries + 1):
        _polite_sleep()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                _last_request_ts = time.time()
                return data
        except HTTPError as e:
            code = e.code
            if code in (429, 502, 503, 504) and attempt < max_retries:
                if code == 429:
                    _slow_mode_until = time.time() + SLOW_MODE_SECONDS
                sleep_sec = base_sleep * (2 ** attempt) * random.uniform(0.8, 1.3)
                sleep_sec = min(sleep_sec, 60)
                print(f"[RETRY] {code} on {url} -> sleep {sleep_sec:.1f}s (attempt {attempt+1}/{max_retries})")
                time.sleep(sleep_sec)
                continue
            raise
        except URLError:
            if attempt < max_retries:
                sleep_sec = base_sleep * (2 ** attempt) * random.uniform(0.8, 1.3)
                sleep_sec = min(sleep_sec, 30)
                print(f"[RETRY] URLError on {url} -> sleep {sleep_sec:.1f}s (attempt {attempt+1}/{max_retries})")
                time.sleep(sleep_sec)
                continue
            raise

def http_get_json(url: str, params: Optional[Dict[str, str]] = None, timeout: int = 20):
    data = http_get_raw(url, params=params, timeout=timeout)
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return json.loads(data)

# =========================
# RSS helpers
# =========================

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

XML_INVALID_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")

def clean_xml_text(s):
    if s is None:
        return ""
    return XML_INVALID_RE.sub("", str(s))

def build_rss(channel_title: str, channel_link: str, channel_desc: str, items: List[Dict], lang: str = "ja-jp") -> str:
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
        title = clean_xml_text(it.get("title", "(no title)"))
        link = clean_xml_text(it.get("link", ""))
        guid = clean_xml_text(it.get("guid", str(random.random())))
        pub = it.get("pubDate")
        pub_dt = dt.datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        desc_plain = truncate(clean_xml_text(it.get("description", "")))
        image = clean_xml_text(it.get("image", ""))

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

# =========================
# Storefront helpers
# =========================

def fetch_app_list() -> List[Dict]:
    """
    Steam の AppList を IStoreService/GetAppList からページング取得する。
    STEAM_WEB_API_KEY 環境変数に Web API キーが必要。
    """
    api_key = os.environ.get("STEAM_WEB_API_KEY")
    if not api_key:
        raise RuntimeError("STEAM_WEB_API_KEY is not set")

    apps: List[Dict] = []
    last_appid = 0

    while True:
        params = {
            "key": api_key,
            "last_appid": last_appid,
            "max_results": 50000,
        }

        js = http_get_json(STEAM_APP_LIST_URL, params=params)
        resp = js.get("response", {})
        chunk = resp.get("apps", [])
        if not chunk:
            break

        apps.extend(chunk)

        if not resp.get("have_more_results"):
            break

        last_appid = resp.get("last_appid", 0)
        if not last_appid:
            break

    return apps

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

# =========================
# Diff helpers（価格・言語）
# =========================

LANG_TAG_RE = re.compile(r"<.*?>")
SEP_RE = re.compile(r"[;,/｜|]")

def normalize_languages(s: Optional[str]) -> List[str]:
    if not s: return []
    txt = LANG_TAG_RE.sub("", s)
    parts = [p.strip().lower() for p in SEP_RE.split(txt) if p.strip()]
    cleaned = []
    for p in parts:
        p = p.replace("full audio", "").replace("interface", "").replace("subtitles","").strip()
        if p: cleaned.append(p)
    return sorted(set(cleaned))

def extract_snapshot(data: dict) -> dict:
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
        "supported_languages": sorted(set([x for x in langs if x])),
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

# =========================
# Item builders
# =========================

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
    base_name = data.get("name", f"App {appid}")
    title = f"{base_name}（新規追加）"  # 新規公開の印
    link = f"https://store.steampowered.com/app/{appid}/"
    image = choose_image(data)
    desc = get_short_description(appid, data)
    guid = f"steam-store-published-{appid}"  # 安定GUID（重複防止）
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
        "supported_languages": "言語",
        "genres": "ジャンル",
        "platforms": "対応OS",
        "release": "リリース",
    }.get(k, k)

def summarize_changes_for_title(changes: List[Tuple[str,str,str]], max_items: int = 3, max_len: int = 80) -> str:
    priority = {"price": 1, "supported_languages": 2, "short_description": 3, "header_image": 4, "name": 5}
    ordered = sorted(changes, key=lambda t: priority.get(t[0], 9))
    parts = []
    for k, ov, nv in ordered[:max_items]:
        if k == "price":
            if ov and nv and ov != nv:
                parts.append(f"価格 {ov} → {nv}")
            else:
                parts.append("価格変更")
        elif k == "supported_languages":
            old = set([x.strip() for x in ov.split(",") if x.strip()]) if ov else set()
            new = set([x.strip() for x in nv.split(",") if x.strip()]) if nv else set()
            added = sorted(new - old)
            removed = sorted(old - new)
            detail = []
            if added:   detail.append("+" + ",".join(added[:3]))
            if removed: detail.append("-" + ",".join(removed[:3]))
            parts.append(f"言語 {'/'.join(detail) if detail else '変更'}")
        else:
            parts.append(f"{pretty_change_label(k)}更新")
    summary = " / ".join(parts)
    return summary if len(summary) <= max_len else (summary[: max_len - 1] + "…")

def build_update_item(appid: int, data: dict, changes: List[Tuple[str,str,str]], now_iso: str) -> Dict:
    base_name = data.get('name', f'App {appid}')
    summary_title = summarize_changes_for_title(changes)
    title = f"{base_name}（{summary_title}）" if summary_title else f"{base_name}（更新）"
    link = f"https://store.steampowered.com/app/{appid}/"
    image = choose_image(data)
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

# =========================
# Main
# =========================

def main():
    ap = argparse.ArgumentParser(description="Steam new-store & store-updates RSS generator (robust HTTP backoff & time-bounded crawl).")
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
    ap.add_argument("--crawl-batch", type=int, default=400, help="Per run: rolling crawl batch size")
    ap.add_argument("--crawl-seconds", type=int, default=1500, help="Soft time budget for rolling crawl (seconds)")
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
        except Exception as e:
            print(f"[WARN] appdetails error (new) {appid}: {e}")
            ok = False
        if ok:
            item = build_new_item(appid, data, now_iso)
            if not any(("/app/%d/" % appid) in it.get("link","") for it in state["items"]):
                published_events.append(item)
            state["seen"][str(appid)] = {"published": True, "detected_at": now_iso}
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
            except Exception as e:
                print(f"[WARN] pending appdetails error {appid}: {e}")
                ok = False
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

    # 4) ローリング全件クロール（差分監視・時間上限あり）
    n = args.crawl_batch
    crawl_deadline = time.time() + args.crawl_seconds if args.crawl_seconds and args.crawl_seconds > 0 else None
    if len(appids) > 0 and n > 0:
        start = state["crawl_cursor"] % len(appids)
        batch = appids[start:start+n] if start+n <= len(appids) else appids[start:] + appids[:(start+n) % len(appids)]
        processed = 0
        for appid in batch:
            # 時間上限（ソフト）に達したら次回に持ち越し
            if crawl_deadline and time.time() >= crawl_deadline:
                print("[INFO] crawl time budget reached, stopping this run")
                break
            processed += 1
            try:
                ok, data = fetch_appdetails(appid, args.cc, args.lang)
            except Exception as e:
                print(f"[WARN] crawl appdetails error {appid}: {e}")
                continue
            if not ok or not data:
                continue
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
