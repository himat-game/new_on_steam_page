import gzip, json, os, datetime

DIR = "snapshots"
MAX_KEEP_DAYS = 14   # 7でもOK。300件RSSを安定させるなら10〜14日推奨
MAX_TOTAL_MB  = 120  # 総量の上限。超えたら古い順に削除

def _today_path():
    os.makedirs(DIR, exist_ok=True)
    d = datetime.date.today().isoformat()  # YYYY-MM-DD
    return os.path.join(DIR, f"{d}.jsonl.gz")

def append_snapshot(obj):
    # 1行＝1レコードのJSONをgzipで追記（壊れにくく、サイズ効率◎）
    p = _today_path()
    with gzip.open(p, "at", encoding="utf-8") as g:
        g.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

def prune():
    # 1) 日数で削除
    if not os.path.isdir(DIR): return
    files = [os.path.join(DIR, n) for n in os.listdir(DIR) if n.endswith(".jsonl.gz")]
    today = datetime.date.today()
    for p in files:
        try:
            d = datetime.date.fromisoformat(os.path.basename(p)[:10])
            if (today - d).days > MAX_KEEP_DAYS:
                os.remove(p)
        except:
            pass
    # 2) 総量で削除（古い順）
    files = [os.path.join(DIR, n) for n in os.listdir(DIR) if n.endswith(".jsonl.gz")]
    files.sort(key=os.path.getmtime)
    def total_mb(): return sum(os.path.getsize(x) for x in files)/(1024*1024)
    while files and total_mb() > MAX_TOTAL_MB:
        os.remove(files.pop(0))
