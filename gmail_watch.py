"""Gmail watch list (dashboard se): aap companies ki list do -> Designer profile ki Gmail mein
un ki email ki .docx dhoondo -> "Edit with Google Docs" se kholo -> automation khud post banaye.

Chalta hai jab list ON ho, Designer connected ho, aur run PAUSE na ho. File ban rahi ho to bhi scan
chal sakta hai (ChatGPT generate wait / files ke beech / idle). Thori der rukta hai jab: Designer
docs/images parh raha ho, YA ChatGPT se banai image download ho rahi ho. Apni alag Gmail tab.
Koi bhi masla -> sirf log, server chalta rahe. Record: Output\\gmail_watch.json"""
import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote

S = None  # server module (setup() se)
STATUS = {"last_scan": None, "message": "Band hai", "busy": False}
DEFAULT_QUERY = ""  # Gmail mein sirf company ka naam search (config gmail_query se aur filter laga sakte ho)
MAX_DAYS = 5       # is se purani email ki file kabhi nahi kholni (config gmail_max_days)
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
    """Scan rukhe: Designer na ho, run PAUSE, ya image/doc download chal raha ho."""
    hub = S.HUB
    if "designer" not in hub.clients:
        return "Designer profile connected nahi"
    if hub.pause_requested or hub.paused:
        return "Run PAUSE par hai — Gmail scan ruka hua"
    # ChatGPT generate wait mein scan chale; download / Designer docs ke dauran wait
    if hub.running and getattr(hub, "designer_busy", False):
        return "Image / docs download chal raha hai — thori der mein scan"
    return None


# ---------------------------------------------------------------- dashboard ----

def today():
    return datetime.now().strftime("%Y-%m-%d")


def per_day():
    """Ek company ki ek din mein zyada se zyada itni files (baqi agle din) — config gmail_max_per_day."""
    return max(1, int(S.CFG.get("gmail_max_per_day", 2)))


def _cutoff():
    """Had pichle 24 GHANTE par (12 baje date badalne se dobara 2 nahi khulti — HR Strategy 10 wala masla)."""
    return (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")


def found_today(item):
    """Pichle 24 ghante mein Gmail se is company ki khuli files (dashboard 'x/2')."""
    cut = _cutoff()
    return [f for f in item.get("found") or [] if f.get("at", "") >= cut]


def recent_files(item, state=None):
    """Pichle 24 ghante mein is company ki files — Gmail se khuli YA kisi bhi tarah khul kar bani (state).
    Har file ek dafa gini jati hai (doc id / naam se)."""
    cut = _cutoff()
    ids = set()
    for f in found_today(item):
        ids.add(f.get("doc") or (f["file"] if re.fullmatch(r"[\w-]{25,}", f.get("file", "")) else S.norm(f.get("file", ""))))
    key = S.norm(item["name"])
    for doc_id, v in (state or {}).items():
        at = str(v.get("at", "")).replace("T", " ")
        title = S.norm(S.folder_of(v.get("title", "")))
        if v.get("status") == "done" and at >= cut and key and (key in title or key.replace(" ", "") in title.replace(" ", "")):
            ids.add(doc_id)
    return ids


def left_today(item, state=None):
    """Is company ki aur kitni files khul sakti hain (24 ghante mein max 2)."""
    if state is None:
        try:
            state = S.load_state()
        except Exception:
            state = {}
    return per_day() - len(recent_files(item, state))


def split_names(text):
    """Spreadsheet / list ka paste: har line / tab / comma / ; ek naam. Numbering ("1." "2)") aur bullets hatao."""
    out = []
    for n in re.split(r"[\r\n\t,;]+", text or ""):
        n = re.sub(r"^\s*(?:\d+\s*[.)\-:]\s*|[•\-*]\s*)", "", n)
        n = re.sub(r"\s+", " ", n).strip()
        if n and S.norm(n) and S.norm(n) not in {S.norm(x) for x in out}:
            out.append(n)
    return out


def name_parts(name):
    """Exact naam na mile to hisse: 'Belle Clinic London UK' -> poora, 'Belle Clinic London', 'Belle Clinic', 'Belle'
    + bina space ('E liquid' -> 'Eliquid'). Aam lafz ('the', 'copy', 'post'...) akele kabhi search nahi."""
    common = getattr(S, "STOPWORDS", set()) | getattr(S, "NOISE_WORDS", set())
    words = name.split()
    out = [name]
    if len(words) > 1:
        out.append("".join(words))
    # shuru ke aam lafz hata kar ("The Molen tax" -> "Molen tax", "Copy of Sweet..." -> "Sweet ...")
    core = list(words)
    while core and S.norm(core[0]) in common:
        core.pop(0)
    for ws in (words, core):
        out += [" ".join(ws[:k]) for k in range(len(ws), 0, -1)]
    seen, parts = set(), []
    for t in out:
        n = S.norm(t)
        meaningful = [w for w in n.split() if w not in common]
        # akela lafz ("Single", "Expert", "International") aam English — ghalat company ki email khulti thi.
        # Isliye hisse kam se kam 2 lafz; sirf tab akela jab company ka naam hi ek lafz ho.
        single = len(n.split()) == 1 and t in words and len(words) > 1
        if not n or n in seen or not meaningful or single or (len(n.split()) == 1 and (len(n) < 4 or n in common)):
            continue
        seen.add(n)
        parts.append(t)
    return parts


def api_state():
    data = load()
    recent = sorted(data["opened"].values(), key=lambda x: x.get("at", ""), reverse=True)[:15]
    try:
        state = S.load_state()
    except Exception:
        state = {}
    items = [dict(i, today=found_today(i), used=len(recent_files(i, state)), limit=per_day()) for i in data["items"]]
    return {"enabled": data["enabled"], "items": items, "recent": recent,
            "status": STATUS, "interval": S.CFG.get("gmail_scan_seconds", 300),
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
        mins = max(1, int(S.CFG.get("gmail_scan_seconds", 300)) // 60)
        STATUS["message"] = f"ON — har {mins} minute Gmail refresh + search" if data["enabled"] else "Band hai"
        S.log("Gmail watch list " + ("ON" if data["enabled"] else "OFF"))
        if data["enabled"] and not STATUS["busy"]:  # ON karte hi pehla scan foran (5 minute ka intezar nahi)
            asyncio.get_event_loop().create_task(safe_scan())
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
        await asyncio.sleep(max(60, int(S.CFG.get("gmail_scan_seconds", 300))))
        await safe_scan()


async def _wait_list(tab, url=None, timeout=30):
    """Search ke NATIJE ka intezar: URL wahi search ho, rows mein text ho, aur 2 dafa ek jaisa parha jaye
    (warna pichle page / inbox ki purani rows se ghalat email khul sakti hai)."""
    from urllib.parse import unquote_plus
    want = unquote_plus(url.split("#", 1)[1]).lower() if url and "#" in url else None
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        st = await S.HUB.exec("designer", tab, "gmail_state", timeout=30) or {}
        if st.get("loggedOut"):
            return st
        here = unquote_plus((st.get("hash") or "").lstrip("#")).lower()
        same_page = want is None or here == want
        rows = st.get("rows") or []
        ready = st.get("empty") or (rows and all(r.get("text") for r in rows))
        sig = (here, tuple(r["id"] for r in rows), bool(st.get("empty")))
        if same_page and ready and sig == last:
            return st
        last = sig if same_page and ready else None
        await asyncio.sleep(1.5)
    return {"rows": [], "ready": False}


def _done_or_open(stem, state, open_titles):
    """Pehle se khuli tab / pehle ban chuki file -> wajah; warna None."""
    key = S.norm(stem)
    if key in {S.norm(S.folder_of(t)) for t in open_titles}:
        return "pehle se khuli hai"
    # naam se "ban chuki" sirf ISI MAHINE ki (agle mahine wahi number — Wear Wood 8 — nayi file hoti hai).
    # Doc id wala check (link / khuli tab) har waqt — same doc = same file.
    month = datetime.now().strftime("%Y-%m")
    done = {S.norm(S.folder_of(v.get("title", ""))) for v in state.values()
            if v.get("status") == "done" and str(v.get("at", "")).startswith(month)}
    if key in done:
        return "is mahine pehle ban chuki"
    return None


def _doc_id(url):
    m = re.search(r"/document/d/([\w-]+)", url or "")
    return m.group(1) if m else None


async def _wait_new_doc(before, gmail_tab=None):
    urls = {t["url"].split("#")[0].split("?")[0] for t in before}
    for _ in range(30):  # Docs ki nayi tab ka intezar (45 second — jaldi email band nahi karni)
        await asyncio.sleep(1.5)
        if gmail_tab is not None:  # "Continue editing? ... previously added to Drive" -> "Edit latest"
            try:
                if await S.HUB.exec("designer", gmail_tab, "gmail_continue_dialog", timeout=20):
                    S.log("  Gmail: 'Continue editing?' -> Edit latest")
            except Exception:
                pass
        tabs = await S.HUB.call("designer", "list_doc_tabs", timeout=20)
        new = next((t for t in tabs if t["url"].split("#")[0].split("?")[0] not in urls), None)
        # tab tab hi "khuli" jab URL mein doc id aa jaye aur title load ho (adhoori tab se run crash hota tha)
        if new and re.search(r"/document/d/[\w-]+", new["url"]) and new.get("title") and new["title"] != "Google Docs":
            return new
    return None


async def _already_made(new, state):
    """Nayi khuli tab ki file pehle ban chuki (state mein DONE)? -> tab band kar do."""
    did = _doc_id(new.get("url"))
    if did and state.get(did, {}).get("status") == "done":
        try:
            await S.HUB.call("designer", "close_tab", tabId=new["tabId"], timeout=20)
        except Exception:
            pass
        return True
    return False


def _mark(item, data, key, name, note=None, doc=None):
    rec = {"file": name, "company": item["name"], "at": now()}
    if doc:
        rec["doc"] = doc  # doc id -> 24 ghante ki ginti mein ek file do dafa na gine
    if note:
        rec["note"] = note
    data["opened"][key] = rec
    if note in (None, "pehle se khuli hai"):  # "pehle ban chuki" = purani file -> aaj ki nayi ka intezar jari
        item.setdefault("found", []).append({k: v for k, v in rec.items() if k != "company"})


async def _open_link(link, row, item, data, state, opened_now):
    """Email ke andar Drive / Docs link ('Wear Wood 9' chip) -> Google Docs mein kholo (same file id)."""
    key = f"{row['id']}|{link['id']}"
    if key in data["opened"] or blocked() or left_today(item) <= 0:  # aaj ki had (2) puri -> baqi kal
        return
    name = link.get("name") or link["id"]
    before = await S.HUB.call("designer", "list_doc_tabs", timeout=20)
    if any(link["id"] in t["url"] for t in before):
        return _mark(item, data, key, name, "pehle se khuli hai")
    why = _done_or_open(name, state, [t["title"] for t in before])
    if not why and state.get(link["id"], {}).get("status") == "done":  # link ka naam na ho -> doc id se
        why = "pehle ban chuki"
    if why:
        return _mark(item, data, key, name, why)
    await S.HUB.call("designer", "open_tab", url=f"https://docs.google.com/document/d/{link['id']}/edit", timeout=60)
    new = await _wait_new_doc(before)
    if new and await _already_made(new, state):
        return _mark(item, data, key, new.get("title") or name, "pehle ban chuki")
    if new:
        _mark(item, data, key, new.get("title") or name, doc=link["id"])
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


async def _open_thread_files(tab, row, item, data, state, opened_now, term=None):
    """Ek email kholo -> us ki .docx (company naam wali; na ho to saari) 'Edit with Google Docs' se kholo."""
    if not await S.HUB.exec("designer", tab, "gmail_open_row", row["id"], timeout=30):
        return
    atts = []
    for _ in range(14):  # email khulne ka intezar (attachment ya Drive link) — ~20 second
        await asyncio.sleep(1.5)
        atts = await S.HUB.exec("designer", tab, "gmail_attachments", timeout=30) or []
        if atts or await S.HUB.exec("designer", tab, "gmail_doc_links", timeout=30):
            break
    links = await S.HUB.exec("designer", tab, "gmail_doc_links", timeout=30) or []
    key_name = S.norm(term or item["name"])  # jis naam / hisse se email mili
    exact = not term or S.norm(term) == S.norm(item["name"])  # hisse se mili -> sirf naam wali file (ghalat email na khule)
    for l in [l for l in links if key_name in S.norm(l.get("name", ""))] or (links if exact else []):
        await _open_link(l, row, item, data, state, opened_now)
    docx = [a for a in atts if re.search(r"\.docx?$", a.get("name", ""), re.I)]
    pick = [a for a in docx if key_name in S.norm(a["name"])] or (docx if exact else [])
    for a in pick:
        if blocked() or left_today(item) <= 0:  # aaj ki had (2) puri -> baqi kal
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
        # "Edit with Google Docs" kabhi foran, kabhi 5-10 second baad aata hai -> 25 second tak sabr
        res = None
        for _ in range(17):
            res = await S.HUB.exec("designer", tab, "gmail_edit", a["name"], timeout=30)
            if res == "ok":
                break
            await asyncio.sleep(1.5)
        new = await _wait_new_doc(before, tab) if res == "ok" else None
        if new and await _already_made(new, state):
            _mark(item, data, key, a["name"], "pehle ban chuki")
            save(data)
            continue
        if new:
            _mark(item, data, key, a["name"], doc=_doc_id(new.get("url")))
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
    pending = [i for i in data["items"] if left_today(i) > 0]  # aaj 2 se kam khuli -> abhi dhoondo
    if not data["items"]:
        STATUS["message"] = "List khaali — company ka naam add karo"
        return
    if not pending:
        STATUS["message"] = f"Aaj har company ki had puri ({per_day()} file) — baqi files kal"
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
        # har scan se pehle Gmail poora REFRESH (nayi emails zaroor nazar aayen) — ?r= se page dobara load hota hai
        inbox = base + f"?r={int(time.time())}#inbox"
        tab = await S.HUB.call("designer", "gmail_tab", url=inbox, timeout=60)
        await S.HUB.call("designer", "goto_quiet", tabId=tab, url=inbox, timeout=90)
        await asyncio.sleep(3)
        for item in pending:
            if blocked():  # beech mein file shuru ho gayi -> foran ruko
                break
            # pehle poora naam; email na mile to naam ke hisse (Belle Clinic London UK -> Belle Clinic -> Belle)
            rows, term = [], item["name"]
            for term in name_parts(item["name"]):
                days = int(S.CFG.get("gmail_max_days", MAX_DAYS))  # 5 din se purani email kabhi nahi
                search = base + "#search/" + quote(f'{query} "{term}" newer_than:{days}d'.strip())
                if tab is None:
                    tab = await S.HUB.call("designer", "gmail_tab", url=search, timeout=60)
                await S.HUB.call("designer", "goto_quiet", tabId=tab, url=search, timeout=60)
                st = await _wait_list(tab, search)
                if st.get("loggedOut"):
                    STATUS["message"] = "Designer profile mein Gmail login nahi"
                    return
                rows = st.get("rows", [])
                if rows:
                    if term != item["name"]:
                        S.log(f"Gmail: '{item['name']}' poora naam nahi mila — '{term}' se email mili")
                    break
            # sirf UNREAD emails — read wali aap dekh chuke (purani bani files dobara na banen)
            rows = [r for r in rows if r.get("unread")]
            rows = list(reversed(rows[:10]))  # kai files hon to PURANI pehle (Gmail list mein neeche wali), phir nayi
            for row in rows:
                if blocked() or left_today(item) <= 0:  # purani pehle; aaj 2 ho gayin to baqi kal
                    break
                await S.HUB.call("designer", "goto_quiet", tabId=tab, url=search, timeout=60)
                await _wait_list(tab, search)
                await _open_thread_files(tab, row, item, data, state, opened_now, term)
                # file khul gayi / pehle se tab mein khuli -> email READ hi rahe.
                # File na khul saki aur email pehle unread thi -> wapis UNREAD (aap pending files isi se dekhte ho)
                got = any(k.startswith(row["id"] + "|") and v.get("note") in (None, "pehle se khuli hai")
                          for k, v in data["opened"].items())
                if row.get("unread") and not got:
                    try:
                        await S.HUB.call("designer", "goto_quiet", tabId=tab, url=search, timeout=60)
                        await _wait_list(tab, search)
                        res = None
                        for _ in range(2):  # ek dafa aur koshish
                            res = await S.HUB.exec("designer", tab, "gmail_mark_unread", row["id"], timeout=40)
                            if res == "ok":
                                break
                            await _wait_list(tab, search)
                        if res != "ok":
                            S.log(f"Gmail: email wapis unread nahi hui ({res}) — {row['text'][:80]}")
                            S.notify("Gmail: email unread nahi hui", f"{row['text'][:120]} — Gmail mein khud unread kar dena")
                    except Exception as e:
                        S.log(f"Gmail: email wapis unread nahi hui ({e})")
            save(data)
        if tab is not None:
            await S.HUB.call("designer", "goto_quiet", tabId=tab, url=base + "#inbox", timeout=60)
        left = [i["name"] for i in data["items"] if not found_today(i)]  # aaj ek bhi nahi mili
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
