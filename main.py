# main.py（例：なければ新規作成、既にあるなら丸ごと置き換えでもOK）
import time

from safe_state import load_state, save_state, is_already_emitted, remember_emitted
from snaplite import append_snapshot, prune
from feed_build import write_feed
from app_db import init_db, get_app, upsert_app, compute_langs_hash, has_japanese

# ===== ここをあなたの既存取得処理で置き換える =====
def fetch_app_info(appid):
    """
    必要な戻り値:
      {
        "published": bool,              # ストアページが公開中か
        "title": "Game Title",          # 任意（あると見栄え良い）
        "url":   "https://store.steampowered.com/app/<appid>",
        "langs": ["english","japanese",...],  # 小文字の英語表記でOK
        "price_cents": 1480,            # 通貨の最小単位（JPYなら円）
        "currency": "JPY"
      }
    取れない項目は適当なデフォルトで埋めてOK
    """
    # --- ダミー（テスト用）。本番ではあなたの処理に差し替え ---
    return {
        "published": True,
        "title": f"Dummy {appid}",
        "url": f"https://store.steampowered.com/app/{appid}",
        "langs": ["english"],
        "price_cents": 0,
        "currency": "JPY",
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
