"""Gmail watch list (dashboard se): aap companies ki list do -> har 30 second Designer profile ki Gmail mein
un ki email ki .docx dhoondo -> "Edit with Google Docs" se kholo -> automation khud post banaye.

Asal automation par asar NAHI: yeh sirf tab chalta hai jab list ON ho, Designer connected ho, koi file ban na
rahi ho aur run PAUSE na ho. Apni alag Gmail tab (script ke group mein). Koi bhi masla -> sirf log, server chalta rahe.
Record: Output\\gmail_watch.json"""
import asyncio
import json
import re
import time
from datetime import datetime
from urllib.parse import quote

S = None  # server module (setup() se)
STATUS = {"last_scan": None, "message": "Band hai", "busy": False}


def setup(server_module):
    global S
    S = server_module


def _path():
    return S.OUT_ROOT / "gmail_watch.json"


def load():
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.setdefault("enabled", False)
    data.setdefault("items", [])
    data.setdefault("opened", {})
    data.setdefault("failed", {})
    return data


def save(data):
    S.OUT_ROOT.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def blocked():
    """Asal automation ko na chhuo: file ban rahi ho / run pause ho / Designer na ho -> scan nahi."""
    hub = S.HUB
    if "designer" not in hub.clients:
        return "Designer profile connected nahi"
    if hub.pause_requested or hub.paused:
        return "Run PAUSE par hai — Gmail scan ruka hua"
    if hub.running and (hub.current.get("file") or not getattr(hub, "watching", False)):
        return "File ban rahi hai — us ke baad scan hoga"
    return None


# ---------------------------------------------------------------- dashboard ----

def api_state():
    data = load()
    recent = sorted(data["opened"].values(), key=lambda x: x.get("at", ""), reverse=True)[:15]
    return {"enabled": data["enabled"], "items": data["items"], "recent": recent,
            "status": STATUS, "interval": S.CFG.get("gmail_scan_seconds", 30),
            "query": S.CFG.get("gmail_query", "has:attachment filename:docx newer_than:1d")}


def api_action(do, q):
    data = load()
    name = re.sub(r"\s+", " ", q.get("name", "")).strip()
    if do == "gmail_add":
        added = []
        for n in re.split(r"[\n,;]+", name):  # ek saath kai naam (comma / nayi line)
            n = n.strip()
            if n and S.norm(n) not in {S.norm(i["name"]) for i in data["items"]}:
                data["items"].append({"name": n, "added": now(), "found": []})
                added.append(n)
        save(data)
        if added:
            S.log(f"Gmail list: {', '.join(added)} add")
        return {"ok": bool(added), "message": f"{len(added)} company list mein" if added else "Naam khaali ya pehle se list mein"}
    if do == "gmail_remove":
        data["items"] = [i for i in data["items"] if S.norm(i["name"]) != S.norm(name)]
        save(data)
        return {"ok": True, "message": f"'{name}' list se hata di"}
    if do == "gmail_clear":
        data["items"] = []
        save(data)
        return {"ok": True, "message": "List khaali"}
    if do == "gmail_toggle":
        data["enabled"] = q.get("on") == "1"
        save(data)
        STATUS["message"] = "ON — har 30 second scan" if data["enabled"] else "Band hai"
        S.log("Gmail watch list " + ("ON" if data["enabled"] else "OFF"))
        return {"ok": True, "message": "Gmail scan ON" if data["enabled"] else "Gmail scan OFF"}
    if do == "gmail_scan":
        if STATUS["busy"]:
            return {"ok": False, "message": "Scan pehle se chal raha hai"}
        asyncio.get_event_loop().create_task(scan(force=True))
        return {"ok": True, "message": "Gmail scan shuru"}
    return {"ok": False, "message": f"Unknown: {do}"}


# ---------------------------------------------------------------- scan ----

async def loop():
    while True:
        await asyncio.sleep(max(15, int(S.CFG.get("gmail_scan_seconds", 30))))
        try:
            await scan()
        except Exception as e:  # kabhi bhi server na gire
            STATUS["message"] = f"Masla: {e}"
            STATUS["busy"] = False


async def _wait_list(tab, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = await S.HUB.exec("designer", tab, "gmail_state", timeout=30) or {}
        if st.get("loggedOut") or st.get("ready"):
            return st
        await asyncio.sleep(1.5)
    return {"rows": [], "ready": False}


def _done_or_open(stem, state, open_titles):
    key = S.norm(stem)
    done = {S.norm(S.folder_of(v.get("title", ""))) for v in state.values() if v.get("status") == "done"}
    return key in done or key in {S.norm(S.folder_of(t)) for t in open_titles}


async def scan(force=False):
    data = load()
    if not data["enabled"] and not force:
        return
    items = [i["name"] for i in data["items"]]
    if not items:
        STATUS["message"] = "List khaali — company ka naam add karo"
        return
    why = blocked()
    if why:
        STATUS["message"] = why
        return
    if STATUS["busy"]:
        return
    STATUS.update(busy=True, message="Gmail scan ho raha hai…")
    opened_now = []
    try:
        base = S.CFG.get("gmail_url", "https://mail.google.com/mail/u/0/")
        query = S.CFG.get("gmail_query", "has:attachment filename:docx newer_than:1d")
        search = base + "#search/" + quote(query)
        tab = await S.HUB.call("designer", "gmail_tab", url=search, timeout=60)
        await S.HUB.call("designer", "goto_quiet", tabId=tab, url=search, timeout=60)
        st = await _wait_list(tab)
        if st.get("loggedOut"):
            STATUS["message"] = "Designer profile mein Gmail login nahi"
            return
        rows = [r for r in st.get("rows", []) if any(S.norm(n) in S.norm(r["text"]) for n in items)]
        state = S.load_state()
        for row in rows[:10]:
            if blocked():  # beech mein file shuru ho gayi -> foran ruko
                break
            match = [n for n in items if S.norm(n) in S.norm(row["text"])]
            await S.HUB.call("designer", "goto_quiet", tabId=tab, url=search, timeout=60)
            await _wait_list(tab)
            if not await S.HUB.exec("designer", tab, "gmail_open_row", row["id"], timeout=30):
                continue
            atts = []
            for _ in range(12):  # email khulne ka intezar
                await asyncio.sleep(1.5)
                atts = await S.HUB.exec("designer", tab, "gmail_attachments", timeout=30) or []
                if atts:
                    break
            docx = [a for a in atts if re.search(r"\.docx?$", a.get("name", ""), re.I)]
            named = [a for a in docx if any(S.norm(n) in S.norm(a["name"]) for n in items)]
            pick = named or (docx if len(docx) == 1 else [])
            for a in pick:
                key = f"{row['id']}|{a['name']}"
                if key in data["opened"]:
                    continue
                stem = re.sub(r"\.docx?$", "", a["name"], flags=re.I)
                before = await S.HUB.call("designer", "list_doc_tabs", timeout=20)
                if _done_or_open(stem, state, [t["title"] for t in before]):
                    data["opened"][key] = {"file": a["name"], "at": now(), "note": "pehle se khuli / ban chuki"}
                    continue
                res = await S.HUB.exec("designer", tab, "gmail_edit", a["i"], timeout=30)
                new = None
                if res == "ok":
                    urls = {t["url"].split("#")[0] for t in before}
                    for _ in range(20):  # Docs ki nayi tab ka intezar
                        await asyncio.sleep(1.5)
                        now_tabs = await S.HUB.call("designer", "list_doc_tabs", timeout=20)
                        new = next((t for t in now_tabs if t["url"].split("#")[0] not in urls), None)
                        if new:
                            break
                if new:
                    company = match[0] if match else stem
                    data["opened"][key] = {"file": a["name"], "company": company, "at": now()}
                    for it in data["items"]:
                        if S.norm(it["name"]) in S.norm(a["name"] + " " + row["text"]):
                            it.setdefault("found", []).append({"file": a["name"], "at": now()})
                    opened_now.append(a["name"])
                    S.log(f"Gmail: '{a['name']}' email se khol di (Edit with Google Docs)")
                    S.notify("Gmail se file khuli", f"{a['name']} — automation is ki posts banayegi", sticky=False)
                else:
                    data["failed"][key] = data["failed"].get(key, 0) + 1
                    S.log(f"Gmail: '{a['name']}' nahi khuli ({res}) — koshish {data['failed'][key]}/3")
                    if data["failed"][key] >= 3:
                        data["opened"][key] = {"file": a["name"], "at": now(), "note": "3 dafa nahi khuli — khud kholo"}
                        S.notify("Gmail: file nahi khuli", f"{a['name']} — Gmail se khud khol lo ({res})")
                save(data)
        await S.HUB.call("designer", "goto_quiet", tabId=tab, url=search, timeout=60)
        STATUS["message"] = (f"{len(opened_now)} file khuli: {', '.join(opened_now)}" if opened_now
                             else f"Scan ho gaya — {len(rows)} milti email, nayi file nahi")
    finally:
        STATUS["busy"] = False
        STATUS["last_scan"] = now()
        save(data)
    if opened_now and S.CFG.get("gmail_autostart", True):
        if not S.HUB.running:
            S.log("Gmail se nayi file — automation khud START")
            S.start_run(None, None)
        # chalte run (watch mode) mein nayi tab khud line mein aa jati hai
