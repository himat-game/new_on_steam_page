import json, os, time, tempfile

STATE_PATH = "state.json"
RECENT_LIMIT = 3000  # 直近出力のappidを保持（重複再出力の防止用）

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"cursor": 0, "last_run": 0, "recent_emitted": []}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(st):
    st["last_run"] = int(time.time())
    d = os.path.dirname(STATE_PATH) or "."
    fd, tmp = tempfile.mkstemp(prefix="state.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, STATE_PATH)  # 原子的置換（途中で落ちても壊れにくい）
    except:
        try: os.remove(tmp)
        except: pass
        raise

def is_already_emitted(st, appid):
    # 最近出したIDの重複を避ける（簡易）
    return appid in st.get("recent_emitted", [])

def remember_emitted(st, appid):
    lst = st.setdefault("recent_emitted", [])
    lst.append(appid)
    if len(lst) > RECENT_LIMIT:
        del lst[:-RECENT_LIMIT]
