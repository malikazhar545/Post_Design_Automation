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
DEFAULT_QUERY = ""  # Gmail mein sirf company ka naam search (config gmail_query se aur filter laga sakte ho)
TODAY_RE = re.compile(r"\b\d{1,2}:\d{2}(\s?[AaPp][Mm])?\s*$")  # list mein aaj ki email ka waqt (purani par tareekh)


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

def today():
    return datetime.now().strftime("%Y-%m-%d")


def found_today(item):
    return [f for f in item.get("found") or [] if f.get("at", "").startswith(today())]


def split_names(text):
    """Spreadsheet / list ka paste: har line / tab / comma / ; ek naam. Numbering ("1." "2)") aur bullets hatao."""
    out = []
    for n in re.split(r"[\r\n\t,;]+", text or ""):
        n = re.sub(r"^\s*(?:\d+\s*[.)\-:]\s*|[•\-*]\s*)", "", n)
        n = re.sub(r"\s+", " ", n).strip()
        if n and S.norm(n) and S.norm(n) not in {S.norm(x) for x in out}:
            out.append(n)
    return out


def api_state():
    data = load()
    recent = sorted(data["opened"].values(), key=lambda x: x.get("at", ""), reverse=True)[:15]
    items = [dict(i, today=found_today(i)) for i in data["items"]]
    return {"enabled": data["enabled"], "items": items, "recent": recent,
            "status": STATUS, "interval": S.CFG.get("gmail_scan_seconds", 30),
            "query": S.CFG.get("gmail_query", DEFAULT_QUERY)}


def api_action(do, q):
    data = load()
    name = re.sub(r"\s+", " ", q.get("name", "")).strip()
    if do == "gmail_add":
        added = []
        for n in split_names(q.get("name", "")):  # poori list ek saath (spreadsheet copy)
            if S.norm(n) not in {S.norm(i["name"]) for i in data["items"]}:
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
        asyncio.get_event_loop().create_task(safe_scan(force=True))
        return {"ok": True, "message": "Gmail scan shuru"}
    return {"ok": False, "message": f"Unknown: {do}"}


# ---------------------------------------------------------------- scan ----

async def safe_scan(force=False):
    """Scan ka koi bhi masla: sirf log + card par dikhao — server / automation par asar nahi."""
    try:
        await scan(force=force)
    except Exception as e:
        STATUS.update(busy=False, message=f"Masla: {e}", last_scan=now())
        S.log(f"Gmail scan masla: {e}")


async def loop():
    while True:
        await asyncio.sleep(max(15, int(S.CFG.get("gmail_scan_seconds", 30))))
        await safe_scan()


async def _wait_list(tab, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = await S.HUB.exec("designer", tab, "gmail_state", timeout=30) or {}
        if st.get("loggedOut") or st.get("ready"):
            return st
        await asyncio.sleep(1.5)
    return {"rows": [], "ready": False}


def _done_or_open(stem, state, open_titles):
    """Pehle se khuli tab / pehle ban chuki file -> wajah; warna None."""
    key = S.norm(stem)
    if key in {S.norm(S.folder_of(t)) for t in open_titles}:
        return "pehle se khuli hai"
    if key in {S.norm(S.folder_of(v.get("title", ""))) for v in state.values() if v.get("status") == "done"}:
        return "pehle ban chuki"
    return None


async def _wait_new_doc(before):
    urls = {t["url"].split("#")[0].split("?")[0] for t in before}
    for _ in range(20):  # Docs ki nayi tab ka intezar
        await asyncio.sleep(1.5)
        tabs = await S.HUB.call("designer", "list_doc_tabs", timeout=20)
        new = next((t for t in tabs if t["url"].split("#")[0].split("?")[0] not in urls), None)
        if new:
            return new
    return None


def _mark(item, data, key, name, note=None):
    rec = {"file": name, "company": item["name"], "at": now()}
    if note:
        rec["note"] = note
    data["opened"][key] = rec
    if note in (None, "pehle se khuli hai"):  # "pehle ban chuki" = purani file -> aaj ki nayi ka intezar jari
        item.setdefault("found", []).append({k: v for k, v in rec.items() if k != "company"})


async def _open_link(link, row, item, data, state, opened_now):
    """Email ke andar Drive / Docs link ('Wear Wood 9' chip) -> Google Docs mein kholo (same file id)."""
    key = f"{row['id']}|{link['id']}"
    if key in data["opened"] or blocked():
        return
    name = link.get("name") or link["id"]
    before = await S.HUB.call("designer", "list_doc_tabs", timeout=20)
    if any(link["id"] in t["url"] for t in before):
        return _mark(item, data, key, name, "pehle se khuli hai")
    why = _done_or_open(name, state, [t["title"] for t in before])
    if why:
        return _mark(item, data, key, name, why)
    await S.HUB.call("designer", "open_tab", url=f"https://docs.google.com/document/d/{link['id']}/edit", timeout=60)
    new = await _wait_new_doc(before)
    if new:
        _mark(item, data, key, name)
        opened_now.append(name)
        S.log(f"Gmail: '{name}' ({item['name']}) email ke Drive link se khol di")
        S.notify("Gmail se file khuli", f"{name} — automation is ki posts banayegi", sticky=False)
    else:
        data["failed"][key] = data["failed"].get(key, 0) + 1
        S.log(f"Gmail: '{name}' link nahi khula — koshish {data['failed'][key]}/3")
        if data["failed"][key] >= 3:
            _mark(item, data, key, name, "3 dafa nahi khuli — khud kholo")
            S.notify("Gmail: file nahi khuli", f"{name} — Gmail se khud khol lo")
    save(data)


async def _open_thread_files(tab, row, item, data, state, opened_now):
    """Ek email kholo -> us ki .docx (company naam wali; na ho to saari) 'Edit with Google Docs' se kholo."""
    if not await S.HUB.exec("designer", tab, "gmail_open_row", row["id"], timeout=30):
        return
    atts = []
    for _ in range(8):  # email khulne ka intezar (attachment ya Drive link)
        await asyncio.sleep(1.5)
        atts = await S.HUB.exec("designer", tab, "gmail_attachments", timeout=30) or []
        if atts or await S.HUB.exec("designer", tab, "gmail_doc_links", timeout=30):
            break
    links = await S.HUB.exec("designer", tab, "gmail_doc_links", timeout=30) or []
    for l in [l for l in links if S.norm(item["name"]) in S.norm(l.get("name", ""))] or links:
        await _open_link(l, row, item, data, state, opened_now)
    docx = [a for a in atts if re.search(r"\.docx?$", a.get("name", ""), re.I)]
    pick = [a for a in docx if S.norm(item["name"]) in S.norm(a["name"])] or docx
    for a in pick:
        if blocked():
            return
        key = f"{row['id']}|{a['name']}"
        if key in data["opened"]:
            continue
        stem = re.sub(r"\.docx?$", "", a["name"], flags=re.I)
        before = await S.HUB.call("designer", "list_doc_tabs", timeout=20)
        why = _done_or_open(stem, state, [t["title"] for t in before])
        if why:  # pehle se khuli / ban chuki -> dobara nahi kholni
            _mark(item, data, key, a["name"], why)
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
            data["opened"][key] = {"file": a["name"], "company": item["name"], "at": now()}
            item.setdefault("found", []).append({"file": a["name"], "at": now()})
            opened_now.append(a["name"])
            S.log(f"Gmail: '{a['name']}' ({item['name']}) email se khol di — Edit with Google Docs")
            S.notify("Gmail se file khuli", f"{a['name']} — automation is ki posts banayegi", sticky=False)
        else:
            data["failed"][key] = data["failed"].get(key, 0) + 1
            S.log(f"Gmail: '{a['name']}' nahi khuli ({res}) — koshish {data['failed'][key]}/3")
            if data["failed"][key] >= 3:
                data["opened"][key] = {"file": a["name"], "company": item["name"], "at": now(),
                                       "note": "3 dafa nahi khuli — khud kholo"}
                S.notify("Gmail: file nahi khuli", f"{a['name']} — Gmail se khud khol lo ({res})")
        save(data)


async def scan(force=False):
    """Har company (jis ki aaj ki file abhi nahi mili) ki ALAG Gmail search — pehle se aayi email bhi."""
    data = load()
    if not data["enabled"] and not force:
        return
    pending = [i for i in data["items"] if not found_today(i)]
    if not data["items"]:
        STATUS["message"] = "List khaali — company ka naam add karo"
        return
    if not pending:
        STATUS["message"] = "Aaj list ki saari companies ki file mil gayi"
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
        query = S.CFG.get("gmail_query", DEFAULT_QUERY)
        state = S.load_state()
        tab = None
        for item in pending:
            if blocked():  # beech mein file shuru ho gayi -> foran ruko
                break
            search = base + "#search/" + quote(f'{query} "{item["name"]}"'.strip())
            if tab is None:
                tab = await S.HUB.call("designer", "gmail_tab", url=search, timeout=60)
            await S.HUB.call("designer", "goto_quiet", tabId=tab, url=search, timeout=60)
            st = await _wait_list(tab)
            if st.get("loggedOut"):
                STATUS["message"] = "Designer profile mein Gmail login nahi"
                return
            rows = st.get("rows", [])
            rows = rows[:1] + [r for r in rows[1:6] if TODAY_RE.search(r["text"])]  # nayi + aaj ki
            for row in rows:
                if blocked():
                    break
                await S.HUB.call("designer", "goto_quiet", tabId=tab, url=search, timeout=60)
                await _wait_list(tab)
                await _open_thread_files(tab, row, item, data, state, opened_now)
            save(data)
        if tab is not None:
            await S.HUB.call("designer", "goto_quiet", tabId=tab, url=base + "#inbox", timeout=60)
        left = [i["name"] for i in data["items"] if not found_today(i)]
        STATUS["message"] = ((f"{len(opened_now)} file khuli: {', '.join(opened_now)}. " if opened_now else "")
                             + (f"Intezar: {', '.join(left)}" if left else "Aaj list ki saari files mil gayin"))
    finally:
        STATUS["busy"] = False
        STATUS["last_scan"] = now()
        save(data)
    if opened_now and S.CFG.get("gmail_autostart", True):
        if not S.HUB.running:
            S.log("Gmail se nayi file — automation khud START")
            S.start_run(None, None)
        # chalte run (watch mode) mein nayi tab khud line mein aa jati hai
