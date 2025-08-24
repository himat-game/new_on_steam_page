import gzip, json, os, datetime, glob

BASE_DIR = "snapshots"
MAX_KEEP_DAYS = 14   # 7でもOK。300件RSSを安定供給するなら10〜14日推奨
MAX_TOTAL_MB  = 120  # kindごとの総量上限。超えたら古い順に削除

def _dir_of(kind):
    d = os.path.join(BASE_DIR, kind)
    os.makedirs(d, exist_ok=True)
    return d

def _today_path(kind):
    d = _dir_of(kind)
    day = datetime.date.today().isoformat()  # YYYY-MM-DD
    return os.path.join(d, f"{day}.jsonl.gz")

def append_snapshot(kind, obj):
    # kind: "new" or "updates"
    p = _today_path(kind)
    with gzip.open(p, "at", encoding="utf-8") as g:
        g.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

def iter_new_to_old(kind):
    d = _dir_of(kind)
    files = sorted(glob.glob(os.path.join(d, "*.jsonl.gz")), reverse=True)
    for p in files:
        with gzip.open(p, "rt", encoding="utf-8") as g:
            for line in reversed(g.read().splitlines()):
                yield json.loads(line)

def prune(kind):
    d = _dir_of(kind)
    # 1) 日数で削除
    today = datetime.date.today()
    for name in os.listdir(d):
        if not name.endswith(".jsonl.gz"): continue
        p = os.path.join(d, name)
        try:
            day = datetime.date.fromisoformat(name[:10])
            if (today - day).days > MAX_KEEP_DAYS:
                os.remove(p)
        except:
            pass
    # 2) 総量で削除（古い順）
    files = [os.path.join(d, n) for n in os.listdir(d) if n.endswith(".jsonl.gz")]
    files.sort(key=os.path.getmtime)
    def total_mb():
        return sum(os.path.getsize(x) for x in files) / (1024*1024)
    while files and total_mb() > MAX_TOTAL_MB:
        os.remove(files.pop(0))
