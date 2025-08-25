#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam 新規ストア公開RSS + ストア更新RSS
- 画像/説明/価格/言語を含む
- 新規はタイトルに「（新規追加）」を付与
- 更新はタイトルに主要な変更点の要約を付与
- 429/502/503/504 に強いHTTP再試行（指数バックオフ＋スローモード）
- --crawl-seconds で実行時間の上限を設けてキャンセル回避
- state.json は長文をハッシュ保存して軽量化

初回:
  python steam_new_store_rss.py --state state.json --rss-out steam_new_store.xml --baseline-if-empty
通常:
  python steam_new_store_rss.py --state state.json --rss-out steam_new_store.xml --updates-out steam_store_updates.xml --pending-retry 100 --max-new 200 --crawl-batch 300 --crawl-seconds 1200
"""
import argparse, datetime as dt, html, io, json, os, random, re, sys, time, urllib.parse, urllib.request, hashlib
from urllib.error import HTTPError, URLError
from typing import Dict, List, Optional, Tuple

STEAM_APP_LIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

# ===== HTTP（堅牢） =====
RATE_MIN_SEC = 0.30      # 通常時の最短間隔
RATE_SLOW_SEC = 0.80     # 429後の一時スロー間隔
SLOW_MODE_SECONDS = 180  # スローモード継続（秒）
_last_request_ts = 0.0
_slow_mode_until = 0.0

def _polite_sleep():
    global _last_request_ts, _slow_mode_until
    now = time.time()
    min_gap = RATE_SLOW_SEC if now < _slow_mode_until else RATE_MIN_SEC
    wait = (_last_request_ts + min_gap) - now
    if wait > 0: time.sleep(wait)

def http_get_raw(url: str, params: Optional[Dict[str, str]] = None, timeout: int = 20) -> bytes:
    global _last_request_ts, _slow_mode_until
    if params: url = url + ("?" + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"User-Agent": "steam-new-store-rss/2.5"})
    max_retries, base_sleep = 4, 1.5
    for attempt in range(max_retries + 1):
        _polite_sleep()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                _last_request_ts = time.time()
                return data
        except HTTPError as e:
            code = e.code
            if code in (429, 502, 503, 504) and attempt < max_retries:
                if code == 429: _slow_mode_until = time.time() + SLOW_MODE_SECONDS
                sleep_sec = min(base_sleep * (2 ** attempt) * random.uniform(0.8, 1.3), 60)
                print(f"[RETRY] {code} {url} sleep {sleep_sec:.1f}s ({attempt+1}/{max_retries})")
                time.sleep(sleep_sec); continue
            raise
        except URLError:
            if attempt < max_retries:
                sleep_sec = min(base_sleep * (2 ** attempt) * random.uniform(0.8, 1.3), 30)
                print(f"[RETRY] URLError {url} sleep {sleep_sec:.1f}s ({attempt+1}/{max_retries})")
                time.sleep(sleep_sec); continue
            raise

def http_get_json(url: str, params: Optional[Dict[str, str]] = None, timeout: int = 20):
    raw = http_get_raw(url, params=params, timeout=timeout)
    try: return json.loads(raw.decode("utf-8"))
    except Exception: return json.loads(raw)

# ===== RSS =====
def guess_mime(u: str) -> str:
    if not u: return "image/jpeg"
    ul = u.lower()
    if ul.endswith(".png"): return "image/png"
    if ul.endswith(".webp"): return "image/webp"
    if ul.endswith(".jpg") or ul.endswith(".jpeg"): return "image/jpeg"
    return "image/jpeg"

def rfc822(dt_utc: dt.datetime) -> str:
    return dt_utc.strftime("%a, %d %b %Y %H:%M:%S +0000")

def truncate(s: str, n: int = 600) -> str:
    if not s: return ""
    return s if len(s) <= n else (s[: n-1] + "…")

def build_rss(channel_title: str, channel_link: str, channel_desc: str, items: List[Dict], lang: str = "ja-jp") -> str:
    last_dt = dt.datetime.utcnow() if not items else dt.datetime.fromisoformat(items[0]["pubDate"].replace("Z","+00:00")).astimezone(dt.timezone.utc)
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">\n')
    out.write("<channel>\n")
    out.write(f"<title>{html.escape(channel_title)}</title>\n")
    out.write(f"<link>{html.escape(channel_link)}</link>\n")
    out.write(f"<description>{html.escape(channel_desc)}</description>\n")
    out.write(f"<language>{html.escape(lang)}</language>\n")
    out.write(f"<lastBuildDate>{rfc822(last_dt)}</lastBuildDate>\n")
    for it in items:
        title, link, guid, pub = it.get("title","(no title)"), it.get("link",""), it.get("guid",str(random.random())), it.get("pubDate")
        pub_dt = dt.datetime.fromisoformat(pub.replace("Z","+00:00")).astimezone(dt.timezone.utc)
        desc_plain, image = truncate(it.get("description","")), it.get("image")
        out.write("<item>\n")
        out.write(f"  <title>{html.escape(title)}</title>\n")
        out.write(f"  <link>{html.escape(link)}</link>\n")
        out.write(f'  <guid isPermaLink="false">{html.escape(guid)}</guid>\n')
        out.write(f"  <pubDate>{rfc822(pub_dt)}</pubDate>\n")
        if desc_plain: out.write(f"  <description>{html.escape(desc_plain)}</description>\n")
        if image:
            mime = guess_mime(image)
            out.write(f'  <enclosure url="{html.escape(image)}" type="{mime}" />\n')
            out.write(f'  <media:content url="{html.escape(image)}" type="{mime}" />\n')
            out.write(f'  <media:thumbnail url="{html.escape(image)}" />\n')
        html_parts = []
        if image: html_parts.append(f'<p><a href="{html.escape(link)}"><img src="{html.escape(image)}" alt="{html.escape(title)}" /></a></p>')
        if desc_plain: html_parts.append(f"<p>{html.escape(desc_plain)}</p>")
        html_parts.append(f'<p><a href="{html.escape(link)}">Steamでページを開く</a></p>')
        out.write("  <content:encoded><![CDATA[" + "".join(html_parts) + "]]></content:encoded>\n")
        out.write("</item>\n")
    out.write("</channel>\n</rss>\n")
    return out.getvalue()

# ===== Storefront =====
def fetch_app_list() -> List[Dict]:
    js = http_get_json(STEAM_APP_LIST_URL)
    return js.get("applist", {}).get("apps", [])

def fetch_appdetails_once(appid: int, cc: str, lang: str) -> Tuple[bool, Optional[Dict]]:
    js = http_get_json(APPDETAILS_URL, params={"appids": str(appid), "cc": cc, "l": lang})
    node = js.get(str(appid))
    if not node or not node.get("success"): return False, None
    data = node.get("data")
    return (True, data) if data else (False, None)

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

# ===== Diff & Snapshot（軽量化）=====
LANG_TAG_RE = re.compile(r"<.*?>")
SEP_RE = re.compile(r"[;,/｜|]")

def sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()

def normalize_languages(s: Optional[str]) -> List[str]:
    if not s: return []
    txt = LANG_TAG_RE.sub("", s)
    parts = [p.strip().lower() for p in SEP_RE.split(txt) if p.strip()]
    cleaned = []
    for p in parts:
        p = p.replace("full audio","").replace("interface","").replace("subtitles","").strip()
        if p: cleaned.append(p)
    return sorted(set(cleaned))

def _strip_q(u: Optional[str]) -> Optional[str]:
    if not u: return u
    sp = urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, "", ""))

def extract_snapshot(data: dict) -> dict:
    price = (data.get("price_overview") or {}).get("final_formatted")
    langs = normalize_languages(data.get("supported_languages"))
    genres = [g.get("description","") for g in (data.get("genres") or [])]
    name = data.get("name") or ""
    snap = {
        "name": name,
        "name_hash": sha1(name),
        "short_description_hash": sha1(data.get("short_description","") or ""),
        "type": data.get("type"),
        "header_image": _strip_q(data.get("header_image")),
        "capsule_imagev5": _strip_q(data.get("capsule_imagev5")),
        "is_free": data.get("is_free"),
        "price": price or ("Free" if data.get("is_free") else ""),
        "supported_languages": sorted(set([x for x in langs if x])),
        "genres_hash": sha1(",".join(sorted(set([g for g in genres if g])))),
        "platforms": json.dumps(data.get("platforms", {}), sort_keys=True),
        "release": json.dumps(data.get("release_date", {}), sort_keys=True),
    }
    return snap

def diff_snap(old: dict, new: dict) -> List[Tuple[str,str,str]]:
    changes = []
    for k in sorted(set(old.keys()) | set(new.keys())):
        ov, nv = old.get(k), new.get(k)
        if ov != nv:
            if isinstance(ov, list): ov = ", ".join(ov)
            if isinstance(nv, list): nv = ", ".join(nv)
            changes.append((k, "" if ov is None else str(ov), "" if nv is None else str(nv)))
    return changes

# ===== Item builders =====
def get_short_description(appid: int, primary_data: dict) -> str:
    desc = primary_data.get("short_description")
    if desc: return desc
    ok, en = fetch_appdetails_once(appid, "us", "en")
    if ok and en and en.get("short_description"): return en["short_description"]
    return f"type={primary_data.get('type')}, appid={appid}"

def choose_image(data: dict) -> Optional[str]:
    return (data.get("header_image") or data.get("capsule_imagev5") or data.get("capsule_image")
            or (data.get("screenshots") or [{}])[0].get("path_full") or data.get("background"))

def build_new_item(appid: int, data: dict, now_iso: str) -> Dict:
    base = data.get("name", f"App {appid}")
    title = f"{base}（新規追加）"
    link = f"https://store.steampowered.com/app/{appid}/"
    return {"title": title, "link": link, "guid": f"steam-store-published-{appid}", "pubDate": now_iso,
            "description": get_short_description(appid, data), "image": choose_image(data)}

def pretty_change_label(k: str) -> str:
    return {
        "name":"タイトル","name_hash":"タイトル","short_description_hash":"説明","type":"タイプ",
        "header_image":"ヘッダー画像","capsule_imagev5":"カプセル画像","is_free":"無料フラグ",
        "price":"価格","supported_languages":"言語","genres_hash":"ジャンル",
        "platforms":"対応OS","release":"リリース",
    }.get(k, k)

def summarize_changes_for_title(changes: List[Tuple[str,str,str]], max_items: int = 3, max_len: int = 80) -> str:
    priority = {"price":1, "supported_languages":2, "short_description_hash":3, "header_image":4, "name":5, "name_hash":5}
    ordered = sorted(changes, key=lambda t: priority.get(t[0], 9))[:max_items]
    parts = []
    for k, ov, nv in ordered:
        if k == "price":
            parts.append(f"価格 {ov} → {nv}" if (ov and nv and ov != nv) else "価格変更")
        elif k == "supported_languages":
            old = set([x.strip() for x in (ov or "").split(",") if x.strip()])
            new = set([x.strip() for x in (nv or "").split(",") if x.strip()])
            add, rem = sorted(new-old), sorted(old-new)
            detail = []
            if add: detail.append("+"+ ",".join(add[:3]))
            if rem: detail.append("-"+ ",".join(rem[:3]))
            parts.append(f"言語 {'/'.join(detail) if detail else '変更'}")
        elif k.endswith("_hash"):
            parts.append(f"{pretty_change_label(k)}更新")
        else:
            parts.append(f"{pretty_change_label(k)}更新")
    s = " / ".join(parts)
    return s if len(s) <= max_len else (s[:max_len-1]+"…")

def build_update_item(appid: int, data: dict, changes: List[Tuple[str,str,str]], now_iso: str) -> Dict:
    base = data.get("name", f"App {appid}")
    title = f"{base}（{summarize_changes_for_title(changes)}）" if changes else f"{base}（更新）"
    link = f"https://store.steampowered.com/app/{appid}/"
    img = choose_image(data)
    lst = []
    for k, ov, nv in changes:
        lbl = pretty_change_label(k)
        if k == "supported_languages": ov, nv = ov or "-", nv or "-"
        if k.endswith("_hash"): lst.append(f"{lbl}: 更新")
        else: lst.append(f"{lbl}: {ov} → {nv}")
    desc = "; ".join(lst)
    return {"title": title, "link": link, "guid": f"steam-store-update-{appid}-{int(time.time())}",
            "pubDate": now_iso, "description": desc, "image": img}

# ===== State I/O =====
def load_state(p: str) -> Dict:
    if not os.path.exists(p):
        return {"seen": {}, "pending": [], "items": [], "updates": [], "snapshots": {}, "applist": [], "crawl_cursor": 0}
    with open(p, "r", encoding="utf-8") as f: return json.load(f)

def save_state(p: str, s: Dict) -> None:
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)

# ===== Main =====
def main():
    ap = argparse.ArgumentParser(description="Steam new-store & updates RSS (robust, time-bounded, compact state).")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--rss-out", default="steam_new_store.xml")
    ap.add_argument("--updates-out", default="steam_store_updates.xml")
    ap.add_argument("--channel-title", default="Steam: Newly Published Store Pages")
    ap.add_argument("--channel-link", default="https://store.steampowered.com/")
    ap.add_argument("--channel-desc", default="Games whose Steam store page just went public (detected)")
    ap.add_argument("--updates-title", default="Steam Store 更新イベント")
    ap.add_argument("--updates-desc", default="Steamストアのメタデータ変更を検知して通知")
    ap.add_argument("--cc", default="jp"); ap.add_argument("--lang", default="ja")
    ap.add_argument("--max-items", type=int, default=300)
    ap.add_argument("--max-updates", type=int, default=500)
    ap.add_argument("--max-new", type=int, default=200)
    ap.add_argument("--pending-retry", type=int, default=100)
    ap.add_argument("--crawl-batch", type=int, default=300)
    ap.add_argument("--crawl-seconds", type=int, default=1200)
    ap.add_argument("--baseline-if-empty", action="store_true")
    args = ap.parse_args()

    state = load_state(args.state)
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    # 1) 全AppIDリスト
    try: apps = fetch_app_list()
    except Exception as e:
        print(f"[ERROR] fetch_app_list failed: {e}", file=sys.stderr); sys.exit(1)

    appids = [int(a["appid"]) for a in apps if "appid" in a]
    current_ids, seen_ids = set(appids), set(int(x) for x in state["seen"].keys())

    # 初回ベースライン
    if not state["seen"] and args.baseline_if_empty:
        for aid in current_ids: state["seen"][str(aid)] = {"published": False, "detected_at": None}
        state["applist"], state["crawl_cursor"] = appids, 0
        open(args.rss_out, "w", encoding="utf-8").write(build_rss(args.channel_title, args.channel_link, args.channel_desc, []))
        open(args.updates_out, "w", encoding="utf-8").write(build_rss(args.updates_title, args.channel_link, args.updates_desc, []))
        save_state(args.state, state)
        print("Initialized baseline."); return

    state["applist"] = appids
    if not isinstance(state.get("crawl_cursor"), int) or state["crawl_cursor"] >= len(appids): state["crawl_cursor"] = 0

    published_events, update_events = [], []

    # 2) 新規AppID
    new_ids = list(current_ids - seen_ids)
    if new_ids: random.shuffle(new_ids); new_ids = new_ids[: args.max_new]
    for aid in new_ids:
        try: ok, data = fetch_appdetails(aid, args.cc, args.lang)
        except Exception as e:
            print(f"[WARN] appdetails error (new) {aid}: {e}"); ok = False
        if ok:
            it = build_new_item(aid, data, now_iso)
            if not any(("/app/%d/" % aid) in x.get("link","") for x in state["items"]): published_events.append(it)
            state["seen"][str(aid)] = {"published": True, "detected_at": now_iso}
            state.setdefault("snapshots", {})[str(aid)] = extract_snapshot(data)
        else:
            state["seen"][str(aid)] = {"published": False, "detected_at": None}
            state["pending"].append(aid)

    # 3) pending 再試行
    if state["pending"]:
        random.shuffle(state["pending"])
        to_check, remain = state["pending"][: args.pending_retry], []
        for aid in to_check:
            try: ok, data = fetch_appdetails(aid, args.cc, args.lang)
            except Exception as e:
                print(f"[WARN] pending appdetails error {aid}: {e}"); ok = False
            if ok:
                it = build_new_item(aid, data, now_iso)
                if not any(("/app/%d/" % aid) in x.get("link","") for x in state["items"]): published_events.append(it)
                state["seen"][str(aid)] = {"published": True, "detected_at": now_iso}
                snap, prev = extract_snapshot(data), state.setdefault("snapshots", {}).get(str(aid))
                if prev:
                    ch = diff_snap(prev, snap)
                    if ch: update_events.append(build_update_item(aid, data, ch, now_iso))
                state["snapshots"][str(aid)] = snap
            else:
                remain.append(aid)
        remain.extend(state["pending"][args.pending_retry:]); state["pending"] = remain

    # 4) ローリング差分監視（時間上限あり）
    n = args.crawl_batch
    deadline = time.time() + args.crawl_seconds if args.crawl_seconds and args.crawl_seconds > 0 else None
    if len(appids) > 0 and n > 0:
        start = state["crawl_cursor"] % len(appids)
        batch = appids[start:start+n] if start+n <= len(appids) else appids[start:] + appids[:(start+n) % len(appids)]
        processed = 0
        for aid in batch:
            if deadline and time.time() >= deadline:
                print("[INFO] crawl time budget reached, stopping this run"); break
            processed += 1
            try: ok, data = fetch_appdetails(aid, args.cc, args.lang)
            except Exception as e:
                print(f"[WARN] crawl appdetails error {aid}: {e}"); continue
            if not ok or not data: continue
            snap, prev = extract_snapshot(data), state.setdefault("snapshots", {}).get(str(aid))
            if prev:
                ch = diff_snap(prev, snap)
                if ch: update_events.append(build_update_item(aid, data, ch, now_iso))
            state["snapshots"][str(aid)] = snap
        state["crawl_cursor"] = (start + processed) % len(appids)

    # 5) RSS と state 保存
    if published_events: state["items"] = (published_events + state["items"])[: args.max_items]
    if update_events:    state["updates"] = (update_events + state.get("updates", []))[: args.max_updates]
    open(args.rss_out, "w", encoding="utf-8").write(build_rss(args.channel_title, args.channel_link, args.channel_desc, state["items"]))
    open(args.updates_out, "w", encoding="utf-8").write(build_rss(args.updates_title, args.channel_link, args.updates_desc, state.get("updates", [])))
    save_state(args.state, state)

    print(f"new_ids_checked={len(new_ids)} published_now={len(published_events)} pending={len(state['pending'])} items={len(state['items'])} updates_now={len(update_events)} snapshots={len(state['snapshots'])} cursor={state['crawl_cursor']}/{len(appids)}")

if __name__ == "__main__":
    main()
