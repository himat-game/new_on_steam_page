# main.py（例：なければ新規作成、既にあるなら丸ごと置き換えでもOK）
import json, time, urllib.request, urllib.error, html, re
import time

from safe_state import load_state, save_state, is_already_emitted, remember_emitted
from snaplite import append_snapshot, prune
from feed_build import write_feed
from app_db import init_db, get_app, upsert_app, compute_langs_hash, has_japanese

# ===== Steam から実データを取る実装（外部ライブラリ不要） =====
STEAM_ENDPOINT = "https://store.steampowered.com/api/appdetails?appids={appid}&l=english&cc=jp"
UA = "Mozilla/5.0 (compatible; steam-rss-bot/1.0)"

def _http_get_json(url, retries=3, backoff=1.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            # 429/5xx を含むネットワーク系は待ってリトライ
            if i < retries - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            # 最後まで駄目なら published=False で返す
            return None
        except json.JSONDecodeError:
            return None
    return None

# "supported_languages" は HTML 断片（<strong>や <br> を含む）なので整形する
_LANG_SPLIT_RE = re.compile(r"[,\n/;]+")
_TAG_RE = re.compile(r"<.*?>")

def _parse_langs(supported_languages_text):
    if not supported_languages_text:
        return []
    # HTMLタグ除去 → HTML実体参照をデコード
    txt = _TAG_RE.sub("", supported_languages_text)
    txt = html.unescape(txt)
    # 改行も区切りにして、英小文字で正規化
    parts = [p.strip().lower() for p in _LANG_SPLIT_RE.split(txt) if p.strip()]
    # 代表的な表記ゆれを正規化
    norm = {
        "english": "english",
        "japanese": "japanese",
        "japanes": "japanese",   # 稀なtypo対策
        "simplified chinese": "schinese",
        "traditional chinese": "tchinese",
        "schinese": "schinese",
        "tchinese": "tchinese",
        "korean": "korean",
        "french": "french",
        "german": "german",
        "spanish - spain": "spanish",
        "spanish - latin america": "latam_spanish",
        # 必要に応じて増やせます
    }
    langs = []
    for p in parts:
        langs.append(norm.get(p, p))  # 未知はそのまま
    # 重複削除
    return sorted(set(langs))

def fetch_app_info(appid):
    """
    戻り値の仕様:
      {
        "published": bool,              # ストアページが存在・取得成功なら True
        "title": "Game Title",
        "url":   "https://store.steampowered.com/app/<appid>",
        "langs": ["english","japanese",...],  # 小文字
        "price_cents": 1480,            # 最小単位（JPYなら円そのもの）
        "currency": "JPY"
      }
    取れない場合は "published": False を返す。
    """
    url = STEAM_ENDPOINT.format(appid=appid)
    data = _http_get_json(url, retries=3, backoff=1.0)
    if not data:
        return {"published": False}

    # レスポンスは { "<appid>": { "success": true, "data": {...} } } の形
    node = data.get(str(appid))
    if not node or not node.get("success"):
        return {"published": False}

    info = node.get("data") or {}
    # data が空のケース（非公開など）を除外
    if not info:
        return {"published": False}

    # タイトルとURL
    title = info.get("name") or f"AppID {appid}"
    url   = f"https://store.steampowered.com/app/{appid}"

    # 言語（HTML断片から抽出）
    langs_text = info.get("supported_languages", "")
    langs = _parse_langs(langs_text)

    # 価格（cc=jp を付けているので、JPY想定 / 無料なら price_overview 無し）
    pov = info.get("price_overview") or {}
    price_cents = int(pov.get("final", 0))      # JPYなら“円”。USDなら“セント”
    currency    = pov.get("currency", "JPY")    # 取れなければ JPY に寄せる

    # ストアページが「存在しているか」を published とする（新着は別途スナップショットで管理）
    return {
        "published": True,
        "title": title,
        "url": url,
        "langs": langs,
        "price_cents": price_cents,
        "currency": currency,
    }
# =======================================================

HARD_LIMIT = 2000    # 1ランで処理する最大件数（時間が長ければ1000に）
WALL_SECS  = 9*60    # 9分で自主終了（Actions timeoutより短く）

def crawl_once():
    st = load_state()
    con = init_db()

    start = time.time()
    processed = 0
    cursor = st.get("cursor", 0)

    while processed < HARD_LIMIT and (time.time() - start) < WALL_SECS:
        appid = cursor

        info = fetch_app_info(appid)
        published = bool(info.get("published"))
        title = info.get("title") or f"AppID {appid}"
        url   = info.get("url")   or f"https://store.steampowered.com/app/{appid}"
        langs = [str(x).lower() for x in info.get("langs") or []]
        price = int(info.get("price_cents") or 0)
        curr  = info.get("currency") or "USD"

        prev = get_app(con, appid)
        now_lang_hash = compute_langs_hash(langs)
        now_has_ja    = has_japanese(langs)

        # 1) 新着（初めて見るappid かつ 公開中）
        if published and prev is None:
            if not is_already_emitted(st, "new", appid):
                append_snapshot("new", {
                    "appid": appid,
                    "title": title,
                    "url": url,
                    "ts": int(time.time())
                })
                remember_emitted(st, "new", appid)

        # 2) 更新（prevあり→今回の値と比較）
        if prev is not None:
            # 日本語追加
            if prev["has_ja"] == 0 and now_has_ja == 1:
                if not is_already_emitted(st, "updates", appid):
                    append_snapshot("updates", {
                        "appid": appid,
                        "event": "lang_added_ja",
                        "title": title,
                        "url": url,
                        "ts": int(time.time())
                    })
                    remember_emitted(st, "updates", appid)
            # 価格変更
            if prev["price_cents"] != price or (prev["currency"] or "") != curr:
                append_snapshot("updates", {
                    "appid": appid,
                    "event": "price_change",
                    "old": prev["price_cents"],
                    "new": price,
                    "currency": curr,
                    "title": title,
                    "url": url,
                    "ts": int(time.time())
                })

        # 3) DBを現在値で更新（次回の比較用）
        upsert_app(con, appid, now_lang_hash, now_has_ja, price, curr)

        cursor += 1
        processed += 1

    # ランの終了処理：進行保存・RSS再構築・古いログ削除
    st["cursor"] = cursor
    save_state(st)
    write_feed("new")
    write_feed("updates")
    prune("new")
    prune("updates")

if __name__ == "__main__":
    try:
        crawl_once()
    except Exception:
        # 何かあっても進行度を残す（壊れにくい）
        st = load_state()
        save_state(st)
        raise
