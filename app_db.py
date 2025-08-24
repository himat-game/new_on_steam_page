import sqlite3, os, time

DB_PATH = "app_state.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
  appid        INTEGER PRIMARY KEY,
  langs_hash   INTEGER,
  has_ja       INTEGER,
  price_cents  INTEGER,
  currency     TEXT,
  first_seen_ts INTEGER,
  last_seen_ts  INTEGER
);
"""

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute(SCHEMA)
    con.commit()
    return con

def get_app(con, appid):
    cur = con.execute("SELECT appid, langs_hash, has_ja, price_cents, currency FROM apps WHERE appid=?", (appid,))
    row = cur.fetchone()
    if not row: return None
    return {
        "appid": row[0],
        "langs_hash": row[1],
        "has_ja": row[2],
        "price_cents": row[3],
        "currency": row[4],
    }

def upsert_app(con, appid, langs_hash, has_ja, price_cents, currency):
    ts = int(time.time())
    cur = con.execute("SELECT 1 FROM apps WHERE appid=?", (appid,))
    if cur.fetchone():
        con.execute(
            "UPDATE apps SET langs_hash=?, has_ja=?, price_cents=?, currency=?, last_seen_ts=? WHERE appid=?",
            (langs_hash, has_ja, price_cents, currency, ts, appid)
        )
    else:
        con.execute(
            "INSERT INTO apps(appid, langs_hash, has_ja, price_cents, currency, first_seen_ts, last_seen_ts) VALUES(?,?,?,?,?,?,?)",
            (appid, langs_hash, has_ja, price_cents, currency, ts, ts)
        )
    con.commit()

def compute_langs_hash(langs):
    # langs は ["english","japanese", ...] の小文字リスト想定
    # 順序に依らないようにソート→タプル→ハッシュ
    return hash(tuple(sorted(set(langs))))

def has_japanese(langs):
    return 1 if "japanese" in set(langs) else 0
