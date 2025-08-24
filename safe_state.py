import json, os, time, tempfile

STATE_PATH = "state.json"
RECENT_LIMIT = 3000  # フィード重複を避けるため最近出したIDを保持（新着/更新で別管理）

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"cursor": 0, "last_run": 0, "recent_emitted": {"new": [], "updates": []}}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(st):
    st["last_run"] = int(time.time())
    d = os.path.dirname(STATE_PATH) or "."
    fd, tmp = tempfile.mkstemp(prefix="state.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, STATE_PATH)  # 途中で落ちても壊れにくい
    except:
        try: os.remove(tmp)
        except: pass
        raise

def is_already_emitted(st, kind, appid):
    # kind: "new" または "updates"
    return appid in st.get("recent_emitted", {}).get(kind, [])

def remember_emitted(st, kind, appid):
    lst = st.setdefault("recent_emitted", {}).setdefault(kind, [])
    lst.append(appid)
    if len(lst) > RECENT_LIMIT:
        del lst[:-RECENT_LIMIT]
