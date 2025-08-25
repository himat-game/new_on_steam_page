# -*- coding: utf-8 -*-
"""
Steam 新規ストアページ検出 → RSS出力
- 状態は Artifact の state/state.json.gz に保存（リポジトリ非コミット）
- 毎回、Steamのアプリ一覧を取得して「前回の続き」から一定件数だけ走査
- 既にストアページが存在していて、まだ seen_ids に無い appid を「新規発見」としてRSSに出力
- 軽量化のため、状態は整数配列 + gzip、APIは必要最小回数に限定
"""

import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time
from typing import Dict, List, Any, Tuple

import requests

# --------- 設定（環境変数でも上書き可能） ----------
DEFAULT_ITEMS_PER_RUN = int(os.getenv("ITEMS_PER_RUN", "300"))  # 1回の走査件数
DEFAULT_LANG = os.getenv("STEAM_LANG", "en")                    # appdetailsの言語
DEFAULT_CC = os.getenv("STEAM_CC", "US")                        # appdetailsの地域
DEFAULT_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "6.0"))       # リクエストタイムアウト秒
DEFAULT_PAUSE = float(os.getenv("HTTP_PAUSE", "0.03"))          # appdetails間の待ち（秒）

RSS_PATH = os.getenv("RSS_PATH", "steam_new_store.xml")
STATE_PATH = os.getenv("STATE_PATH", "state/state.json.gz")

USER_AGENT = os.getenv("USER_AGENT", "steam-new-store-rss (+https://github.com/himat-game/new_on_steam_page)")

# --------- ユーティリティ ----------
def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s

def safe_get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

# --------- 状態の読み書き（gzip JSON） ----------
def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"seen_ids": [], "cursor": 0, "stats": {}}
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            s = json.load(f)
    else:
        with open(path, "r", encoding="utf-8") as f:
            s = json.load(f)

    # 旧形式サポート：{"seen": {"123": true}} -> seen_ids へ
    if "seen_ids" not in s:
        if isinstance(s.get("seen"), dict):
            s["seen_ids"] = [int(k) for k in s["seen"].keys()]
        else:
            s["seen_ids"] = []
    s.setdefault("cursor", 0)
    s.setdefault("stats", {})
    return s

def save_state(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # seen_ids はユニーク & 昇順で保存（gzip圧縮効率UP）
    if "seen_ids" in state:
        state["seen_ids"] = sorted(set(int(x) for x in state["seen_ids"]))
    if path.endswith(".gz"):
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))

# --------- Steam API ----------
APPLIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"

def fetch_applist(sess: requests.Session, timeout: float) -> List[Dict[str, Any]]:
    r = sess.get(APPLIST_URL, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    apps = safe_get(data, "applist", "apps", default=[])
    # apps: [{"appid": 10, "name": "Counter-Strike"}, ...]
    return apps

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

def has_store_page_and_info(sess: requests.Session, appid: int, lang: str, cc: str, timeout: float) -> Tuple[bool, Dict[str, Any]]:
    """
    ストアページがあるか確認し、基本情報を抜き出す
    """
    params = {
        "appids": str(appid),
        "l": lang,
        "cc": cc,
        # filters を付けると返却が軽くなる（未対応なら無視される）
        "filters": "basic,price_overview,release_date,header_image,short_description,genres,is_free,type"
    }
    try:
        r = sess.get(APPDETAILS_URL, params=params, timeout=timeout)
        r.raise_for_status()
        j = r.json()
        info = j.get(str(appid), {})
        if not info or not info.get("success"):
            return False, {}
        data = info.get("data", {})
        if not data or not isinstance(data, dict):
            return False, {}

        # 一部アプリは非ゲーム（tool, dlc など）も含むが、ここでは全て許容
        name = data.get("name") or ""
        if not name.strip():
            return False, {}

        # ページが存在するものとして扱う
        picked = {
            "appid": appid,
            "name": name,
            "type": data.get("type"),
            "is_free": data.get("is_free", False),
            "header_image": data.get("header_image"),
            "short_description": data.get("short_description"),
            "release_date": data.get("release_date", {}),
            "genres": data.get("genres", []),
            "price_overview": data.get("price_overview", {}),
            "steam_appid": data.get("steam_appid", appid),
        }
        return True, picked
    except requests.RequestException:
        return False, {}

# --------- RSS ----------
from xml.sax.saxutils import escape as xml_escape

def build_rss(now_utc: dt.datetime, items: List[Dict[str, Any]], rss_path: str) -> None:
    """
    RSS 2.0 を生成（新規検出分のみを items に入れて都度追記ではなく丸ごと出力）
    - Feed 全体は「今回の新規検出分」のみ（軽量化のため履歴は持たない）
    - 履歴を持ちたい場合は、別ファイルでアペンド管理してください
    """
    # 軽量化のため、空なら最小限のRSSを出力して終了
    if not items:
        with open(rss_path, "w", encoding="utf-8") as f:
            f.write("""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Steam: Newly Published Store Pages (This Run)</title>
<link>https://store.steampowered.com/</link>
<description>Newly discovered Steam store pages found by crawler (this run only).</description>
<lastBuildDate>{}</lastBuildDate>
</channel>
</rss>""".format(now_utc.strftime("%a, %d %b %Y %H:%M:%S +0000")))
        return

    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<rss version="2.0">')
    lines.append("<channel>")
    lines.append("<title>Steam: Newly Published Store Pages (This Run)</title>")
    lines.append("<link>https://store.steampowered.com/</link>")
    lines.append("<description>Newly discovered Steam store pages found by crawler (this run only).</description>")
    lines.append("<lastBuildDate>{}</lastBuildDate>".format(now_utc.strftime("%a, %d %b %Y %H:%M:%S +0000")))

    for it in items:
        appid = it["appid"]
        title = xml_escape(it.get("name") or f"App {appid}")
        link = f"https://store.steampowered.com/app/{appid}/"
        desc_parts = []
        sd = (it.get("short_description") or "").strip()
        if sd:
            desc_parts.append(xml_escape(sd))
        rls = it.get("release_date") or {}
        if isinstance(rls, dict):
            coming = rls.get("coming_soon")
            date_str = rls.get("date")
            if coming is True:
                desc_parts.append("Coming Soon")
            if date_str:
                desc_parts.append(f"Release Date: {xml_escape(date_str)}")
        if it.get("is_free"):
            desc_parts.append("Free to Play")
        pov = it.get("price_overview") or {}
        if "final" in pov and "currency" in pov:
            # final は通貨最小単位。軽量のため四捨五入のみ
            final = int(pov.get("final", 0))
            currency = pov.get("currency", "USD")
            # 例: JPYなら 12345 -> 12345 円、USDなら 999 -> $9.99
            if currency.upper() == "JPY":
                desc_parts.append(f"Price: {final} JPY")
            else:
                desc_parts.append(f"Price: {final/100:.2f} {currency}")

        header = it.get("header_image")
        if header:
            # 画像は <description> 内に簡易的に埋め込み（RSSリーダーの互換性考慮）
            # ※ HTML を入れるだけなのでCDATAでラップしない（軽量優先）
            desc_parts.append(f'<br/><img src="{xml_escape(header)}" referrerpolicy="no-referrer" />')

        description = "<br/>".join(desc_parts) if desc_parts else "Newly discovered store page."

        lines.append("<item>")
        lines.append(f"<title>{title}</title>")
        lines.append(f"<link>{xml_escape(link)}</link>")
        lines.append(f"<guid isPermaLink='false'>steam-app-{appid}</guid>")
        lines.append(f"<pubDate>{now_utc.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>")
        lines.append(f"<description>{description}</description>")
        lines.append("</item>")

    lines.append("</channel>")
    lines.append("</rss>")

    with open(rss_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

# --------- メイン処理 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-path", default=STATE_PATH, help="Path to gzipped JSON state (e.g., state/state.json.gz)")
    ap.add_argument("--rss-path", default=RSS_PATH, help="Output RSS file path")
    ap.add_argument("--items", type=int, default=DEFAULT_ITEMS_PER_RUN, help="Apps to scan per run")
    ap.add_argument("--lang", default=DEFAULT_LANG, help="Steam store language (e.g., en, ja)")
    ap.add_argument("--cc", default=DEFAULT_CC, help="Steam country code for pricing")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds")
    ap.add_argument("--pause", type=float, default=DEFAULT_PAUSE, help="Pause between requests seconds")
    args = ap.parse_args()

    state = load_state(args.state_path)
    seen_ids: List[int] = list(state.get("seen_ids", []))
    seen_set = set(seen_ids)
    cursor = int(state.get("cursor", 0))

    sess = http_session()

    # 1) 全アプリ一覧を取得
    try:
        apps = fetch_applist(sess, args.timeout)
    except Exception as e:
        print(f"failed to fetch applist: {e}", file=sys.stderr)
        apps = []

    total = len(apps)
    if total == 0:
        # applist が取れない時は安全に終了（状態は保存）
        state["stats"]["last_error"] = "applist=0"
        save_state(args.state_path, {"seen_ids": seen_ids, "cursor": cursor, "stats": state.get("stats", {})})
        # 空でもRSSは最低限を出す（読み手側のエラー回避）
        build_rss(utcnow(), [], args.rss_path)
        print(f"new_ids_checked=0 published_now=0 items={args.items} updates_now=0 snapshots={len(seen_ids)} cursor={cursor}/0")
        return

    # cursor が範囲外なら巻き戻す
    if cursor >= total or cursor < 0:
        cursor = 0

    # 2) 今回処理する範囲を決定
    items_to_check = args.items
    end = min(cursor + items_to_check, total)

    # 3) appdetails を順次確認
    published_now: List[Dict[str, Any]] = []
    new_ids_checked = 0

    for i in range(cursor, end):
        app = apps[i]
        appid = int(app.get("appid", 0))
        if appid <= 0:
            continue

        new_ids_checked += 1

        if appid in seen_set:
            # 既知はスキップ
            continue

        ok, info = has_store_page_and_info(sess, appid, args.lang, args.cc, args.timeout)
        # ネットワークや一時失敗は軽くリトライ（1回）
        if not ok:
            time.sleep(args.pause)
            ok, info = has_store_page_and_info(sess, appid, args.lang, args.cc, args.timeout)

        if ok:
            # ストアページを新規発見
            seen_set.add(appid)
            seen_ids.append(appid)
            published_now.append(info)

        # サーバーに優しく（ごく短い待ち）
        if args.pause > 0:
            time.sleep(args.pause)

    # 4) カーソル更新（末尾まで来たら0に戻す）
    new_cursor = end if end < total else 0

    # 5) RSS 出力（今回分のみ）
    now_utc = utcnow()
    try:
        build_rss(now_utc, published_now, args.rss_path)
    except Exception as e:
        # RSS生成に失敗しても状態は保存
        print(f"failed to write RSS: {e}", file=sys.stderr)

    # 6) 状態保存（gzip, seen_idsは昇順ユニーク化）
    state_out = {
        "seen_ids": seen_ids,
        "cursor": new_cursor,
        "stats": {
            "last_run": now_utc.isoformat(),
            "total_apps": total,
            "last_published_count": len(published_now),
        },
    }
    save_state(args.state_path, state_out)

    # 7) 進捗ログ（Actionsの末尾で見やすい形式）
    print(
        f"new_ids_checked={new_ids_checked} "
        f"published_now={len(published_now)} "
        f"items={args.items} updates_now=0 "
        f"snapshots={len(state_out['seen_ids'])} "
        f"cursor={new_cursor}/{total}"
    )

if __name__ == "__main__":
    main()
