"""
Post Design Automation — server
-------------------------------
4 Chrome extensions (Designer / Waseem / Wajid / Instagram profiles) is server se judti hain.
Designer extension ka START dabao -> yeh server poora kaam extensions se karwata hai:

 1. Designer profile mein khuli Google Docs tabs, EK WAQT MEIN EK file.
 2. File ke naam se company (date / .docx hata kar).
 3. Waseem ChatGPT mein company chat (case-insensitive). Na mile to Wajid se
    "give me whole instructions..." -> Waseem new chat mein paste + save, chat ka naam = company.
 4. Doc se sirf HEADING content + image link (REEL + CAPTION skip).
    Images designer profile se (Freepik bhi). Instagram link -> Instagram profile mein khol kar
    har slide ki image ka screenshot (caption nahi), last slide CTA ho to skip.
 5. Waseem chat mein sab ek message mein: posts 4:5 (LinkedIn bhi), story 9:16, branding rules.
 6. Images banne tak wait -> Output folder mein download -> phir agli file.
"""

import asyncio
import base64
import io
import itertools
import json
import mimetypes
import re
import shutil
import time
import traceback
from datetime import datetime, date as _date, timedelta as _timedelta
from pathlib import Path
from urllib.parse import urlparse, quote
from html.parser import HTMLParser
import sys
from website_branding import collect_website_branding, WebsiteBrandingUnavailable

# Windows console mein emoji / ✔ print karne se crash na ho
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from websockets.asyncio.server import serve
from collections import deque
import dashboard_api
import company_references
import gmail_watch

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
SEL = CFG["selectors"]
OUT_ROOT = BASE / CFG["output_dir"]
STATE_FILE = OUT_ROOT / "state.json"
CHATGPT = "https://chatgpt.com"
PORT = int(__import__("os").environ.get("POST_AUTOMATION_PORT", "8765"))
ROLES = ("designer", "waseem", "wajid")            # zaroori
OPTIONAL_ROLES = ("instagram",)                  # sirf jab link ho (Instagram + Envato screenshot); Envato extension band


# ------------------------------------------------------------- hub / RPC ----

PART_RE = re.compile(r"\s*\((\d+(?:-\d+)?)\)\s*$")  # "dental care 10 posts 3 (4-6)" -> bari file ka hissa


class DocSplit(Exception):
    """Bari file nayi Google Docs (hisson) mein baant di: asal file khud nahi banti, hisse alag files ban kar."""
    def __init__(self, parts):
        super().__init__(f"{len(parts)} hisson mein baant di")
        self.parts = parts


class DayLimit(Exception):
    """Company ki 24 ghante ki had (6 posts — reels bhi ginti mein) puri: yeh set / file agle din."""
    def __init__(self, company, need):
        super().__init__(f"{company}: aaj ki had puri")
        self.company, self.need = company, need


class FileSkipped(BaseException):
    """Dashboard se 'Skip' — chalti file agle safe point (STOP wali jagah) par chhod kar agli file.
    BaseException: beech ke 'except Exception' isay nigal na saken, seedha run_all tak jaye."""


class Hub:
    def __init__(self):
        self.clients = {}
        self.pending = {}
        self.ids = itertools.count(1)
        self.running = False
        self.run_task = None
        self.pause_requested = False
        self.paused = False
        self.paused_seconds = 0.0
        self.pause_started = None
        self.pause_signal = asyncio.Event()
        self.resume_signal = asyncio.Event()
        self.resume_signal.set()
        # dashboard ke liye: live log, alerts, abhi kaun si file
        self.log_lines = deque(maxlen=1500)
        self.log_seq = 0
        self.alerts = deque(maxlen=300)
        self.alert_seq = 0
        self.current = {}
        self.started_at = time.time()
        self.skip = set()          # norm(folder) — dashboard se skip ki hui files
        self.skip_raised = None    # chalti file ke liye skip ek hi dafa raise
        self.queue_view = []       # dashboard: line mein agli files
        self.watching = False      # run nayi files ke intezar mein (Gmail scan isi waqt ho sakta hai)
        self.designer_busy = False # docs/images download (Designer ya ChatGPT) -> Gmail scan ruke
        self.rest_until = 0.0      # ChatGPT aaram (dashboard par countdown)
        self.open_docs = []        # Designer ki khuli Google Docs (har 20s) — dashboard line + Gmail ginti
        self.waiting_notes = {}    # norm(folder) -> kyun ruki (bari file ka set / company ki din ki limit)
        self.stuck_events = 0      # is file mein ChatGPT kitni dafa atka (refresh / Try again / wahi image dobara)
        self.waiting_limit_only = True  # sirf 12 ghante ki limit wali ruki hain (yeh raat ko intezar nahi karwati)
        self.no_new_files = False  # raat ka waqt (night_stop_at) guzar gaya -> nayi files line mein nahi
        self.rest_reason = ""

    def run_status(self):
        return {"type": "busy", "busy": self.running, "paused": self.paused,
                "stopping": self.pause_requested and not self.paused, "can_stop": True,
                "connected": sorted(self.clients),
                "rest_left": max(0, int(self.rest_until - time.time())), "rest_reason": self.rest_reason}

    async def publish_status(self):
        await self.send("designer", self.run_status())

    async def request_pause(self):
        if self.running and not self.pause_requested:
            self.pause_requested = True
            self.resume_signal.clear()
            self.pause_signal.set()
            log("STOP: nayi requests rok raha hoon — current browser command khatam hone ka intezar.")
            set_run_marker(paused=True)
        await self.publish_status()

    async def resume(self):
        if self.running and self.pause_requested:
            if self.pause_started is not None:
                self.paused_seconds += time.time() - self.pause_started
                self.pause_started = None
            self.pause_requested = self.paused = False
            self.pause_signal.clear()
            self.resume_signal.set()
            log("RESUME: wahi pending kaam aage chal raha hai.")
            set_run_marker(paused=False)
        await self.publish_status()

    async def checkpoint(self):
        # Only the automation worker pauses. WebSocket replies, controls and debug
        # requests must remain available, including when the Internet is offline.
        if asyncio.current_task() is not self.run_task:
            return
        while self.pause_requested:
            if not self.paused:
                self.paused = True
                self.pause_started = time.time()
                log("RUK GAYA. Ab net band kar sakte hain. Server khula rakhein; START se wahi kaam resume hoga.")
                await self.publish_status()
            await self.resume_signal.wait()
        cur = self.current.get("file")
        if cur and norm(cur) in self.skip and self.skip_raised != cur:
            self.skip_raised = cur
            raise FileSkipped(cur)

    async def send(self, role, msg):
        ws = self.clients.get(role)
        if ws:
            try:
                await ws.send(json.dumps(msg))
            except Exception:
                pass

    async def call(self, role, cmd, timeout=600, **args):
        await self.checkpoint()
        ws = self.clients.get(role)
        if not ws:
            raise RuntimeError(f"'{role.upper()}' profile ki extension connected nahi — woh Chrome profile kholo")
        mid = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        await ws.send(json.dumps({"type": "cmd", "id": mid, "cmd": cmd, "args": args}))
        try:
            res = await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(mid, None)
        await self.checkpoint()
        if not res.get("ok"):
            raise RuntimeError(f"[{role}:{cmd}] {res.get('error')}")
        return res.get("result")

    async def exec(self, role, tab_id, fn, *args, timeout=600):
        return await self.call(role, "exec", timeout=timeout, tabId=tab_id, fn=fn, args=list(args))


HUB = Hub()


def run_time():
    """Elapsed-work clock; a commute must not exhaust generation/QA deadlines."""
    now = time.time()
    if asyncio.current_task() is getattr(HUB, "run_task", None):
        return now - HUB.paused_seconds - (now - HUB.pause_started if HUB.pause_started is not None else 0)
    return now


async def chatgpt_rest(minutes, why):
    """ChatGPT ko aaram (quality / 'too many requests' se bachao). Dashboard par countdown dikhta hai."""
    if minutes <= 0:
        return
    log(f"  ChatGPT aaram: {minutes:g} minute — {why}")
    HUB.rest_until, HUB.rest_reason = time.time() + minutes * 60, why
    try:
        await run_sleep(minutes * 60)
    finally:
        HUB.rest_until, HUB.rest_reason = 0.0, ""


async def run_sleep(seconds):
    """Pause polling/retry/idle waits promptly without losing the worker's state."""
    if asyncio.current_task() is not getattr(HUB, "run_task", None):
        return await asyncio.sleep(seconds)
    await HUB.checkpoint()
    sleeper = asyncio.create_task(asyncio.sleep(seconds))
    pause = asyncio.create_task(HUB.pause_signal.wait())
    try:
        await asyncio.wait({sleeper, pause}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (sleeper, pause):
            if not task.done():
                task.cancel()
        await asyncio.gather(sleeper, pause, return_exceptions=True)
    await HUB.checkpoint()


async def internet_ok():
    """Internet hai? (chatgpt.com / google tak connection)"""
    for host in ("chatgpt.com", "www.google.com"):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, 443), 8)
            w.close()
            return True
        except Exception:
            pass
    return False


async def wait_for_internet():
    """Net band ho to yahin ruko; net aate hi aage (STOP/RESUME ka bhi khayal)."""
    if await internet_ok():
        return
    log("Internet band hai — script ruki hui hai, net aate hi wahin se chalegi.")
    notify("Internet band", "Script ruki hui hai. Net wapas aate hi khud wahin se chalegi.", sticky=False)
    while not await internet_ok():
        await run_sleep(15)
    log("Internet wapas aa gaya — kaam wahin se jaari.")
    await run_sleep(10)  # Chrome tabs ko reconnect hone do


def reload_config():
    """config.json har file se pehle dobara parho — rules badlo to server restart ki zaroorat nahi."""
    try:
        fresh = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"(config.json parhne mein ghalti, purani settings chal rahi hain: {e})")
        return
    CFG.clear()
    CFG.update(fresh)
    SEL.clear()
    SEL.update(CFG["selectors"])
CHAT_MEMORY_FILE = BASE / CFG.get("output_dir", "Output") / "chat_memory.json"
try:  # role -> {company: {"id", "title"}} — ek dafa mili/bani chat dobara dhoondni nahi padti
    CHAT_MEMORY = json.loads(CHAT_MEMORY_FILE.read_text(encoding="utf-8"))
except Exception:
    CHAT_MEMORY = {}


CHAT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def valid_chat_id(cid):
    """ChatGPT conversation id UUID hona chahiye — 'same' / short junk se /c/same broken page ban jati hai."""
    return bool(cid and CHAT_ID_RE.match(str(cid).strip()))


def remember_chat(role, company, cid, title, refs=None):
    """Company ki chat yaad rakho (+ kaun se style references is chat mein ja chuke — dobara upload na hon)."""
    if not valid_chat_id(cid):
        log(f"  (chat id reject — ghalat: {cid!r})")
        return
    old = CHAT_MEMORY.setdefault(role, {}).get(norm(company)) or {}
    keep = old.get("refs", []) if old.get("id") == cid else []
    CHAT_MEMORY[role][norm(company)] = {"id": cid, "title": title, "refs": sorted(set(keep) | set(refs or []))}
    try:
        CHAT_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHAT_MEMORY_FILE.write_text(json.dumps(CHAT_MEMORY, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


CHAT_LIST_CACHE = {}  # role -> (time, [{'id','title'}]) — har file par poori list na mangni pade (429)


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    HUB.log_seq += 1
    HUB.log_lines.append((HUB.log_seq, line))
    try:  # log file bhi (baad mein dekhne ke liye) — test server (doosra port) asal log mein na likhe
        if PORT != 8765:
            raise OSError("test")
        d = OUT_ROOT / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{datetime.now():%Y-%m-%d}.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    asyncio.get_event_loop().create_task(HUB.send("designer", {"type": "log", "line": line}))


def notify(title, message, sticky=True):
    """Windows popup (designer extension se) — taake doosri profile par kaam karte hue bhi pata chale.
    Dashboard ke Alerts mein bhi aata hai."""
    HUB.alert_seq += 1
    level = "error" if re.search(r"error|ghalat|fail|limit|nahi", title, re.I) else (
        "done" if re.search(r"khatam|done|complete", title, re.I) else "info")
    HUB.alerts.append({"id": HUB.alert_seq, "time": datetime.now().strftime("%H:%M:%S"),
                       "date": datetime.now().strftime("%Y-%m-%d"), "title": title, "message": message, "level": level})
    role = "designer" if "designer" in HUB.clients else next(iter(HUB.clients), None)
    if role:
        asyncio.get_event_loop().create_task(
            HUB.send(role, {"type": "notify", "title": title, "message": message, "sticky": sticky}))


async def handler(ws):
    role = None
    try:
        async for raw in ws:
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "hello":
                role = msg["role"]
                try:
                    want = json.loads((BASE / "extension_src" / "manifest.json").read_text(encoding="utf-8"))["version"]
                except Exception:
                    want = msg.get("version")
                if msg.get("version") and msg["version"] != want:
                    # purana extension code -> khud reload (folder se naya code utha lega)
                    print(f"{role}: extension v{msg['version']} purani hai -> reload (v{want})")
                    await ws.send(json.dumps({"type": "cmd", "id": 0, "cmd": "reload", "args": {}}))
                    continue
                if role in CFG.get("disabled_roles", []):  # band ki hui extension (Envato) -> kaam nahi milta
                    log(f"{role.upper()} extension band (config disabled_roles) — connect nahi ki")
                    role = None
                    continue
                HUB.clients[role] = ws
                log(f"Connected: {role.upper()} v{msg.get('version') or 'PURANA (reload karo)'}   "
                    f"(abhi connected: {', '.join(r.upper() for r in HUB.clients)})")
                if role == "designer":
                    await HUB.publish_status()
            elif t == "result":
                fut = HUB.pending.get(msg["id"])
                if fut and not fut.done():
                    fut.set_result(msg)
            elif t == "debug":  # debug_cmd.py se: kisi extension ko seedha command
                try:
                    res = {"ok": True, "result": await HUB.call(msg["role"], msg["cmd"], timeout=120, **msg.get("args", {}))}
                except Exception as e:
                    res = {"ok": False, "error": str(e)}
                await ws.send(json.dumps(res, default=str))
            elif t == "start":
                if HUB.running:
                    if HUB.pause_requested:
                        await HUB.resume()
                    else:
                        log("Pehle se chal raha hai.")
                else:
                    start_run(msg.get("only"), msg.get("only_post"))
            elif t == "stop":
                await HUB.request_pause()
            elif t == "status":
                await ws.send(json.dumps(HUB.run_status()))
    finally:
        if role and HUB.clients.get(role) is ws:
            del HUB.clients[role]
            print(f"Disconnected: {role}")


# ---------------------------------------------------------------- state ----

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


PROGRESS_FILE = OUT_ROOT / "progress.json"


def load_progress():
    """doc_id -> {output naam: file path} — adhoori file ki ban chuki images (resume ke liye)."""
    try:
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_progress(doc_id, name, path):
    if not doc_id or not path:
        return
    data = load_progress()
    data.setdefault(doc_id, {})[name] = str(path)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def clear_progress(doc_id):
    data = load_progress()
    if data.pop(doc_id, None) is not None:
        PROGRESS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def save_state(state):
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ------------------------------------------------------------ doc parsing ----

# file naam ke yeh lafz company ka hissa nahi (e.g. "Molen_Tax_Image_Details_From_10-3")
NOISE_WORDS = {"image", "images", "detail", "details", "from", "post", "posts", "content", "contents",
               "design", "designs", "story", "stories", "reel", "reels", "caption", "captions", "copy",
               "social", "media", "week", "weekly", "month", "monthly", "final", "new", "updated", "draft",
               "file", "doc", "instructions", "brief"}


def clean_title(title):
    name = re.sub(r"\s*-\s*Google (Docs|Drive)\s*$", "", title).strip()
    name = re.sub(r"\.(docx?|pdf|txt|rtf|odt)$", "", name, flags=re.I).strip()
    return re.sub(r"[_]+", " ", name).strip()


def company_from_title(title):
    """'Molen_Tax_Image_Details_From_10-3.docx' -> 'Molen Tax';  'Belle Clinic London UK 7' -> 'Belle Clinic London UK'"""
    words = clean_title(title).split()
    out = []
    for i, w in enumerate(words):
        if re.search(r"\d", w):
            break  # pehla number -> yahan se aage company nahi
        if w.lower().strip("-.,()") in NOISE_WORDS and out and all(
                re.search(r"\d", x) or x.lower().strip("-.,()") in NOISE_WORDS for x in words[i:]):
            break  # aage sirf faltu lafz / number (Image Details From 10-3) -> company khatam
        out.append(w)
    return " ".join(out).strip(" -") or clean_title(title)


def company_alias(company):
    """config.company_aliases: {"ficus financial": "Beyond the Budget"} -> file ka naam, asal company ki chat."""
    aliases = {norm(k): v for k, v in (CFG.get("company_aliases") or {}).items()}
    return aliases.get(norm(company))


def search_terms(title):
    """Chat dhoondne ke liye: poora naam, company, phir chhote hisse (pehle 3, 2, 1 lafz)."""
    full = re.sub(r"\s+", " ", clean_title(title))
    company = company_from_title(title)
    terms = [full, company]
    words = company.split()
    terms += [" ".join(words[:k]) for k in range(len(words) - 1, 0, -1)]
    seen, out = set(), []
    for t in terms:
        if t and norm(t) not in seen and len(norm(t)) >= 3:
            seen.add(norm(t))
            out.append(t)
    return out


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


DONE_RE = re.compile(r"(?<![a-z])(done|posted|✅|✔️?|☑️?)(?![a-z])", re.I)  # "POST#4 posted" = pehle publish ho chuki
NOPUB_RE = re.compile(r"\b(do\s*n[o']?t\s*pub\w*|not\s*pub\w*|don'?t\s*post)\b", re.I)
SECTION_RE = re.compile(
    r"^[\W\d_]*?"                                       # bullets / numbering / emoji (📌 Post 2)
    r"(?P<pre>(?:done|✅|✔️?|☑️?)[\s:\-–]*)?"            # "DONE INSTA POST:" (pehle se bani)
    r"(?P<label>(?:[A-Za-z][A-Za-z0-9 /&+\-]{0,40}?)?\b(?:posts?|story|stories|reels?|carousel|video)\b"
    r"[A-Za-z0-9 #/&+\-]{0,25}?(?:\s*\([^)]*\))?)"
    r"\s*[*_~]*\s*"                                     # markdown wrappers around label
    r"(?:[:\-–;]\s*(?P<rest>.*)|$)", re.I)  # "Story; 4 ..." bhi section
# Molen jaisa: "10/3Done", "10/11 Do not publish"
DATE_RE = re.compile(r"^\s*(?P<label>\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s*(?P<rest>.{0,40})$")
HEADING_RE = re.compile(r"^\W*(?:headings?|headlines?|lines?|on[ -]screen(?: reel)? text)\W*?(?:[:;]\s*(?P<rest>.*)|\s*$)", re.I)
CAPTION_RE = re.compile(r"^\W*caption\W*?(?::\s*(?P<rest>.*)|\s*$)", re.I)   # "CAPTION: x" / "**Caption **"
URL_RE = re.compile(r"https?://\S+")
IG_POST_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|tv)/", re.I)
# reel aksar link se pehchani jati hai: Instagram reel, Freepik video, stock video, Shorts/TikTok
VIDEO_LINK_RE = re.compile(
    r"(instagram\.com/(?:[\w.]+/)?reels?/|(?:freepik\.[a-z.]+|magnific\.com)/(?:free-video|premium-video|video)|"
    r"envato\.com/[^ ]*(?:stock-video|video-templates|/video/)|videohive\.net|pexels\.com/[a-z-]*/?video|"
    r"pixabay\.com/videos|youtube\.com/shorts|youtu\.be/|youtube\.com/watch|tiktok\.com|vimeo\.com|"
    r"\.(mp4|mov|webm)(\?|$))", re.I)
# sirf yeh links "image" maane jaate hain (company website / blog / donate links nahi)
IMAGE_HOST_RE = re.compile(
    r"(pinterest\.|pin\.it/|pinimg\.com|freepik\.|magnific\.com|envato\.com|envatousercontent\.com|unsplash\.com|pexels\.com|pixabay\.com|shutterstock\.com|"
    r"istockphoto\.com|gettyimages\.|stock\.adobe\.com|dreamstime\.com|123rf\.com|depositphotos\.com|rawpixel\.com|"
    r"vecteezy\.com|images\.openai\.com|oaiusercontent\.com|gstatic\.com/images|googleusercontent\.com|"
    r"drive\.google\.com/(file|open|uc)|docs\.google\.com/uc|google\.[a-z.]+/imgres|google\.[a-z.]+/search\?.*(tbm=isch|udm=2)|"
    r"imgur\.com|ibb\.co|wixstatic\.com|squarespace-cdn\.com|share\.google/|images\.app\.goo\.gl)", re.I)
IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|avif|heic)$", re.I)


def is_image_link(url):
    if VIDEO_LINK_RE.search(url):
        return False
    return bool(IMAGE_HOST_RE.search(url) or IMAGE_EXT_RE.search(urlparse(url).path))


def clean_url(u):
    return u.rstrip(").,;\"'”")


def is_long(text):
    """Lamba paragraph = caption (heading hamesha choti hoti hai)."""
    return len(text.split()) > 20 or len(text) > 140


def first_line(text):
    """Caption ki sirf pehli line / pehla sentence (over content nahi)."""
    text = text.strip().split("\n")[0]
    m = re.match(r"^(.+?[.!?])(\s|$)", text)
    line = (m.group(1) if m else text).strip()
    if len(line.split()) > 30:  # bohat lamba sentence -> pehle comma tak
        line = re.split(r"[,;:\u2013\u2014]", line)[0].strip()
    return line


def prepare_doc_format(doc):
    """Add labels to a working copy only. Original rows stay byte-for-byte unchanged."""
    rows = list(doc)
    cleaned = [_clean_line(line) for line, _ in rows]
    starts = []
    for i, line in enumerate(cleaned):
        if match_label(line)[0] or DATE_RE.match(line):
            starts.append(i)
    if starts:
        spans = []
        if starts[0] and any(URL_RE.search(x) for x in cleaned[:starts[0]]):
            spans.append((0, starts[0], False))
        spans.extend((start, starts[n + 1] if n + 1 < len(starts) else len(rows), True)
                     for n, start in enumerate(starts))
    else:
        # Link-first documents: an image link starts each post.
        links = [i for i, line in enumerate(cleaned) if URL_RE.fullmatch(line)
                 and (is_image_link(line) or IG_POST_RE.search(line))]
        first = next((i for i, line in enumerate(cleaned) if line), None)
        if links and first == links[0]:
            spans = [(start, links[n + 1] if n + 1 < len(links) else len(rows), False)
                     for n, start in enumerate(links)]
        else:
            spans = [(0, len(rows), False)] if rows else []
    additions, warnings = {}, []
    for start, end, labelled in spans:
        title = cleaned[start] if labelled else f"Post at line {start + 1}"
        if labelled and (DATE_RE.match(title) or re.search(r"\b(reel|video)\b", title, re.I)):
            continue  # design briefs and reels are not heading/caption documents
        indices = [i for i in range(start + int(labelled), end) if cleaned[i]]
        hi = next((i for i in indices if HEADING_RE.match(cleaned[i])), None)
        ci = next((i for i in indices if CAPTION_RE.match(cleaned[i])), None)
        if hi is not None and ci is not None:
            continue
        text = [i for i in indices if not URL_RE.search(cleaned[i])
                and not HEADING_RE.match(cleaned[i]) and not CAPTION_RE.match(cleaned[i])]
        if not text:
            continue
        if ci is not None:
            before = [i for i in text if i < ci]
            if before:
                additions[before[0]] = "Heading:"
            continue  # explicit caption-only post remains caption-only
        if hi is not None:
            # Explicit heading: a link followed by prose is an unlabelled caption.
            link = next((i for i in indices if i > hi and URL_RE.search(cleaned[i])), None)
            after = [i for i in text if link is not None and i > link]
            if after:
                additions[after[0]] = "Caption:"
            continue
        first = text[0]
        links_after = [i for i in indices if i > first and URL_RE.search(cleaned[i])]
        link = links_after[0] if links_after else None
        before = [i for i in text if link is not None and i < link]
        # A short opening headline followed by its own link is unambiguous.
        # Mixed bold elsewhere in the document must not suppress this headline.
        if before and all(len(cleaned[i].split()) <= 40 and len(cleaned[i]) <= 280 for i in before):
            head = before
        elif rows[first][1]:
            head = []
            for i in text:
                if not rows[i][1]:
                    break
                head.append(i)
        elif (len(cleaned[first].split()) <= 40 and len(cleaned[first]) <= 280
              and (len(text) == 1 or is_long(cleaned[text[1]])
                   or (indices[0] < first and URL_RE.fullmatch(cleaned[indices[0]])
                       and is_image_link(cleaned[indices[0]])))):
            head = [first]
        else:
            warnings.append(f"{title}: heading/caption boundary unclear")
            continue
        additions[head[0]] = "Heading:"
        rest = [i for i in text if i > head[-1]]
        if rest:
            additions[rest[0]] = "Caption:"
    formatted, inserted = [], []
    for i, row in enumerate(rows):
        if i in additions:
            inserted.append(len(formatted))
            formatted.append((additions[i], True))
        formatted.append(row)
    assert [r for i, r in enumerate(formatted) if i not in set(inserted)] == rows
    return formatted, {"inserted_labels": len(inserted), "inserted_row_indices": inserted,
                       "warnings": warnings, "original_content_preserved": True}


async def prepare_file(tab_id, doc_id, out_dir):
    original = await fetch_doc(tab_id, doc_id)
    formatted, review = prepare_doc_format(original)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "source_content.txt").write_text("\n".join(l for l, _ in original), encoding="utf-8")
    (out_dir / "doc_text.txt").write_text("\n".join(f"**{l}**" if b else l for l, b in original), encoding="utf-8")
    (out_dir / "formatted_content.txt").write_text("\n".join(l for l, _ in formatted), encoding="utf-8")
    (out_dir / "format_review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    if review["warnings"]:
        # poori file nahi rokte: woh post bhi banegi, summary mein "heading check karo" aayega
        log(f"  (Format check: {'; '.join(review['warnings'])} — post banegi, aap heading check kar lena)")
    log(f"  Format ready: {out_dir.name}, {review['inserted_labels']} labels added; original text same")
    return formatted


def match_label(line):
    """Post label line? 'Post 1 https://...' -> label URL ke baghair pehchano. Return (match, urls_on_line)."""
    urls = [u.group(0) for u in URL_RE.finditer(line)]
    probe = URL_RE.sub(" ", line).strip(" -–:\t") if urls else line
    probe = re.sub(r"(\d)\.\s*$", r"\1", probe)  # "Post 2." (Angela 15: neeche wali 2 posts chhoot gayi thin)
    # "Post 2 | Did you know?" -> label "Post 2" ("|" ke baad sirf qisam ka naam; Dental 10 posts: 7 posts chhoot gayi thin)
    probe = re.sub(r"^(\W*(?:posts?|story|stories|reels?|carousel|video)\s*#?\s*\d+)\s*\|.*$", r"\1", probe, flags=re.I)
    if re.match(r"^\W*carousel\s+slides?\W*$", probe, re.I):
        return None, urls  # "Carousel slides:" post ke andar ka sub-heading hai, nayi post nahi
    m = SECTION_RE.match(probe)
    if m and (m.group("rest") is not None or len(probe.split()) <= 6) \
            and not HEADING_RE.match(probe) and not CAPTION_RE.match(probe):
        return m, urls
    return None, urls


def _clean_line(line):
    line = line.strip().replace("\u00a0", " ")
    line = re.sub(r"\*+([A-Za-z0-9][^*]{0,40}?)\*+", r"\1", line)  # "*POST 2* https://..." -> "POST 2 https://..."
    return re.sub(r"^\*+\s*|\s*\*+$", "", line).strip()  # markdown ** hatao


def split_sections(doc):
    """Doc ko posts mein baanto. Jo tareeqa doc mein mile:
       1) label lines: "INSTA/FB POST:", "Post 28", "STORY:", "REEL:"
       2) date lines:  "10/3 Done", "10/11 Do not publish"   (Molen jaisa — neeche image prompt / brief)
       3) kuch na mile: har image link (Pinterest waghera) se nayi post  (dental care jaisa)"""
    sections, cur, pre = [], None, []
    for line, bold in doc:
        line = _clean_line(line)
        if re.fullmatch(r"[=\-_*~#\s]{3,}", line):
            continue  # "=========" separator
        if cur is None and line:
            pre.append((line, bold))  # pehle label se upar ka text
        m, label_urls = match_label(line)
        d = DATE_RE.match(line)
        if m and cur is not None and not any(x for x in cur["lines"]) and not label_urls \
                and re.fullmatch(r"\s*(?:(?:\d+\s*[- ]?\s*)?sec(?:ond)?s?\s+)?(story|stories|reels?|carousel|video)"
                                 r"(?:\s+(?:text|script|cover|title|hook))?\s*", m.group("label"), re.I) \
                and not (m.group("rest") or "").strip():
            # "Post#3" ke foran neeche akela "Story" -> nayi post nahi, pichli post story hai
            cur["label"] += f" ({m.group('label').strip()})"
            continue
        if m and cur is not None and re.fullmatch(
                r"(?:(?:\d+\s*[- ]?\s*)?sec(?:ond)?s?\s+)?reels?\s*(?:cover|title|hook|text|script)?",
                m.group("label").strip(), re.I) and not (m.group("rest") or "").strip():
            # "POST 17 | REEL" ke neeche "**REEL COVER**" (lines pehle se hain) -> nayi section nahi
            if "(reel)" not in cur["label"].lower():
                cur["label"] += " (reel)"
            cur["lines"].append(line)
            cur["bold"].append(bold)
            continue
        if m and cur is not None and re.fullmatch(r"(video|reel)s?\s*links?", m.group("label").strip(), re.I):
            # "Post 3" ke neeche "Video link: ..." -> nayi post nahi; yeh Post 3 hi hai aur woh REEL hai
            if "(video)" not in cur["label"]:
                cur["label"] += " (video)"
            cur["lines"].append(line)
            cur["bold"].append(bold)
            continue
        if m:
            rest = (m.group("rest") or "").strip()
            label = m.group("label").strip()
            cur = {"label": DONE_RE.sub("", label).strip(" ()-#"), "lines": [rest] + label_urls,
                   "bold": [None] * (1 + len(label_urls)),
                   "done": bool(m.group("pre")) or bool(DONE_RE.search(label)) or bool(re.match(r"^\W*done\b", rest, re.I)),
                   "nopub": bool(NOPUB_RE.search(rest)), "brief": False,
                   # "Post 1 | Informational carousel" -> "|" ke baad ki qisam (comparison / carousel pehchan)
                   "desc": (re.search(r"\|\s*(.*)$", URL_RE.sub("", line)) or [None, ""])[1].strip()}
            sections.append(cur)
        elif d and (not d.group("rest") or len(d.group("rest").split()) <= 5):
            rest = d.group("rest").strip()
            cur = {"label": f"Post {d.group('label')}", "lines": [rest], "bold": [None],
                   "done": bool(DONE_RE.search(rest)), "nopub": bool(NOPUB_RE.search(rest)), "brief": True}
            sections.append(cur)
        elif cur is not None:
            # label ke foran neeche akela "DONE" -> bhi bani hui
            if re.fullmatch(r"\W*(done|✅|✔️?|☑️?)\W*", line, re.I) and len(cur["lines"]) <= 3:
                cur["done"] = True
                continue
            cur["lines"].append(line)
            cur["bold"].append(bold)
    if sections:
        # Eliquid jaisa: pehli post par "Post 1" likha hi nahi -> label se upar text + link ho to woh bhi post
        pre = [(l, b) for l, b in pre if not SECTION_RE.match(l)]
        if pre and any(URL_RE.search(l) for l, _ in pre):
            sections.insert(0, {"label": "Post 1", "lines": [""] + [l for l, _ in pre], "bold": [None] + [b for _, b in pre],
                                "done": False, "nopub": False, "brief": False})
        return sections

    # 3) koi label nahi -> image link se posts alag karo.
    #    Image hamesha USI post ki rahe: pehle dekho link post ke SHURU mein hai ya AAKHIR mein.
    lines = [(_clean_line(l), b) for l, b in doc]
    lines = [(l, b) for l, b in lines if l and not re.fullmatch(r"[=\-_*~#\s]{3,}", l)]

    def is_link_line(l):
        urls = [clean_url(u.group(0)) for u in URL_RE.finditer(l)]
        return bool(urls) and len(l.split()) <= 2 and bool(is_image_link(urls[0]) or IG_POST_RE.search(urls[0])
                                                            or VIDEO_LINK_RE.search(urls[0]))

    link_idx = [i for i, (l, _) in enumerate(lines) if is_link_line(l)]
    if not link_idx:
        return []
    link_first = link_idx[0] == 0  # doc link se shuru -> link = nayi post ka shuru (dental care jaisa)
    new = lambda: {"label": "Post", "lines": [""], "bold": [None], "done": False, "nopub": False, "brief": False}
    cur = new()
    for i, (l, b) in enumerate(lines):
        if link_first and is_link_line(l) and len(cur["lines"]) > 1:
            sections.append(cur)
            cur = new()
        cur["lines"].append(l)
        cur["bold"].append(b)
        if not link_first and is_link_line(l):
            sections.append(cur)  # link post ke aakhir mein -> yahin post khatam
            cur = new()
    if len(cur["lines"]) > 1 and any(not URL_RE.search(x) for x in cur["lines"][1:]):
        sections.append(cur)
    return sections


def parse_doc(doc):
    """RULES wala parser (backup). doc = text ya [(line, bold)] -> (designs, reels, done_posts)"""
    if isinstance(doc, str):
        doc = [(l, None) for l in doc.replace("\r", "").split("\n")]
    has_bold = any(b for _, b in doc)

    def title_of(s):  # summary ke liye: heading ya pehli asal line
        for i, l in enumerate(s["lines"]):
            h = HEADING_RE.match(l)
            if h:
                return (h.group("rest") or next((x for x in s["lines"][i + 1:] if x), ""))[:60]
        first = next((l for l in s["lines"][1:] if l and not URL_RE.search(l)), "")
        return re.sub(r"^\W*caption\W*:\s*", "", first, flags=re.I)[:60]

    designs, reels, done_posts = [], [], []
    carousel_no = 0
    for s in split_sections(doc):
        label = s["label"].upper()
        desc = (s.get("desc") or "").upper()  # "POST 17 | REEL | NINJA" -> desc mein REEL
        all_urls = [clean_url(u.group(0)) for l in s["lines"] for u in URL_RE.finditer(l)]
        video_link = next((u for u in all_urls if VIDEO_LINK_RE.search(u)), None)
        has_image = any(is_image_link(u) or IG_POST_RE.search(u) for u in all_urls)
        if (re.search(r"REEL|VIDEO", label) or re.search(r"\bREEL\b|\bVIDEO\b", desc)
                or (video_link and not has_image)
                or any(REEL_TEXT_RE.match(line) for line in s["lines"])):
            # reel aap khud banate ho (label se ya video link se pehchani) -> skip, summary mein
            why = f" [video link: {video_link[:60]}]" if video_link and not re.search(r"REEL|VIDEO", label) else ""
            reels.append(f"{s['label']}: {title_of(s)}{why}".strip(": "))
            if designs:  # reel ki jagah (sets / din ki ginti: reel bhi 6 mein)
                designs[-1]["reels_after"] = designs[-1].get("reels_after", 0) + 1
            continue
        if s["done"] or s["nopub"]:
            why = "done" if s["done"] else "do not publish"
            done_posts.append(f"{s['label']} ({why}): {title_of(s)}".strip(": "))
            continue
        body = list(zip(s["lines"][1:], s["bold"][1:]))  # pehli line = label ke aage ka note, content nahi

        # links: poori post mein dhoondo (shuru / heading ke baad / caption ke baad)
        urls = [clean_url(u.group(0)) for l, _ in body for u in URL_RE.finditer(l)]
        ig_url = next((u for u in urls if IG_POST_RE.search(u)), None)

        # CAROUSEL: "Slide 1 / Slide 2 ..." -> har slide ALAG image (us slide ka text + us slide ki image)
        slides = split_slides(body)
        if slides:
            carousel_no += 1
            # poore carousel ki ek image (slides se upar, Dental: share.google link) -> pehli slide ki photo
            post_img = next((u for u in urls if not IG_POST_RE.search(u) and is_image_link(u)
                             and all(u != sl["image"] for sl in slides)), None)
            for k, sl in enumerate(slides, 1):
                img = sl["image"] or (post_img if k == 1 else None)
                d = new_design("post", f"{s['label']} - Slide {k}", sl["text"], img, ig_url)
                d["carousel"], d["slide"], d["section"] = carousel_no, k, s["label"]
                if not sl["text"] and ig_url:
                    d["ig_slide"] = k  # text Instagram post ki isi number ki slide se
                elif not sl["text"]:
                    d["text_from_image"] = True
                designs.append(d)
            continue
        image_url = next((u for u in urls if not IG_POST_RE.search(u) and is_image_link(u)), None)
        ref_links = [u for u in urls if u != image_url and u != ig_url and not is_image_link(u)]
        text_lines = [(l, b) for l, b in body if l and not (URL_RE.search(l) and len(l.split()) <= 3)]

        # writer ne image ki jagah IMAGE PROMPT likha (label ke saath ya heading se pehle) -> image khud banegi
        prompt_lines = [s["lines"][0]] if is_image_prompt(s["lines"][0]) else []
        if not s["brief"]:
            stop = next((i for i, (l, _) in enumerate(text_lines) if HEADING_RE.match(l) or CAPTION_RE.match(l)), None)
            pre = text_lines[:stop] if stop is not None else []
            if pre and any(is_image_prompt(l) for l, _ in pre) and (
                    stop is not None and HEADING_RE.match(text_lines[stop][0]) or all(is_long(l) for l, _ in pre)):
                prompt_lines += [l for l, _ in pre]
                text_lines = text_lines[stop:]
        image_prompt = "\n".join(URL_RE.sub("", l).strip() for l in prompt_lines).strip()

        content, caption_first = [], None
        if s["brief"]:
            # Molen jaisa: poora image prompt / brief hi content hai
            content = [URL_RE.sub("", l).strip(" ()") for l, _ in text_lines]
            content = [c for c in content if c and not re.search(r"still in draft", c, re.I)]
        else:
            hi = next((i for i, (l, _) in enumerate(text_lines) if HEADING_RE.match(l)), None)
            ci = next((i for i, (l, _) in enumerate(text_lines) if CAPTION_RE.match(l)), None)
            if ci is not None:
                rest = CAPTION_RE.match(text_lines[ci][0]).group("rest")
                after = [l for l, _ in text_lines[ci + 1:]]
                cap = (rest or "").strip() or (after[0] if after else "")
                caption_first = first_line(cap) if cap else None
            if hi is not None:
                # "HEADING:" / "**Heading**" se "CAPTION" tak sab heading content
                rest = HEADING_RE.match(text_lines[hi][0]).group("rest")
                if rest and rest.strip():
                    content.append(rest.strip())
                end = ci if ci is not None and ci > hi else len(text_lines)
                for l, b in text_lines[hi + 1:end]:
                    if ci is None and is_long(l) and not b:
                        break  # CAPTION likha nahi, lamba para = caption
                    content.append(l)
            else:
                # heading likha nahi -> shuru ki bold lines heading (writer bold karti hai)
                top = text_lines[:ci] if ci is not None else text_lines
                if has_bold:
                    for l, b in top:
                        if not b:
                            break
                        content.append(l)
                else:
                    # bold nahi: agar link se pehle text hai (Eliquid jaisa) -> wahi heading
                    first_link = next((i for i, (l, _) in enumerate(body) if URL_RE.search(l)), None)
                    before = [l for l, _ in body[:first_link] if l] if first_link else []
                    src = [(l, None) for l in before] if before else top
                    for l, b in src[:4]:
                        if is_long(l):
                            break
                        content.append(l)
                if not content and top:
                    caption_first = first_line(top[0][0])  # caption ki pehli line
        from_caption = False
        if not content and caption_first and not ig_url:
            content = [caption_first]  # heading nahi mili -> sirf caption ki pehli line, us se aage nahi
            from_caption = True
        content_text = "\n".join(content).strip()
        text_from_image = False
        if not content_text and not ig_url:
            if not image_url and not image_prompt:
                continue  # na text, na image -> summary mein "samajh nahi aayi"
            text_from_image = True  # sirf image: us image par likha text hi is post/story ka text
        # product store: product ka link (aksar caption mein) -> us product ki asal image
        # BEFORE / AFTER (construction jaisi): "Before: link" + "After: link" -> DONO images zaroori.
        #   carousel: slide 1 before photo + chhota "BEFORE" label, slide 2 after photo + "AFTER"
        #   comparison: ek hi image mein dono photos (config before_after_mode)
        ba = before_after_links(body)
        if ba and not s["brief"]:
            # heading sirf "BEFORE / AFTER" ho to woh label hi hai; asal heading ho to slide 1 / comparison par
            # "Work in progress: <link>" / "After:" lines heading nahi (photo par chhap jati)
            ba_text = "\n".join(l for l in content if not BA_RE.match(l) and not URL_RE.search(l)).strip()
            extra = ba_text if re.sub(r"(?i)\b(before|after|vs|work\s*in\s*progress|in\s*progress|wip|during)\b|[\W_]+",
                                      "", ba_text) else ""
            # format doc se: "comparison / side by side / split / one image" -> ek image; "carousel / swipe" -> carousel;
            # kuch na likha ho -> config (before_after_mode)
            words = " ".join([s["label"], s.get("desc", "")] + [l for l, _ in body if not URL_RE.search(l)][:6]).lower()
            if re.search(r"compar|side[ -]by[ -]side|split|one image|single (image|post)|\bvs\b|collage", words):
                mode = "comparison"
            elif re.search(r"carousel|swipe|slides?\b", words):
                mode = "carousel"
            else:
                mode = CFG.get("before_after_mode", "carousel")
            if mode == "comparison":
                d = new_design("post", s["label"], "\n".join(x for x in (extra, ba[2], "AFTER") if x), ba[0])
                d.update(section=s["label"], before_after="pair", after_url=ba[1], after_path=None, ba_heading=extra,
                         ba_label=ba[2])
                designs.append(d)
                continue
            carousel_no += 1
            for k, (tag, url) in enumerate(zip((ba[2], "AFTER"), ba[:2]), 1):
                head = extra if k == 1 else ""
                d = new_design("post", f"{s['label']} - {tag.title()}", "\n".join(x for x in (head, tag) if x), url)
                d.update(carousel=carousel_no, slide=k, section=s["label"], before_after=tag, ba_heading=head)
                designs.append(d)
            continue
        product_url = next((u for u in ref_links if is_product_link(u)), None)
        # pakka product link nahi -> baqi store-jaise links baad mein page khol kar check (Product data hai?)
        maybe_products = [u for u in ref_links if u != product_url and not SOCIAL_RE.search(u)][:2]
        designs.append(new_design("story" if "STOR" in label else "post", s["label"], content_text,
                                  image_url, ig_url, s["brief"], ref_links if s["brief"] else [], from_caption,
                                  product_url))
        designs[-1]["maybe_products"] = [] if s["brief"] else maybe_products
        designs[-1]["text_from_image"] = text_from_image and not image_prompt
        designs[-1]["section"] = s["label"]
        if image_prompt and not image_url:
            designs[-1]["image_prompt"] = image_prompt
    return designs, reels, done_posts


IMAGE_PROMPT_RE = re.compile(
    r"\b(create|design|generate|show(?:ing)?|depict|illustrat\w*|photograph\w*|photo|image|visual\w*|render\w*|"
    r"infographic|layout|composition|mockup|scene|background|lighting|light|aesthetic|realistic|editorial|"
    r"icons?|tones?|depth of field|close-up|flat ?lay|minimalist|cinematic)\b", re.I)


def is_image_prompt(line):
    """Writer ki image banane wali hidayat (prompt) — heading/caption nahi. Lambi line + design ke lafz."""
    line = (line or "").strip()
    return len(line.split()) >= 10 and len(IMAGE_PROMPT_RE.findall(line)) >= 2


# "Work in progress" / "WIP" / "During" = BEFORE photo (Aim Tech: sirf WIP wali photo li jati thi, After chhoot jati)
BA_RE = re.compile(r"^\W*(?P<tag>before|after|work\s*in\s*progress|in\s*progress|wip|during)\b"
                   r"(?:\s*(?:photo|image|pic(?:ture)?))?\W*(?P<rest>.*)$", re.I)


def before_after_links(body):
    """'Before: link' / 'After: link' (ya label ki agli line par link) -> (before_url, after_url); dono na hon -> None."""
    found, pending, labels = {}, None, {}
    for line, _ in body:
        if CAPTION_RE.match(line):
            break
        urls = [clean_url(u.group(0)) for u in URL_RE.finditer(line)]
        urls = [u for u in urls if is_image_link(u) and not IG_POST_RE.search(u)]
        m = BA_RE.match(line)
        if m and (urls or not URL_RE.sub("", m.group("rest")).strip(" :-–")):
            tag = "after" if m.group("tag").lower() == "after" else "before"
            if tag == "before":  # jo writer ne likha wohi photo par chhape: "WORK IN PROGRESS" ya "BEFORE"
                labels.setdefault("before", re.sub(r"\s+", " ", m.group("tag")).upper())
            if urls:
                found.setdefault(tag, urls[0])
                pending = None
            else:
                pending = tag
        elif pending and urls and len(line.split()) <= 2:
            found.setdefault(pending, urls[0])
            pending = None
        elif line.strip():
            pending = None
    return (found["before"], found["after"], labels.get("before", "BEFORE")) if len(found) == 2 else None


SLIDE_RE = re.compile(r"^\W*slide\s*(\d+)\W*?(?:[:\-–;]\s*(?P<rest>.*))?$", re.I)
SLIDE_CONT_RE = re.compile(r"^\W*(\d{1,2})\W*?(?:[:\-–;]\s*(?P<rest>.*))?$", re.I)  # "2; url" (Slide 1 ke baad)


def split_slides(body):
    """Carousel ki slides: 'Slide 1: text' / 'slide 2 https://img' ... (CAPTION se pehle). 2+ slides -> list."""
    slides, cur = [], None
    for line, _ in body:
        if CAPTION_RE.match(line):
            break
        probe = URL_RE.sub(" ", line).strip()
        m = SLIDE_RE.match(probe) if probe else None
        if not m and slides and probe:
            m = SLIDE_CONT_RE.match(probe)
            if m and not (re.search(r"[:\-–;]", probe) or URL_RE.search(line)):
                m = None  # "2 DAYS TO GO!" caption line nahi
        if m:
            cur = {"text": [], "image": None}
            slides.append(cur)
            rest = (m.group("rest") or "").strip()
            if rest:
                cur["text"].append(rest)
        elif cur is None:
            continue
        elif probe and not HEADING_RE.match(line):
            cur["text"].append(probe)
        for u in URL_RE.finditer(line):
            url = clean_url(u.group(0))
            if cur and cur["image"] is None and is_image_link(url) and not IG_POST_RE.search(url):
                cur["image"] = url
    if len(slides) < 2:
        return []
    return [{"text": "\n".join(t for t in sl["text"] if t).strip(), "image": sl["image"]} for sl in slides]


PRODUCT_RE = re.compile(
    r"(/products?/|/shop/[^/?#]+|/item/|/dp/|/gp/product/|/p/[\w\-]+|/collections/[^/]+/products/|"
    r"[?&](product|product_id|variant)=|etsy\.com/listing/|amazon\.[a-z.]+/|daraz\.|ebay\.[a-z.]+/itm/)", re.I)


SOCIAL_RE = re.compile(r"(facebook\.com|instagram\.com|tiktok\.com|youtube\.com|youtu\.be|linkedin\.com|twitter\.com|"
                       r"x\.com/|wa\.me|whatsapp|zeffy\.com|gofundme|calendly|mailto:|tel:)", re.I)


async def is_product_page(url):
    """Link khol kar dekho: store ka product page hai? (Product data / og:type product / price)"""
    html = ""
    try:
        r = await HUB.call("designer", "fetch", url=url)
        if r["status"] == 200 and "html" in r["contentType"]:
            html = base64.b64decode(r["b64"]).decode("utf-8", "replace")
    except Exception:
        pass
    if not html:
        try:
            st, ct, body = await asyncio.to_thread(direct_get, url)
            html = body.decode("utf-8", "replace") if st == 200 else ""
        except Exception:
            return False
    return bool(re.search(r'"@type"\s*:\s*"Product"|og:type"\s+content="product|content="product"\s+property="og:type"|'
                          r'product:price:amount|itemprop="price"|woocommerce-product|shopify-product', html, re.I))


def is_product_link(url):
    return bool(PRODUCT_RE.search(url)) and not IG_POST_RE.search(url) and not is_image_link(url)


def new_design(kind, label, content, image_url=None, ig_url=None, brief=False, ref_links=(), from_caption=False,
               product_url=None):
    return {"kind": kind, "label": label, "content": content, "from_caption": from_caption, "brief": brief,
            "image_url": image_url, "image_path": None, "ig_url": ig_url, "ig_shot": None,
            "ref_links": list(ref_links), "ref_text": None, "product_url": product_url, "product_path": None}


async def parse_with_chatgpt(doc):
    """BACKUP jab rules wala parser kuch na samjhe (naya format): Wajid ki TEMPORARY chat
    (history mein save nahi hoti) doc parhti hai. Jo text/link doc mein hoobahoo nahi, woh hata diya jata hai."""
    rows = [(i, l.strip(), b) for i, (l, b) in enumerate(doc) if l.strip()]
    numbered = "\n".join(f"{i}: {'**' + l + '**' if b else l}" for i, l, b in rows)
    wajid = Chat("wajid")
    await wajid.goto(CHATGPT + "/?temporary-chat=true")
    await ensure_chat_mode(wajid)
    sent_at = await wajid.send(CFG["parse_prompt"] + "\n\nDOCUMENT (line number: text, **bold**):\n" + numbered)
    await wajid.wait_done(CFG["timeouts_minutes"]["text_reply"])
    await run_sleep(2)
    reply = await wajid.latest_reply(sent_at)
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        raise RuntimeError("ChatGPT se doc ka JSON nahi mila")
    posts = json.loads(m.group(0)).get("posts", [])

    text_at = {i: l for i, l, _ in rows}
    designs, reels, done_posts, last_end = [], [], [], -1
    for p in sorted(posts, key=lambda p: int(p.get("first_line", 0))):
        a, z = int(p.get("first_line", 0)), int(p.get("last_line", 0))
        if a <= last_end or z < a:
            log(f"  (ChatGPT post lines {a}-{z} overlap — skip)")
            continue
        last_end = z
        region = "\n".join(text_at.get(i, "") for i in range(a, z + 1))
        nregion = norm(region)
        # sirf woh heading lines jo is post ke text mein hoobahoo hain (naya content nahi)
        head = [h for h in str(p.get("heading") or "").split("\n") if h.strip() and norm(h) and norm(h) in nregion]
        inside = lambda u: bool(u) and u in region  # link isi post ka ho
        imgs = [u for u in p.get("image_links") or [] if inside(u)]
        ig = p.get("instagram_link") if inside(p.get("instagram_link")) else None
        prod = p.get("product_link") if inside(p.get("product_link")) else None
        refs = [u for u in p.get("reference_links") or [] if inside(u)]
        title = (head[0] if head else region.split("\n")[0])[:60]
        t, status = str(p.get("type", "post")).lower(), str(p.get("status", "todo")).lower()
        if t == "reel":
            reels.append(f"Reel (line {a}): {title}")
            if designs:
                designs[-1]["reels_after"] = designs[-1].get("reels_after", 0) + 1
            continue
        if status in ("done", "do_not_publish"):
            done_posts.append(f"Post (line {a}) ({status.replace('_', ' ')}): {title}")
            continue
        if not head and not ig:
            continue
        label = "LinkedIn Post" if t == "linkedin" else ("Story" if t == "story" else "Post")
        designs.append(new_design("story" if t == "story" else "post", label, "\n".join(head),
                                  imgs[0] if imgs else None, ig, bool(p.get("is_brief")), refs, False, prod))
    return designs, reels, done_posts


class _DocHTML(HTMLParser):
    """Google Docs HTML export -> [(line, bold)]  (bold = line ka zyada tar text bold)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.cur, self.style, self.bold_classes = [], [], "", set()
        self.skip = 0          # <style>/<title>/<script> ke andar
        self.bold_stack = []   # har khule tag ka bold hai ya nahi
        self.block_bold = False

    def _flush(self):
        text = "".join(t for t, _ in self.cur).strip()
        if text:
            total = sum(len(t.strip()) for t, _ in self.cur) or 1
            boldc = sum(len(t.strip()) for t, b in self.cur if b)
            self.lines.append((re.sub(r"\s+", " ", text), boldc / total >= 0.6))
        self.cur = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "title", "script", "head"):
            self.skip += 1
            return
        if tag in ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "div"):
            self._flush()
            self.block_bold = tag.startswith("h")
        if tag == "br":
            self._flush()
            return
        style = (a.get("style") or "").replace(" ", "")
        bold = tag in ("b", "strong") or "font-weight:700" in style or "font-weight:bold" in style \
            or bool(set((a.get("class") or "").split()) & self.bold_classes)
        self.bold_stack.append(bold)

    def handle_endtag(self, tag):
        if tag in ("style", "title", "script", "head"):
            self.skip -= 1
            if tag == "style":
                self.bold_classes |= set(re.findall(r"\.([\w-]+)\{[^}]*font-weight:\s*(?:700|bold)", self.style))
            return
        if self.bold_stack:
            self.bold_stack.pop()
        if tag in ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "div"):
            self._flush()
            self.block_bold = False

    def handle_data(self, data):
        if self.skip:
            if self.lasttag == "style":
                self.style += data  # CSS: kaun si class bold hai
            return
        self.cur.append((data, self.block_bold or any(self.bold_stack)))


def html_to_lines(html):
    p = _DocHTML()
    p.feed(html)
    p.close()
    p._flush()
    return p.lines


async def fetch_doc(tab_id, doc_id):
    """Doc -> [(line, bold)]. Pehle HTML (bold pata chalta hai), phir txt, phir .docx."""
    # pehle background mein (aap ki tab chhede baghair), na ho to tab ke andar se
    try:
        r = await HUB.call("designer", "fetch", url=f"https://docs.google.com/document/d/{doc_id}/export?format=html")
        html = base64.b64decode(r["b64"]).decode("utf-8", "replace") if r["status"] == 200 else ""
        if "text/html" in r["contentType"] and "<body" in html:
            lines = html_to_lines(html)
            if lines:
                return lines
    except Exception:
        pass
    r = await HUB.exec("designer", tab_id, "fetch_text", f"/document/d/{doc_id}/export?format=html")
    if r and r["status"] == 200 and "text/html" in r["contentType"] and "<body" in r["text"]:
        lines = html_to_lines(r["text"])
        if lines:
            return lines
    r = await HUB.exec("designer", tab_id, "fetch_text", f"/document/d/{doc_id}/export?format=txt")
    if r and r["status"] == 200 and "text/plain" in r["contentType"]:
        return [(l, None) for l in r["text"].lstrip("﻿").replace("\r", "").split("\n")]
    # .docx (Office mode) -> Drive se docx download kar ke parho (runs se bold)
    from docx import Document
    for url in (f"https://drive.google.com/uc?export=download&id={doc_id}",
                f"https://docs.google.com/document/d/{doc_id}/export?format=docx"):
        r = await HUB.call("designer", "fetch", url=url)
        body = base64.b64decode(r["b64"])
        if r["status"] == 200 and body[:2] == b"PK":
            out = []
            for p in Document(io.BytesIO(body)).paragraphs:
                runs = [x for x in p.runs if x.text.strip()]
                style_bold = bool(p.style and p.style.font and p.style.font.bold) or "Heading" in (p.style.name or "")
                bold = bool(runs) and all(x.bold or (x.bold is None and style_bold) for x in runs)
                for part in p.text.split("\n"):  # shift+enter wali lines
                    out.append((part, bold))
            return out
    raise RuntimeError("Doc ka text nahi mila (html + txt + docx teeno fail)")


# ------------------------------------------------------- image download ----

def _ext_for(ct, url):
    ext = mimetypes.guess_extension((ct or "").split(";")[0].strip()) or ""
    if ext in ("", ".jpe"):
        ext = Path(urlparse(url).path).suffix or ".jpg"
    return ext if len(ext) <= 5 else ".jpg"


async def download_image(url, dest_noext):
    """Designer profile (us ke cookies, Freepik login) se image download."""
    host = urlparse(url).netloc.lower()
    if ("freepik." in host or "magnific.com" in host) and not host.startswith("img."):
        return await download_freepik_or_screenshot(url, dest_noext)
    if "envato.com" in host:
        return await download_envato(url, dest_noext)
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?.*?id=)([\w-]+)", url)
    if m:  # Google Drive file
        url = f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    if re.search(r"share\.google/|images\.app\.goo\.gl", url):  # Google share link -> google.com/imgres?imgurl=...
        try:
            url = await asyncio.to_thread(resolve_redirect, url)
        except Exception as e:
            log(f"    (share link nahi khula: {e})")
    m = re.search(r"[?&]imgurl=([^&]+)", url)
    if m:  # Google Images link -> asal image ka url us ke andar hota hai
        from urllib.parse import unquote
        url = unquote(m.group(1))
    r = await HUB.call("designer", "fetch", url=url)
    if r["status"] == 200 and r["contentType"].startswith("image/"):
        return _save(r, url, dest_noext)
    # page link (Pinterest / product / koi bhi page) -> page ki asal image
    candidates = []
    if r["status"] == 200 and "html" in r["contentType"]:
        candidates = page_image_candidates(base64.b64decode(r["b64"]).decode("utf-8", "replace"), url)
    if not candidates:  # public page -> server khud seedha khole (Pinterest waghera ko login nahi chahiye)
        try:
            st, ct, body = await asyncio.to_thread(direct_get, url)
            if st == 200 and "html" in ct:
                candidates = page_image_candidates(body.decode("utf-8", "replace"), url)
        except Exception as e:
            log(f"    (seedha page nahi khula: {e})")
    if not candidates:  # JS wala page -> tab khol kar dekho
        tab = await HUB.call("designer", "open_tab", url=url, active=False)
        try:
            await run_sleep(4)
            candidates = [c for c in await HUB.exec("designer", tab, "main_images") or [] if c]
        finally:
            await HUB.call("designer", "close_tab", tabId=tab)
    for img in candidates:
        r = await HUB.call("designer", "fetch", url=img)
        if r["status"] == 200 and r["contentType"].startswith("image/") and len(r["b64"]) > 8000:
            return _save(r, img, dest_noext)
        try:  # extension se na aaye to server seedha
            st, ct, body = await asyncio.to_thread(direct_get, img)
            if st == 200 and ct.startswith("image/") and len(body) > 6000:
                return _save({"contentType": ct, "b64": base64.b64encode(body).decode()}, img, dest_noext)
        except Exception:
            pass
    # aakhri tareeqa: page khol kar "Save image as" (Google Images waghera)
    tab = await HUB.call("designer", "open_tab", url=url, active=True)
    try:
        await run_sleep(5)
        src = await HUB.call("designer", "save_image", tabId=tab)
        return _from_download(src, dest_noext)
    except Exception as e:
        raise RuntimeError(f"Image download nahi hui: {url} ({e})")
    finally:
        await HUB.call("designer", "close_tab", tabId=tab)


def resolve_redirect(url):
    """share.google / goo.gl -> aakhri link (Dental 10 posts ki images isi tarah thin)."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.geturl()


def direct_get(url):
    """Server khud (bina browser cookies) public page / image download kare."""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/141.0 Safari/537.36", "Accept": "text/html,image/*,*/*"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.headers.get("content-type", ""), r.read()


async def fetch_reference_text(url, limit=900):
    """Link wala page parho (title + description + pehle paragraphs) — sirf image ke idea ke liye."""
    r = await HUB.call("designer", "fetch", url=url)
    if r["status"] != 200 or "html" not in r["contentType"]:
        return None
    html = base64.b64decode(r["b64"]).decode("utf-8", "replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    desc = re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']+)', html, re.I)
    body = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    paras = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", p)).strip()
             for p in re.findall(r"<(?:p|h1|h2|h3|li)[^>]*>(.*?)</(?:p|h1|h2|h3|li)>", body, re.S | re.I)]
    paras = [p for p in paras if len(p.split()) >= 5]
    import html as _h
    parts = [x for x in [title and title.group(1).strip(), desc and desc.group(1)] if x] + paras
    text = _h.unescape(" | ".join(parts))
    if re.search(r"page not found|log ?in|sign in|private", text[:200], re.I) and len(paras) < 2:
        return None  # draft / login wala page
    return text[:limit].strip() or None


def _save(r, url, dest_noext):
    path = Path(str(dest_noext) + _ext_for(r["contentType"], url))
    path.write_bytes(base64.b64decode(r["b64"]))
    return path


def page_image_candidates(html, page_url):
    """Page ke HTML se asal image: product JSON-LD > og:image > twitter:image. Pinterest -> originals."""
    from urllib.parse import urljoin
    out = []
    # product schema (Shopify / WooCommerce / zyada tar stores)
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            o = stack.pop(0)
            if isinstance(o, dict):
                if "@graph" in o:
                    stack += o["@graph"]
                t = o.get("@type")
                if (t == "Product" or (isinstance(t, list) and "Product" in t)) and o.get("image"):
                    img = o["image"]
                    img = img[0] if isinstance(img, list) else img
                    img = img.get("url") if isinstance(img, dict) else img
                    if img:
                        out.append(img)
    for prop in ("og:image:secure_url", "og:image", "twitter:image"):
        for m in re.finditer(rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]*>', html, re.I):
            c = re.search(r'content=["\']([^"\']+)', m.group(0))
            if c:
                out.append(c.group(1))
    res = []
    for u in out:
        u = urljoin(page_url, u.replace("&amp;", "&"))
        if u.startswith("//"):
            u = "https:" + u
        if "pinimg.com" in u:  # Pinterest: sab se bari (original) image pehle try
            res.append(re.sub(r"pinimg\.com/\d+x\d*/", "pinimg.com/originals/", u))
        res.append(u)
    return list(dict.fromkeys(res))


async def download_freepik(url, dest_noext):
    tab = await HUB.call("designer", "open_tab", url=url, active=True)
    try:
        await run_sleep(4)
        since = await HUB.call("designer", "now")
        for attempt in range(3):
            clicked = await HUB.exec("designer", tab, "freepik_click")
            if not clicked:
                raise RuntimeError("Freepik par Download button nahi mila")
            try:
                src = await HUB.call("designer", "wait_download", since=since, timeout=25000)
                return _from_download(src, dest_noext)
            except RuntimeError:
                await run_sleep(2)  # shayad popup khula -> dobara Download
        raise RuntimeError("Freepik download nahi hua")
    finally:
        await HUB.call("designer", "close_tab", tabId=tab)


async def download_freepik_or_screenshot(url, dest_noext):
    """Freepik/Magnific: 2 dafa asal download; dono fail -> page ki preview image (ya screenshot). Post skip nahi."""
    errors = []
    for attempt in range(2):
        try:
            return await download_freepik(url, dest_noext)
        except Exception as e:
            errors.append(str(e))
            log(f"    (Freepik download {attempt + 1}/2 fail: {e})")
            await run_sleep(3)
    log("    Freepik download 2 dafa fail — page ki image ka screenshot le raha hoon")
    try:
        return await envato_screenshot(url, dest_noext, role="designer")
    except Exception as e:
        raise RuntimeError(f"Freepik download aur screenshot dono fail: {'; '.join(errors)}; screenshot: {e}")


async def download_envato(url, dest_noext):
    """Envato: download nahi — link hamesha Azhar (instagram) profile mein khol kar preview image ka screenshot.
    (config envato_screenshot_role). Pehli dafa na mile -> ek dafa aur."""
    role = CFG.get("envato_screenshot_role", "instagram")
    if role not in HUB.clients:
        raise RuntimeError(f"{role.upper()} (Azhar) profile ki extension connected nahi — Envato screenshot nahi ho saka")
    errors = []
    for attempt in range(2):
        try:
            path = await envato_screenshot(url, dest_noext, role=role)
            log(f"    Envato: {role} profile se screenshot liya")
            return path
        except Exception as e:
            errors.append(str(e))
            log(f"    (Envato screenshot {attempt + 1}/2 fail: {e})")
            await run_sleep(3)
    raise RuntimeError(f"Envato screenshot nahi hua: {'; '.join(errors)}")


async def envato_screenshot(url, dest_noext, role=None):
    """Stock page (Envato / Freepik) ki preview image: asal preview file, warna us ka screenshot."""
    from PIL import Image
    role = role or CFG.get("envato_screenshot_role", "instagram")
    tab = await HUB.call(role, "open_tab", url=url, active=True)
    try:
        await run_sleep(6)
        rect = await HUB.exec(role, tab, "main_image_rect")
        if not rect:
            raise RuntimeError("page par preview image nahi mili")
        path = Path(str(dest_noext) + "_screenshot.png")
        if rect.get("src", "").startswith("http") and min(rect.get("nw", 0), rect.get("nh", 0)) >= 600:
            try:  # preview image ki asal file (screenshot se behtar)
                r = await HUB.call(role, "fetch", url=rect["src"])
                if r["status"] == 200 and r["contentType"].startswith("image/"):
                    Image.open(io.BytesIO(base64.b64decode(r["b64"]))).convert("RGB").save(path)
                    return path
            except Exception:
                pass
        png = await HUB.call(role, "capture", tabId=tab)
        img = Image.open(io.BytesIO(base64.b64decode(png.split(",", 1)[1])))
        d = rect["dpr"]
        box = tuple(int(round(v * d)) for v in (rect["x"], rect["y"], rect["x"] + rect["w"], rect["y"] + rect["h"]))
        if box[2] - box[0] >= 100 and box[3] - box[1] >= 100:
            img.crop(box).convert("RGB").save(path)
            return path
        # image ki jagah 0 size mili (window chhoti / tab nazar nahi aa rahi) -> preview file jitni bhi ho
        if rect.get("src", "").startswith("http") and min(rect.get("nw", 0), rect.get("nh", 0)) >= 300:
            r = await HUB.call(role, "fetch", url=rect["src"])
            if r["status"] == 200 and r["contentType"].startswith("image/"):
                Image.open(io.BytesIO(base64.b64decode(r["b64"]))).convert("RGB").save(path)
                return path
        raise RuntimeError(f"preview image screen par nazar nahi aayi ({role} profile ki window minimize to nahi?)")
    finally:
        await HUB.call(role, "close_tab", tabId=tab)


async def _download_envato_once(url, dest_noext):
    """ENVATO profile (Envato Elements login) mein item kholo -> Download -> 'Add & Download'."""
    if "envato" not in HUB.clients:
        raise RuntimeError("ENVATO profile ki extension connected nahi")
    tab = await HUB.call("envato", "open_tab", url=url, active=True)
    try:
        await run_sleep(5)
        since = await HUB.call("envato", "now")
        for attempt in range(4):  # Download -> (project/license popup) -> Add & Download
            clicked = await HUB.exec("envato", tab, "envato_click")
            if not clicked and attempt == 0:
                raise RuntimeError("Envato par Download button nahi mila (login hai?)")
            try:
                src = await HUB.call("envato", "wait_download", since=since, timeout=20000)
                return _from_download(src, dest_noext)
            except RuntimeError:
                await run_sleep(2)
        raise RuntimeError("Envato download nahi hua")
    finally:
        await HUB.call("envato", "close_tab", tabId=tab)


def _from_download(src, dest_noext):
    """Download hui file -> image. Zip ho to andar ki sab se bari image nikaalo."""
    import zipfile
    src = Path(src)
    if src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            imgs = [i for i in z.infolist() if IMAGE_EXT_RE.search(i.filename) and not i.filename.startswith("__MACOSX")]
            if not imgs:
                raise RuntimeError("Zip mein koi image nahi")
            best = max(imgs, key=lambda i: i.file_size)
            path = Path(str(dest_noext) + Path(best.filename).suffix.lower())
            path.write_bytes(z.read(best))
            return path
    path = Path(str(dest_noext) + (src.suffix or ".jpg"))
    shutil.copy(src, path)
    return path


def instagram_embed_urls(html):
    """Instagram ki embed page se images: album (GraphSidecar) -> har slide ka link (tarteeb se), warna post ki image.
    Album ke links JSON ke andar kai dafa escape hote hain (slash aur %) -> saaf karo."""
    import html as _html

    def clean(u):
        u = re.sub(r"\\+/", "/", u)
        u = re.sub(r"\\+u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), u)
        return _html.unescape(u.replace("\\", ""))
    if "GraphSidecar" in html:
        urls = []
        for u in re.findall(r'display_url\W+(https.+?)\\*"', html):
            u = clean(u)
            if u not in urls:
                urls.append(u)
        if len(urls) > 1:
            return urls
    m = re.search(r'class="EmbeddedMediaImage"[^>]*?src="([^"]+)"', html)
    return [clean(m.group(1))] if m else []


async def instagram_from_embed(url, dest):
    """Screen ke baghair: embed page se saari slides ki asal image files (Instagram dheere load ho tab bhi)."""
    from PIL import Image
    m = re.search(r"instagram\.com/(?:[\w.]+/)?(p|reel|tv)/([\w-]+)", url)
    if not m:
        return []
    r = await HUB.call("instagram", "fetch", url=f"https://www.instagram.com/{m.group(1)}/{m.group(2)}/embed/captioned/")
    if r["status"] != 200:
        return []
    shots = []
    for k, u in enumerate(instagram_embed_urls(base64.b64decode(r["b64"]).decode("utf-8", "replace")), 1):
        img_r = await HUB.call("instagram", "fetch", url=u)
        if img_r["status"] != 200 or len(img_r["b64"]) < 8000:
            return []  # ek slide bhi na mile -> screen wala tareeqa (koi slide chhootni nahi)
        path = dest.with_name(f"{dest.stem}_slide{k}.png")
        Image.open(io.BytesIO(base64.b64decode(img_r["b64"]))).convert("RGB").save(path)
        shots.append(path)
    return shots


async def screenshot_instagram(url, dest):
    """INSTAGRAM profile se post ki har slide ki image (caption nahi). Pehle embed page se (tez, poora album),
    na mile to post khol kar screen se. Last slide sirf kisi company ka CTA ho to skip."""
    from PIL import Image
    shots = []
    try:
        shots = await instagram_from_embed(url, dest)
    except Exception as e:
        log(f"    (Instagram embed se nahi mila: {e} — post khol kar screen se)")
    if shots:
        log(f"    Instagram: {len(shots)} slide(s) seedha image file se" + (" (album)" if len(shots) > 1 else ""))
        if len(shots) > 1 and await is_cta_slide(shots[-1]):
            shots[-1].rename(shots[-1].with_name(shots[-1].stem + "_SKIPPED_CTA.png"))
            shots.pop()
        return shots
    tab = await HUB.call("instagram", "open_tab", url=url, active=True)
    try:
        await run_sleep(5)
        await HUB.exec("instagram", tab, "ig_prepare")
        await run_sleep(2)
        for slide in range(1, 21):
            # Instagram kabhi bohot dheere load hota hai (HR 8: 7s mein khaali page, 30s baad image) ->
            # pehli slide ke liye ~60s intezar (beech mein ek reload), agli slide ke liye ~10s
            rect, waits = None, (24 if slide == 1 else 4)
            for w in range(waits):
                rect = await HUB.exec("instagram", tab, "ig_media")
                if rect and rect.get("w", 0) > 100:
                    break
                rect = None
                if slide == 1 and w == 12:
                    log("    (Instagram page load nahi hua — reload kar ke dobara intezar)")
                    await HUB.call("instagram", "navigate", tabId=tab, url=url)
                    await run_sleep(5)
                if slide == 1 and w % 4 == 3:
                    await HUB.exec("instagram", tab, "ig_prepare")
                await run_sleep(2.5)
            if not rect:
                if not shots:
                    raise RuntimeError("Instagram post ki image nahi mili (60s intezar ke baad bhi — Instagram profile mein login hai?)")
                break
            path = None
            if rect.get("src", "").startswith("http"):  # asal image file (poori, kati hui nahi)
                try:
                    r = await HUB.call("instagram", "fetch", url=rect["src"])
                    if r["status"] == 200 and r["contentType"].startswith("image/") and len(r["b64"]) > 8000:
                        img = Image.open(io.BytesIO(base64.b64decode(r["b64"]))).convert("RGB")
                        path = dest.with_name(f"{dest.stem}_slide{slide}.png")
                        img.save(path)
                except Exception as e:
                    log(f"    (Instagram image file nahi mili: {e} — screenshot le raha hoon)")
            if not path:  # backup: screen ka screenshot (image beech mein laa kar)
                await run_sleep(1)
                png = await HUB.call("instagram", "capture", tabId=tab)
                img = Image.open(io.BytesIO(base64.b64decode(png.split(",", 1)[1])))
                d = rect["dpr"]
                box = tuple(int(round(v * d)) for v in (rect["x"], rect["y"], rect["x"] + rect["w"], rect["y"] + rect["h"]))
                path = dest.with_name(f"{dest.stem}_slide{slide}.png")
                img.crop(box).save(path)
            shots.append(path)
            # album ka "Next" button page der se dikhata hai (HR 8: album sirf slide 1 bana) -> pehli slide par
            # kuch dafa dobara dekho, tab hi single post maano
            for _ in range(4 if slide == 1 and not rect["hasNext"] else 0):
                await run_sleep(2.5)
                await HUB.exec("instagram", tab, "ig_prepare")
                again = await HUB.exec("instagram", tab, "ig_media") or {}
                if again.get("hasNext"):
                    rect["hasNext"] = True
                    break
            if not rect["hasNext"]:
                break
            await HUB.exec("instagram", tab, "ig_next")
            await run_sleep(1.5)
    finally:
        await HUB.call("instagram", "close_tab", tabId=tab)
    if len(shots) > 1 and await is_cta_slide(shots[-1]):
        shots[-1].rename(shots[-1].with_name(shots[-1].stem + "_SKIPPED_CTA.png"))
        shots.pop()
    return shots


def _ocr(path):
    import winocr
    from PIL import Image
    return winocr.recognize_pil_sync(Image.open(path), "en")["text"]


_FOREIGN_LINE_RE = re.compile(
    r"@[a-z0-9_.]{3,}|https?://|www\.|\b[\w\-]+\.(?:com|co|org|net|in|uk|pk|io|au|ca|us)\b|[\w.+-]+@[\w-]+\.\w+"
    r"|\+?\d[\d\s\-()]{7,}\d", re.I)
_SOCIAL_ONLY_RE = re.compile(r"^(?:like|comment|share|save|send|follow(?: us)?(?: on)?|swipe\W*|get started\W*|[\W_]|\s)+$", re.I)


def _ocr_lines(path):
    import winocr
    from PIL import Image
    r = winocr.recognize_pil_sync(Image.open(path).convert("RGB"), "en")
    return [l["text"].strip() for l in r.get("lines") or [] if l.get("text", "").strip()] or \
        [x for x in (r.get("text") or "").split("\n") if x.strip()]


def slide_text(path):
    """Instagram slide ka text (OCR) — doosri company ka @handle / website / email / phone aur like-share lines hata kar."""
    keep = [l for l in _ocr_lines(path) if not _FOREIGN_LINE_RE.search(l) and not _SOCIAL_ONLY_RE.match(l)]
    return "\n".join(keep).strip()


def _words(text):
    return set(re.findall(r"[a-z]{3,}", (text or "").lower()))


def text_match(a, b):
    """Do texts kitne milte hain (0..1) — OCR ki ghalti ke bawajood lafzon se."""
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, min(len(wa), len(wb))) if wa and wb else 0.0


async def reorder_by_text(batch, out_dir, names):
    """ChatGPT carousel ki slides apne order mein bhi bana deta hai (Linkedup: 'Slide 2' file mein asal slide 3).
    Har bani image ka OCR asal slide ke text se milao -> file sahi slide ke naam par. Pakka na ho -> jaisa hai."""
    have = [o for o in batch if o.get("file") and o.get("expect_text")]
    if len(have) < 2:
        return
    try:
        got = [" ".join(await asyncio.to_thread(_ocr_lines, o["file"])) for o in have]
    except Exception as e:
        log(f"  (slide order check nahi hua: {e})")
        return
    score = [[text_match(g, o["expect_text"]) for o in have] for g in got]  # score[file][slot]
    pairs = sorted(((score[i][j], i, j) for i in range(len(have)) for j in range(len(have))), reverse=True)
    pick, used_i, used_j = {}, set(), set()
    for s, i, j in pairs:  # sab se pakki jodi pehle
        if i not in used_i and j not in used_j and s >= 0.35:
            pick[i] = j
            used_i.add(i)
            used_j.add(j)
    if len(pick) < len(have) or all(i == j for i, j in pick.items()):
        return
    files = [Path(o["file"]) for o in have]
    tmp = [f.with_name(f.stem + "_reorder" + f.suffix) for f in files]
    for f, t in zip(files, tmp):
        f.replace(t)
    for i, j in pick.items():
        dest = files[j]
        tmp[i].replace(dest)
        have[j]["file"] = str(dest)
    moved = [f"{have[j]['name']} <- {have[i]['name']}" for i, j in pick.items() if i != j]
    log(f"  Slide order theek kiya (OCR se): {'; '.join(moved)}")


async def is_cta_slide(path):
    """Last slide OCR se parho: agar sirf kisi company ka CTA hai (follow us, call, website...) -> True."""
    try:
        text = await asyncio.to_thread(_ocr, path)  # Windows OCR apna alag event loop chahta hai
    except Exception as e:
        log(f"  (OCR nahi chala: {e}) — last slide rakh raha hoon")
        return False
    low = " " + text.lower() + " "
    # website / www / @handle / phone aksar HAR slide ke footer mein hote hain (HR 8: asal aakhri slide
    # "website" ki wajah se skip ho gayi) -> yeh akele CTA nahi; CTA = asal jumla (follow us, book now...)
    strong = [k for k in CFG["cta_keywords"] if k in low and k.strip() != "www."]
    contact = ["www"] if "www." in low else []
    contact += ["@handle"] if re.search(r"@[a-z0-9_.]{3,}", low) else []
    contact += ["phone"] if re.search(r"\+?\d[\d\s\-()]{8,}\d", low) else []
    contact += ["website"] if re.search(r"\b[\w\-]+\.(com|co|org|net|in|uk|pk|io)\b", low) else []
    hits = strong + contact
    words = len(text.split())
    cta = (bool(strong) and (words <= CFG["cta_max_words"] or len(strong) >= 2)) \
        or (bool(contact) and words <= CFG.get("cta_contact_only_words", 12))
    log(f"  Last slide OCR ({words} words, CTA hits: {hits}) -> {'SKIP' if cta else 'rakh li'}")
    return cta


# -------------------------------------------------------------- ChatGPT ----

class AttachFailed(RuntimeError):
    """Image message box mein nahi lagi."""


class ChatLimit(RuntimeError):
    """ChatGPT account ki limit lagi hui hai (message nahi gaya)."""


class BrandChatNotFound(RuntimeError):
    """Search completed successfully, but no matching company branding chat exists."""


def is_chat_limit_notice(text):
    # Bare "rate" matches "generate"; only explicit limit notices qualify.
    return bool(re.search(
        r"\b(?:rate[ -]*limit(?:ed|ing)?|(?:usage|image(?: generation)?|message|daily|generation) limit|"
        r"(?:reached|hit|exceeded) (?:the |your |my |a )?(?:\w+ ){0,3}limit|"
        r"too many requests|usage resets|chat paused|paused until|limit reached|"
        r"try again (?:later|after|in \d)|upgrade to continue)\b", text, re.I))


class Chat:
    """Ek ChatGPT profile (waseem / wajid) ki tab."""

    def __init__(self, role):
        self.role = role
        self.tab = None

    async def ensure_tab(self):
        if self.tab is None:
            self.tab = await HUB.call(self.role, "chat_tab")
        return self.tab

    async def api(self, path, method="GET", body=None, retry=True):
        await self.ensure_tab()
        waits = CFG.get("api_429_waits", [10, 30, 60, 120]) if retry else []
        for attempt in range(len(waits) + 1):
            r = await HUB.exec(self.role, self.tab, "api", path, method, body)
            if r["status"] != 429 or attempt == len(waits):
                break
            # "Too many requests" -> ChatGPT thoda ruk kar phir jawab deta hai; fail nahi karte
            log(f"    ({self.role}: ChatGPT ne 'too many requests' kaha — {waits[attempt]}s ruk kar dobara)")
            await run_sleep(waits[attempt])
        if r["status"] >= 400:
            raise RuntimeError(f"ChatGPT API {path} -> {r['status']}: {r['text'][:200]}")
        return json.loads(r["text"]) if r["text"] else {}

    async def all_chats(self):
        """Sidebar ki chat list (id + title). Ek run mein ek dafa layi jati hai, phir yaad (429 se bachao)."""
        cached = CHAT_LIST_CACHE.get(self.role)
        if cached and time.time() - cached[0] < CFG.get("chat_list_cache_minutes", 15) * 60:
            return cached[1]
        items, offset = [], 0
        while offset < CFG.get("chat_list_max", 1000):
            data = await self.api(f"/backend-api/conversations?offset={offset}&limit=100&order=updated")
            batch = data.get("items", [])
            items += [{"id": i["id"], "title": i.get("title") or ""} for i in batch]
            if len(batch) < 100:
                break
            offset += 100
            await run_sleep(1.5)  # pages ke beech waqfa
        CHAT_LIST_CACHE[self.role] = (time.time(), items)
        return items

    async def goto(self, url):
        """ChatGPT page kholo. Load na ho to RUKO aur dobara koshish (30 files ke dauran fail nahi)."""
        tries = CFG.get("chatgpt_load_retries", 10)
        for attempt in range(1, tries + 1):
            try:
                # har dafa nayi tab (purani band) -> seedha isi link / chat par
                self.tab = await HUB.call(self.role, "fresh_chat_tab", url=url)
                for _ in range(60):
                    if await HUB.exec(self.role, self.tab, "has", SEL["composer"]):
                        await run_sleep(2)
                        return
                    await run_sleep(1)
            except Exception as e:
                log(f"  ({self.role} ChatGPT: {e})")
            if attempt == 1:  # page ka haal save karo taake selector theek ho sake
                try:
                    diag = await HUB.exec(self.role, self.tab, "diag") or {}
                    OUT_ROOT.mkdir(parents=True, exist_ok=True)
                    p = OUT_ROOT / f"debug_chatgpt_{self.role}.json"
                    p.write_text(json.dumps(diag, indent=2, ensure_ascii=False), encoding="utf-8")
                    log(f"  {self.role} page: {diag.get('url')} | \"{diag.get('title')}\" | details: {p}")
                except Exception as e:
                    log(f"  (diag fail: {e})")
            if attempt < tries:
                msg = f"{self.role.upper()} ChatGPT load nahi ho raha — 1 minute ruk kar dobara ({attempt}/{tries})"
                log("  " + msg)
                notify("ChatGPT load nahi ho raha", msg + ". Login / internet check kar lo.")
                await run_sleep(60)
        raise RuntimeError(f"{self.role}: ChatGPT ka message box load nahi hua ({tries} koshish) — login / internet check karo")

    async def url(self):
        return (await HUB.call(self.role, "tab_info", tabId=self.tab))["url"]

    async def conv_id(self):
        m = re.search(r"/c/([0-9a-f\-]{20,})", await self.url())
        return m.group(1) if m else None

    async def generating(self):
        """ChatGPT abhi likh / bana raha hai? (Stop button message box ke paas)"""
        if await HUB.exec(self.role, self.tab, "has", SEL["stop_button"]):
            return True
        btns = await HUB.exec(self.role, self.tab, "compose_buttons", SEL) or []
        return any(re.search(r"stop", f"{b.get('aria') or ''} {b.get('testid') or ''}", re.I) for b in btns)

    async def send(self, text, files=()):
        await ensure_chat_mode(self)  # Work mode kabhi nahi (har message se pehle 'Chat')
        payload = []
        for f in files:
            f = Path(f)
            payload.append({"name": f.name, "type": mimetypes.guess_type(f.name)[0] or "image/png",
                            "b64": base64.b64encode(f.read_bytes()).decode()})
        try:
            typed = await HUB.exec(self.role, self.tab, "compose", text, payload, SEL, timeout=300)
        except RuntimeError as e:
            if payload:
                raise AttachFailed(f"{self.role}: attachment verify nahi hui ({e})") from e
            raise
        if payload and (not typed or typed.get("attached", 0) != len(payload)):
            # image (product / photo) nahi lagi -> bina image ke message NAHI bhejna
            raise AttachFailed(f"{self.role}: {len(payload)} mein se {(typed or {}).get('attached', 0)} image hi lagi")
        if not typed or not typed.get("typed"):
            # page abhi load / badal raha tha -> ruk kar chat dobara kholo, ek dafa phir likho
            log(f"    ({self.role}: text box mein nahi gaya — page dobara khol kar phir likh raha hoon)")
            await run_sleep(5)
            await self.goto(await self.url())
            await run_sleep(3)
            try:
                typed = await HUB.exec(self.role, self.tab, "compose", text, payload, SEL, timeout=300)
            except RuntimeError as e:
                if payload:
                    raise AttachFailed(f"{self.role}: attachment verify nahi hui ({e})") from e
                raise
            if payload and (not typed or typed.get("attached", 0) != len(payload)):
                raise AttachFailed(f"{self.role}: {len(payload)} mein se {(typed or {}).get('attached', 0)} image hi lagi")
            if not typed or not typed.get("typed"):
                raise RuntimeError(f"{self.role}: ChatGPT box mein text nahi likha gaya")
        sent_at = time.time()
        how = await HUB.exec(self.role, self.tab, "submit", SEL, timeout=300)
        log(f"    ({self.role}: message bheja via {how})")
        await run_sleep(3)
        # ChatGPT ki limit? ("Chat paused until usage resets at 8:38 PM")
        form = await HUB.exec(self.role, self.tab, "query", "main form") or []
        note = " ".join(f.get("text") or "" for f in form)
        if is_chat_limit_notice(note):
            raise ChatLimit(f"{self.role.upper()} ChatGPT limit: {note.strip()[:120]}")
        for _ in range(60):  # reply shuru hone tak
            if await self.generating():
                break
            await run_sleep(1)
        return sent_at

    async def wait_done(self, minutes):
        end = run_time() + minutes * 60
        started, stuck = run_time(), CFG.get("stuck_refresh_minutes", 10) * 60
        while run_time() < end or await self.generating():
            if run_time() - started > stuck:  # 10 min se likh hi raha hai -> atka hua; refresh kar ke API se jawab
                log(f"  {self.role}: ChatGPT {stuck // 60} minute se atka — tab refresh")
                url = await self.url()
                await self.goto(url)
                return
            if not await self.generating():
                await run_sleep(3)
                if not await self.generating():
                    return
            await run_sleep(3)
        raise RuntimeError("ChatGPT reply time-out")

    async def find_chat(self, terms, company, doc_text):
        """terms = [poora file naam, company, chhote hisse...] (case-insensitive).
        Pehle poore naam se, phir hisson se; har milti chat ka content file se verify.
        Return (conv id, chat title) ya (None, None)."""
        await self.goto(CHATGPT + "/")
        nterms = [norm(t) for t in terms]

        def score(title):
            t = norm(title)
            if len(t) < 3 or re.match(r"^\W*old\b", title or "", re.I):
                return 0  # "OLD - Dental..." = bhaari / atki purani chat -> kabhi nahi kholni
            tc = t.replace(" ", "")  # "E liquid" / "E-Liquid" == "eliquid"
            best = 0
            for k, nt in enumerate(nterms):  # pehle wale terms (lambe) zyada behtar
                nc = nt.replace(" ", "")
                if len(nc) >= 5 and len(tc) >= 5 and (nc in tc or tc in nc):
                    best = max(best, (100 - k) if tc == nc else (70 - k * 5))  # space ke farq ke saath
                if t == nt:
                    best = max(best, 100 - k)
                elif len(nt) >= 4 and (re.search(rf"\b{re.escape(nt)}\b", t) or re.search(rf"\b{re.escape(t)}\b", nt)):
                    best = max(best, 80 - k * 5)
            return best

        def rank(items):
            seen, ranked = set(), []
            for it in sorted((it for it in items if it.get("id")), key=lambda it: -score(it["title"])):
                if it["id"] not in seen and score(it["title"]) > 0:
                    seen.add(it["id"])
                    ranked.append(it)
            return ranked

        async def check(candidates, tried):
            # ghalat chat se bachne ke liye: har candidate ka andar ka content file se milao
            for n, it in enumerate(candidates[:3]):  # sirf top 3 (har check ek bhari request)
                if it["id"] in tried:
                    continue
                tried.add(it["id"])
                if norm(it["title"]).replace(" ", "") in [x.replace(" ", "") for x in nterms[:2]]:
                    # title hoobahoo company ka naam -> bhari verify request ki zaroorat nahi
                    log(f"  {self.role}: \"{it['title']}\" -> MATCH (naam hoobahoo)")
                    remember_chat(self.role, company, it["id"], it["title"])
                    return it["id"], it["title"]
                if n:
                    await run_sleep(1)  # requests ke beech waqfa (429 se bachao)
                ok, why = await self.verify(it["id"], company, doc_text, exact=it.get("known") or
                                            norm(it["title"]).replace(" ", "") in [x.replace(" ", "") for x in nterms[:2]])
                log(f"  {self.role}: \"{it['title']}\" check -> {'MATCH' if ok else 'match nahi'} ({why})")
                if ok:
                    remember_chat(self.role, company, it["id"], it["title"])
                    return it["id"], it["title"]
            return None

        tried = set()
        # 1) yaad: is company ki chat pehle mil/ban chuki -> seedha (koi search/list nahi)
        known = CHAT_MEMORY.get(self.role, {}).get(norm(company))
        if known and not valid_chat_id(known.get("id")):  # tooti yaad -> bhool kar dobara dhoondo
            log(f"  {self.role}: yaad mein ghalat chat id {known.get('id')!r} — dobara dhoondh raha hoon")
            CHAT_MEMORY[self.role].pop(norm(company), None)
            known = None
        if known:  # pehle verify ho chuki -> dobara bhari request nahi
            log(f"  {self.role}: \"{known['title']}\" (yaad se)")
            return known["id"], known["title"]
        # 2) page ke sidebar se titles (API request nahi -> 429 nahi)
        items = []
        try:
            items = await HUB.exec(self.role, self.tab, "sidebar_chats", 40, timeout=120) or []
        except Exception as e:
            log(f"    ({self.role}: sidebar nahi parha gaya: {e})")
        found = await check(rank(items), tried)
        if found:
            return found
        # 3) ChatGPT search (2 requests, rukne par retry nahi)
        search_ok = False
        # poora naam, company, BINA SPACE ("Booth scout" -> "Boothscout"), pehla lafz ("Booth") — milte hi ruko
        words = company.split()
        queries = list(dict.fromkeys([t for t in terms[:2]] + ([company.replace(" ", "")] if len(words) > 1 else [])
                                     + ([words[0]] if words and len(words[0]) >= 4 else [])))
        for t in queries:
            try:
                data = await self.api(f"/backend-api/conversations/search?query={quote(t)}&cursor=", retry=False)
                search_ok = True
                items += [{"id": i.get("conversation_id") or i.get("id"), "title": i.get("title") or ""}
                          for i in data.get("items", [])]
            except Exception:
                pass
            found = await check(rank(items), tried)
            if found:
                return found
            await run_sleep(1)
        if search_ok:
            # ChatGPT search ne poori history dhoondi aur nahi mili -> chat waqai nahi; ruki hui list request nahi
            return None, None
        # 4) aakhri raasta: poori list (API). Yeh bhi ruk jaye to NAYI chat nahi banani (duplicate ho jati)
        try:
            items += await self.all_chats()
        except Exception as e:
            raise RuntimeError(f"{self.role}: ChatGPT ne chat list rok di ({e}) — duplicate chat se bachne ke liye "
                               "nayi chat nahi banayi; thodi der baad yeh file dobara chalegi") from e
        found = await check(rank(items), tried)
        return found or (None, None)

    async def verify(self, cid, company, doc_text, exact=False):
        """Chat ka text (bina message bheje, API se) file ke content se milao."""
        try:
            conv = await self.conversation(cid)
        except Exception as e:
            return False, f"chat parh nahi saka: {e}"
        chat = " " + norm(conversation_text(conv)) + " "
        if not chat.strip():
            return False, "chat khaali hai"
        name = norm(company)
        name_hit = f" {name} " in chat or name.replace(" ", "") in chat.replace(" ", "")
        toks = [t for t in name.split() if len(t) >= 3 and t not in STOPWORDS]
        tok_hit = sum(f" {t} " in chat for t in toks) / max(len(toks), 1)
        kws = doc_keywords(doc_text)
        kw_hit = sum(f" {k} " in chat for k in kws) / max(len(kws), 1)
        why = f"naam: {'haan' if name_hit else 'nahi'}, naam ke lafz: {tok_hit:.0%}, content keywords: {kw_hit:.0%}"
        ok = (exact and name_hit) or (name_hit and kw_hit >= 0.10) or (tok_hit == 1 and kw_hit >= 0.20) or kw_hit >= 0.40
        return ok, why

    async def latest_reply(self, sent_at):
        """ChatGPT ka jawab (API se — temporary chat ka bhi /c/<id> hota hai). Khaali = abhi nahi aaya."""
        cid = await self.conv_id()
        if not cid:
            return ""
        try:
            return assistant_text_after(await self.conversation(cid), sent_at)
        except Exception:
            return ""

    async def conversation(self, cid):
        return await self.api(f"/backend-api/conversation/{cid}")


STOPWORDS = set("""a an the and or but for of to in on at by with from into over under about as is are was were be
been being it its this that these those you your yours we our ours they their them he she his her i me my
can will would should could may might must do does did done have has had not no yes so if then than too very
just more most less much many some any all each every other such only own same also here there when where
what which who whom why how out up down off again further once new one two three get got make made use used
post posts caption captions heading story stories reel reels insta instagram facebook linkedin image images
picture photo photos client karein main use https http www com png jpg jpeg link links today now day days
week weeks year years help helps need needs time""".split())


def doc_keywords(text, n=25):
    """File ke sab se zyada aane wale khaas lafz (content ki 'type' pehchanne ke liye)."""
    words = [w for w in norm(URL_RE.sub(" ", text)).split()
             if len(w) >= 4 and w not in STOPWORDS and not w.isdigit()]
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:n]]


def conversation_text(conv):
    parts = []
    for m in ordered_messages(conv):
        for p in (m.get("content") or {}).get("parts") or []:
            if isinstance(p, str):
                parts.append(p)
    return "\n".join(parts)


def ordered_messages(conv):
    msgs = [n["message"] for n in conv.get("mapping", {}).values() if n.get("message")]
    return sorted(msgs, key=lambda m: m.get("create_time") or 0)


def assistant_text_after(conv, after_time):
    parts = []
    for m in ordered_messages(conv):
        if (m.get("create_time") or 0) < after_time or m["author"]["role"] != "assistant":
            continue
        c = m.get("content", {})
        if c.get("content_type") in ("text", "multimodal_text"):
            parts += [p for p in c.get("parts", []) if isinstance(p, str) and p.strip()]
    return "\n\n".join(parts).strip()


def image_ids_after(conv, since):
    """Mere message (since) ke BAAD ChatGPT ki banayi images ke file ids (purani nahi)."""
    ids = []
    mapping = conv.get("mapping", {})
    current = conv.get("current_node")
    if current in mapping:
        branch, visited = [], set()
        while current in mapping and current not in visited:
            visited.add(current)
            node = mapping[current]
            if node.get("message"):
                branch.append(node["message"])
            current = node.get("parent")
        messages = reversed(branch)
    else:
        messages = ordered_messages(conv)
    for m in messages:
        if m["author"]["role"] == "user" or (m.get("create_time") or 0) < since:
            continue
        for p in m.get("content", {}).get("parts", []) or []:
            if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer":
                fid = p.get("asset_pointer", "").split("://", 1)[-1]
                if fid and fid not in ids:
                    ids.append(fid)
    return ids


def image_file_ids(conv):
    ids = []
    for m in ordered_messages(conv):
        if m["author"]["role"] == "user":
            continue
        for p in m.get("content", {}).get("parts", []) or []:
            if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer":
                fid = p.get("asset_pointer", "").split("://", 1)[-1]
                if fid and fid not in ids:
                    ids.append(fid)
    return ids


async def get_instructions_from_wajid(company, terms, doc_text):
    wajid = Chat("wajid")
    cid, found = await wajid.find_chat(terms, company, doc_text)
    if not cid:
        raise BrandChatNotFound(f"Wajid mein bhi '{company}' ki chat nahi mili (search kiya: {terms})")
    log(f"  Wajid mein chat mili: \"{found}\" — instructions maang raha hoon")
    await wajid.goto(f"{CHATGPT}/c/{cid}")
    # chat ki AAKHRI baat-cheet hi instructions hai? -> dobara poochne ki zaroorat nahi
    conv = await wajid.conversation(cid)
    last = existing_instructions(conv, only_last=True)
    if last:
        log(f"  Wajid chat ka aakhri message hi instructions hai ({len(last)} chars) — dobara nahi poocha")
        return last
    try:
        sent_at = await wajid.send(CFG["wajid_ask_message"])
    except ChatLimit as e:
        log(f"  {e} — chat mein pehle se maujood instructions use kar raha hoon")
        text = existing_instructions(conv)
        if len(text) < 300:
            notify("Instructions nahi mili", f"{company}: {e} aur chat mein purani instructions bhi nahi")
            raise RuntimeError(f"{e} — aur '{company}' ki purani instructions bhi nahi mili")
        log(f"  Instructions mil gayin ({len(text)} chars)")
        return text
    await wajid.wait_done(CFG["timeouts_minutes"]["text_reply"])
    text = ""
    for _ in range(3):  # jawab page par poora ho chuka (wait_done) -> API se sirf 1-3 dafa
        await run_sleep(4)
        conv = await wajid.conversation(cid)
        text = assistant_text_after(conv, sent_at)
        if len(text) >= 300:
            break
    if len(text) < 300:
        arrived = any(m["author"]["role"] == "user" and (m.get("create_time") or 0) >= sent_at
                      for m in ordered_messages(conv))
        log(f"  Wajid se naya jawab nahi aaya ({'message gaya tha' if arrived else 'message gaya hi nahi — limit?'}) "
            f"— chat mein pehle se maujood instructions dhoond raha hoon")
        text = existing_instructions(conv)
    if len(text) < 300:
        notify("Instructions nahi mili", f"{company}: Wajid se instructions nahi mili — yeh file manual karni hogi")
        raise RuntimeError(f"Wajid se '{company}' ki instructions nahi mili (khaali jawab) — Waseem mein chat NAHI banayi")
    log(f"  Instructions mil gayin ({len(text)} chars)")
    return text


def existing_instructions(conv, only_last=False):
    """Chat mein pehle kabhi 'instructions' maangi gayi thi? -> us ka (sab se naya) jawab.
    only_last=True: sirf tab jab chat ka AAKHRI user message hi instructions wala ho."""
    msgs = ordered_messages(conv)
    def visible_user(m):  # ChatGPT ke chhupe (system/context) user messages nahi
        c = m.get("content") or {}
        return (m["author"]["role"] == "user" and c.get("content_type") in ("text", "multimodal_text")
                and any(isinstance(p, str) and p.strip() for p in c.get("parts") or [])
                and not (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"))
    users = [i for i, m in enumerate(msgs) if visible_user(m)]
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if only_last and users and i != users[-1]:
            continue
        parts = (m.get("content") or {}).get("parts") or []
        ask = " ".join(p for p in parts if isinstance(p, str))
        if visible_user(m) and re.search(r"in?str?u?ctions?|guideline|branding", ask, re.I):
            reply = []
            for n in msgs[i + 1:]:
                if visible_user(n):
                    break
                c = n.get("content") or {}
                if n["author"]["role"] == "assistant" and c.get("content_type") in ("text", "multimodal_text"):
                    reply += [p for p in c.get("parts") or [] if isinstance(p, str) and p.strip()]
            text = "\n\n".join(reply).strip()
            if len(text) >= 300:
                return text
    return ""


async def ensure_chat_mode(chat):
    """ChatGPT ke naye page par 'Chat | Work' toggle — hamesha 'Chat' (Work mode ki alag 5 ghante wali limit)."""
    try:
        if await HUB.exec(chat.role, chat.tab, "click_text", "button,[role=tab],[role=radio]", "Chat"):
            await run_sleep(2)
    except Exception:
        pass  # purani extension / toggle nahi -> jaisa hai
    if chat.role == "waseem":  # aap ka rule: Thinking effort hamesha High
        await chat_mode_ok(chat, set_high=True)


async def chat_mode_ok(chat, set_high=False):
    """Chat normal (Work nahi) aur Thinking effort High? Pata na chal sake -> True (jaisa hai chalne do)."""
    try:
        r = await HUB.exec(chat.role, chat.tab, "chat_mode_info", set_high, timeout=30) or {}
    except Exception:
        return True
    if set_high and r.get("effort") and r["effort"].lower() != "high":
        log(f"  (ChatGPT effort '{r['effort']}' — High nahi ho saka)")
    return not r.get("work") and (not r.get("effort") or r["effort"].lower() == "high")


async def create_waseem_chat(waseem, company, instructions, files=()):
    if len(instructions.strip()) < 300:
        raise RuntimeError("Branding instructions khaali/incomplete hain — nayi chat nahi banayi")
    await waseem.goto(CHATGPT + "/")
    await ensure_chat_mode(waseem)
    try:  # pehle se maujood chats (nayi chat pehchanne ke liye)
        known_ids = {c["id"] for c in await HUB.exec("waseem", waseem.tab, "sidebar_chats", 5) or []}
    except Exception:
        known_ids = set()
    await waseem.send(instructions + "\n\n" + CFG["waseem_save_message"].format(company=company), files)
    await waseem.wait_done(CFG["timeouts_minutes"]["text_reply"])
    cid = None
    for _ in range(120):  # lambi instructions -> ChatGPT der se nayi chat ka link deta hai
        cid = await waseem.conv_id()
        if cid:
            break
        await run_sleep(1)
    if not cid:  # backup: sidebar mein sab se nayi chat jo pehle nahi thi
        try:
            for c in await HUB.exec("waseem", waseem.tab, "sidebar_chats", 5) or []:
                if c["id"] not in known_ids:
                    cid = c["id"]
                    break
        except Exception:
            pass
    if not cid:
        page = (await HUB.exec("waseem", waseem.tab, "diag") or {}).get("text", "")
        if is_chat_limit_notice(page):  # message gaya hi nahi — limit (jaise "5-hour Work usage limit")
            note = next((l for l in page.splitlines() if is_chat_limit_notice(l)), "limit")
            raise ChatLimit(f"WASEEM ChatGPT limit (nayi chat nahi bani): {note.strip()[:150]}")
        raise RuntimeError("Nayi Waseem chat ka id nahi mila")
    await run_sleep(3)
    await waseem.api(f"/backend-api/conversation/{cid}", "PATCH", {"title": company})
    remember_chat("waseem", company, cid, company)
    if "waseem" in CHAT_LIST_CACHE:  # nayi chat foran list mein (isi company ki agli file ko mil jaye)
        CHAT_LIST_CACHE["waseem"][1].insert(0, {"id": cid, "title": company})
    log(f"  Waseem mein nayi chat bani, naam rakha: {company}")
    return cid


CHAT_REFRESH_FILE = OUT_ROOT / "chat_refresh.json"


def chat_refresh_pending():
    """Kin companies ki chat bhaari / atki hai -> agli file se pehle nayi chat (config + khud pakdi hui)."""
    try:
        auto = json.loads(CHAT_REFRESH_FILE.read_text(encoding="utf-8"))
    except Exception:
        auto = []
    return {norm(c) for c in list(auto) + list(CFG.get("refresh_chat_companies") or [])}


def mark_chat_refresh(company, add=True):
    try:
        data = json.loads(CHAT_REFRESH_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = []
    data = [c for c in data if norm(c) != norm(company)] + ([company] if add else [])
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    CHAT_REFRESH_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if not add and any(norm(c) == norm(company) for c in CFG.get("refresh_chat_companies") or []):
        # config wali list ek dafa ki -> nayi chat ban gayi to naam hata do (har file par nayi chat na bane)
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        cfg["refresh_chat_companies"] = [c for c in cfg.get("refresh_chat_companies", []) if norm(c) != norm(company)]
        (BASE / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        CFG["refresh_chat_companies"] = cfg["refresh_chat_companies"]


async def refresh_company_chat(waseem, company, old_cid, old_title):
    """Bhaari / baar baar atakti chat (Dental Care): usi chat se poori instructions lo -> Waseem mein NAYI chat
    (instructions + naam) -> purani ka naam 'OLD - ...' (script phir kabhi nahi kholti)."""
    log(f"  {company}: chat bhaari / atki — instructions le kar NAYI chat bana raha hoon")
    await waseem.goto(f"{CHATGPT}/c/{old_cid}")
    # pehle chat mein jo instructions pehle se hain (chat banate waqt ka pehla message) — High par ChatGPT se
    # dobara likhwana 10+ minute leta tha (ENDURING: time-out, nayi chat bani hi nahi)
    text = ""
    try:
        conv = await waseem.conversation(old_cid)
        text = existing_instructions(conv) or next(
            (p for m in ordered_messages(conv) if m["author"]["role"] == "user"
             for p in (m.get("content") or {}).get("parts") or [] if isinstance(p, str) and len(p) >= 300), "")
    except Exception as e:
        log(f"  (purani chat parhi nahi: {e})")
    if len(text) >= 300:
        log("  Purani chat ki instructions mil gayin (ChatGPT se dobara likhwaye baghair)")
        sent_at = None
    else:
        sent_at = await waseem.send(CFG["wajid_ask_message"])
        await waseem.wait_done(CFG["timeouts_minutes"]["text_reply"])
    for _ in range(3 if sent_at is not None else 0):
        await run_sleep(4)
        conv = await waseem.conversation(old_cid)
        text = assistant_text_after(conv, sent_at)
        if len(text) >= 300:
            break
    if len(text) < 300:  # jawab na aaya -> chat banate waqt di gayi instructions (pehla message)
        text = existing_instructions(conv) or next(
            (p for m in ordered_messages(conv) if m["author"]["role"] == "user"
             for p in (m.get("content") or {}).get("parts") or [] if isinstance(p, str) and len(p) >= 300), "")
    if len(text) < 300:
        raise RuntimeError(f"'{company}' ki purani chat se instructions nahi mili — purani chat hi chalegi")
    cid = await create_waseem_chat(waseem, company, text)
    try:
        await waseem.api(f"/backend-api/conversation/{old_cid}", "PATCH", {"title": f"OLD - {old_title}"})
        if "waseem" in CHAT_LIST_CACHE:
            for c in CHAT_LIST_CACHE["waseem"][1]:
                if c.get("id") == old_cid:
                    c["title"] = f"OLD - {old_title}"
    except Exception as e:
        log(f"  (purani chat ka naam nahi badla: {e} — memory nayi chat ki hai, phir bhi nayi hi khulegi)")
    mark_chat_refresh(company, add=False)
    log(f"  Nayi chat tayyar; purani ka naam 'OLD - {old_title}' (ab nahi khulegi)")
    notify("Nayi ChatGPT chat", f"{company}: purani chat bhaari thi — nayi chat bana di, purani 'OLD - {old_title}'.")
    return cid


async def resolve_company_chat(waseem, company, terms, doc_text, out_dir):
    cid, found = await waseem.find_chat(terms, company, doc_text)
    if cid and norm(company) not in chat_refresh_pending():
        # aap ka rule: purani chat Work mode / High ke ilawa effort par ho -> instructions le kar NAYI chat (Chat + High)
        try:
            await waseem.goto(f"{CHATGPT}/c/{cid}")
            if not await chat_mode_ok(waseem):
                log(f"  \"{found}\" chat Work mode / High ke baghair hai — nayi chat (Chat + High) banegi")
                mark_chat_refresh(company)
        except ChatLimit:
            raise
        except Exception as e:
            log(f"  (chat mode check: {e})")
    if cid and norm(company) in chat_refresh_pending():
        try:
            return await refresh_company_chat(waseem, company, cid, found), None
        except ChatLimit:
            raise
        except Exception as e:
            log(f"  (nayi chat nahi bani: {type(e).__name__}: {e} — purani chat se hi)")
            mark_chat_refresh(company, add=False)  # har file par 5 min zaya na hon; purani chat Chat + High par set hoti hai
            log("    " + " | ".join(traceback.format_exc().strip().splitlines()[-4:]))
    if cid:
        log(f"  Waseem mein chat mili: \"{found}\"")
        return cid, None
    log("  Waseem mein chat nahi — Wajid ki branding search kar raha hoon")
    try:
        brand = None
        try:
            instructions = await get_instructions_from_wajid(company, terms, doc_text)
        except BrandChatNotFound:
            log("  Dono profiles mein company chat nahi mili — file ki website se branding le raha hoon")
            brand = await collect_website_branding(HUB, company, doc_text, out_dir)
            instructions = brand["instructions"]
        cid = await create_waseem_chat(waseem, company, instructions, files=[brand["logo"]] if brand else ())
        return cid, brand
    except ChatLimit:
        raise
    except Exception as e:
        # kuch bhi na mila / beech mein masla -> aap ke Posts folder ke aakhri approved designs se LAZMI branding
        refs = style_refs(company)
        if not refs:
            raise
        log(f"  ({e}) — aap ke {len(refs)} aakhri approved designs se branding bana raha hoon")
        cid = await create_waseem_chat(waseem, company, CFG["refs_branding_message"].format(company=company),
                                       files=refs)
        remember_chat("waseem", company, cid, company, refs=[reference_memory_key(company, r) for r in refs])
        return cid, None


def expand_outputs(designs):
    """Har design se kitni images: post/story = 1, product store post = sirf 2 mockups (4:5), Eliquid jaisi = 1."""
    outs = []
    for d in designs:
        if d.get("product_path") and not d.get("no_mockups"):
            # product store: sirf 2 mockups (post design nahi)
            outs.append({"d": d, "type": "mockup_placed"})
            outs.append({"d": d, "type": "mockup_use"})
            continue
        outs.append({"d": d, "type": d["kind"]})
    return outs


def product_name(d):
    """Product link ke aakhri hisse se naam: .../products/double-wall-bamboo-...-bottle-550ml -> 'Double Wall Bamboo ...'."""
    url = d.get("product_url") or ""
    slug = urlparse(url).path.rstrip("/").split("/")[-1] if url else ""
    slug = re.sub(r"-(copy|\d{6,})$", "", slug)
    name = " ".join(w for w in slug.split("-") if w).title()
    return name or (d.get("content") or "the attached product").split("\n")[0][:60]


MODEL_FILE = OUT_ROOT / "model_rotation.json"


def pick_model(d):
    """Har product ka alag insaan, baari baari: Asian aurat, African mard, South Asian aurat... Ginti file mein
    yaad rehti hai -> agli file wahan se aage (pichli file wala insaan dobara nahi). Ek product = ek hi chuna hua."""
    if d.get("model"):
        return d["model"]
    rot = CFG["model_rotation"]
    try:
        data = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    n = data.get("next", 0)
    d["model"] = rot[n % len(rot)]
    data["next"] = n + 1  # baqi ginti (comparison style) bhi isi file mein — mita ni do
    try:
        OUT_ROOT.mkdir(parents=True, exist_ok=True)
        MODEL_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass
    return d["model"]


def pick_ba_style(d):
    """Comparison post ka layout baari baari (aap ke approved Aim Tech designs: inset / upar-neeche / slider).
    Ginti file mein yaad -> agli comparison agla style. Ek post = ek hi style (dobara bhejne par bhi)."""
    styles = CFG.get("before_after_comparison_styles") or []
    if not styles:
        return ""
    if "ba_style" not in d:
        try:
            data = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        n = data.get("ba_style_next", 0)
        d["ba_style"] = n % len(styles)
        data["ba_style_next"] = n + 1
        try:
            MODEL_FILE.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass
    b = d.get("ba_label", "BEFORE")
    return styles[d["ba_style"] % len(styles)].format(before_label=b, after_label="AFTER",
                                                     before_label_lc=b.lower(), after_label_lc="after")


def doc_post_count(designs, reels, done_posts):
    """Doc ki posts (carousel = 1, mockups = apni post) + reels + pehle se done — checker ki ginti."""
    keys = {d.get("section") if d.get("carousel") else d["label"] for d in designs}
    return len(keys) + len(reels) + len(done_posts)


# reel ki pehchan: "On-screen reel text", "REEL TEXT", "10-SECOND REEL TEXT:", "Reel script" (Bandz / My gym)
REEL_TEXT_RE = re.compile(
    r"^\W*(?:on[ -]screen\s+(?:reel\s+)?(?:text|script)\b|"
    r"(?:\d+\s*[- ]?\s*sec(?:ond)?s?\s+)?reel\s+(?:text|script|cover|title|hook)\b)", re.I)
LOOSE_LABEL_RE = re.compile(r"^\W*(?:post|story|reel|carousel|video)\s*#?\s*\d+\b", re.I)


def loose_post_estimate(lines):
    """Parser se alag, seedhi ginti: 'Post 3' jaisi lines aur 'Heading' / 'Caption' wali lines (jo zyada ho)."""
    text = [_clean_line(l) for l, _ in lines]
    labels = sum(1 for l in text if LOOSE_LABEL_RE.match(l))
    heads = sum(1 for l in text if HEADING_RE.match(l) and not (HEADING_RE.match(l).group("rest") or "").strip())
    caps = sum(1 for l in text if CAPTION_RE.match(l))
    return max(labels, heads, caps)


async def recheck_file(doc_id, tab_id, folder, made_sections):
    """File DONE ke 2 files baad: doc dobara parho — koi post chhoot to nahi gayi (Angela 15: 'Post 2.' miss)
    ya writer ne baad mein post to nahi daal di. Masla ho to alert (khud dobara nahi banata — duplicate na ho)."""
    try:
        lines = await fetch_doc(tab_id, doc_id)
    except Exception as e:
        log(f"  (checker: '{folder}' dobara nahi parh saka: {e})")
        return
    designs, reels, done_posts = parse_doc(lines)
    now = doc_post_count(designs, reels, done_posts)
    loose = loose_post_estimate(lines)
    worst = max(now, loose)
    if made_sections is not None and worst > made_sections:
        msg = (f"'{folder}': doc mein ab ~{worst} posts hain, bani thi {made_sections} — "
               f"{worst - made_sections} post(s) shayad miss / baad mein daali gayi. Doc dekh lo.")
        log(f"⚠ CHECKER: {msg}")
        notify("Checker: post miss?", msg)
    else:
        log(f"  Checker: '{folder}' theek — {made_sections} posts, koi miss nahi")


EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⌀-⏿"
                      "️‍⃣\U000E0020-\U000E007F]+")


def strip_emoji(text):
    """Heading mein emoji image par nahi chhapne (Single Parent / Pro Channel mein ChatGPT ne emoji bhi bana diye)."""
    return "\n".join(re.sub(r"[ \t]{2,}", " ", EMOJI_RE.sub("", line)).strip() for line in text.split("\n"))


def build_design_message(company, designs, outputs=None, already=()):
    """Message + upload files (usi number se jo message mein likha). Return (msg, files, outputs).
    already = jo files isi chat mein pehle upload ho chuki -> dobara upload nahi, sirf hawala."""
    blocks, files, outs = [], [], expand_outputs(designs) if outputs is None else outputs

    def file_no(path, fresh=False):  # product har request ke saath; branding reuse ho sakti hai
        if path in already and not fresh:
            return "from my earlier message in this chat (same image — not re-uploaded)"
        if path not in files:
            files.append(path)
        return files.index(path) + 1

    posts = stories = mockups = 0
    for o in outs:
        d, t = o["d"], o["type"]
        lines = []
        if t.startswith("mockup"):
            mockups += 1
            o.setdefault("name", f"Mockup {mockups} (4:5)")
            key = "product_mockup_placed" if t == "mockup_placed" else "product_mockup_use"
            n_prod = file_no(d["product_path"], fresh=True)
            lines.append(CFG[key].format(n=n_prod))
            # product ka naam (link se) + sirf isi message wala product — Wear Wood 10: bottle ki jagah
            # ChatGPT ne chat ke purane sunglasses / watch dobara bana diye
            lines.append(f"PRODUCT IN THIS MOCKUP: {product_name(d)} — use ONLY the product in uploaded file {n_prod} "
                         "of THIS message. NEVER show a product from earlier images in this chat (no other sunglasses, "
                         "watch or item).")
            if t == "mockup_use" and CFG.get("model_rotation"):  # har product ka apna insaan (Wear Wood)
                lines.append(CFG["model_for_mockup_note"].format(model=pick_model(d)))
            lines.append(CFG["product_mockup_rules"])
            blocks.append(f"{o['name']}:\n" + "\n".join(lines))
            continue
        if t == "story":
            stories += 1
            o.setdefault("name", f"Story {stories} (9:16)")
        else:
            posts += 1
            platform = "LinkedIn " if "LINKEDIN" in d["label"].upper() else ""
            if d.get("before_after"):  # Downloads mein: "POST 1 Before.png" / "POST 1 Comparison.png"
                tag = "Comparison" if d["before_after"] == "pair" else d["before_after"].title()
                o.setdefault("name", f"{d['section'].title()} {tag} (4:5)")
            elif d.get("carousel"):  # Downloads mein: "Carousel 1 Slide 3.png"
                o.setdefault("name", f"Carousel {d['carousel']} Slide {d['slide']} (4:5)")
            else:
                o.setdefault("name", f"{platform}Post {posts} (4:5)")
        if d.get("before_after") and d["image_path"]:
            # before/after: dono asal photos zaroori, label chhota (bara heading nahi)
            if d["before_after"] == "pair":
                lines.append(CFG["before_after_comparison_note"].format(
                    b=file_no(d["image_path"]), a=file_no(d["after_path"]), before_label=d.get("ba_label", "BEFORE"),
                    style=pick_ba_style(d)))
            else:
                lines.append(CFG["before_after_slide_note"].format(
                    k=d["slide"], n=file_no(d["image_path"]), label=d["before_after"]))
            if d.get("ba_heading"):
                lines.append(CFG["before_after_heading_note"] + "\n" + strip_emoji(d["ba_heading"]) + "\nEND OF EXACT HEADING")
            if d.get("website_brand"):
                brand = d["website_brand"]
                lines.append(f"Uploaded file {file_no(brand['logo'])} is the verified COMPANY LOGO. Use it exactly; it is not the main photo.")
                lines.append(brand["instructions"])
            blocks.append(f"{o['name']}:\n" + "\n".join(lines))
            continue
        if d.get("carousel"):
            lines.append(CFG["carousel_note"].format(k=d["slide"]))
        if d.get("ig_shot"):
            nums = [file_no(p) for p in d["ig_shot"]]
            n = str(nums[0]) if len(nums) == 1 else f"{nums[0]} to {nums[-1]} (all slides, in order)"
            lines.append(CFG["instagram_note"].format(n=n, company=company))
        if d.get("text_from_image") and d.get("image_path") and not d["content"]:
            lines.append(CFG["text_from_image_note"].format(n=file_no(d["image_path"])))
        if d.get("product_path"):
            lines.append(CFG["product_photo_note"].format(n=file_no(d["product_path"], fresh=True)))
            lines.append("Use ONLY the product attached to THIS message. Do not combine it with products from earlier messages or style references.")
        if d["image_path"]:
            lines.append(CFG["given_photo_note"].format(n=file_no(d["image_path"])))
            if "_screenshot" in Path(d["image_path"]).stem:  # Envato download nahi hua -> preview ka screenshot
                lines.append(CFG["screenshot_photo_note"])
        elif d.get("ig_shot"):
            if CFG.get("instagram_no_photo_note"):  # khaali -> instagram_note hi kaafi (relevant image wahi kehta hai)
                lines.append(CFG["instagram_no_photo_note"])
        elif d.get("image_prompt"):
            # writer ne image nahi, image ka PROMPT diya -> ChatGPT khud image banaye (brand style mein)
            lines.append(CFG["image_prompt_note"] + "\n" + d["image_prompt"] + "\nEND OF IMAGE DIRECTION")
        elif not d.get("product_path"):
            lines.append(CFG["brief_photo_note"] if d.get("brief") else CFG["no_photo_note"])
        if d.get("brief"):
            lines.append(CFG["brief_note"])
        if d["content"] and not d.get("brief"):
            lines.append("EXACT HEADING TO PRINT ON THE IMAGE (every line is required; do not shorten, paraphrase, omit or replace):\n"
                         + strip_emoji(d["content"]) + "\nEND OF EXACT HEADING")
        elif d["content"]:
            lines.append(d["content"])
        if d.get("ref_text"):
            lines.append(CFG["reference_note"] + "\n" + d["ref_text"])
        if d.get("website_brand"):
            brand = d["website_brand"]
            lines.append(f"Uploaded file {file_no(brand['logo'])} is the verified COMPANY LOGO. Use it exactly; it is not the main photo.")
            lines.append(brand["instructions"])
        blocks.append(f"{o['name']}:\n" + "\n".join(lines))
    rules = "\n".join("- " + r.format(company=company, count=len(outs)) for r in CFG["design_rules"])
    rules += "\n- Heading:, Caption:, Links:, output names, attachment numbers and heading boundary markers are instructions/metadata ONLY. NEVER print these labels or instructions on the image. Print ONLY the writer's exact heading content inside the heading block. Captions are not image text."
    if any(o["d"].get("before_after") for o in outs):
        if any(o["d"].get("before_after") == "pair" for o in outs):
            rules = rules.replace("NEVER create a collage", "Except the before/after comparison, NEVER create a collage", 1)
        rules += "\n- " + CFG["before_after_rule"]
    # real models wali companies (Wear Wood): har post mein naya chehra / naya insaan (2 files baad wahi face aa gaya tha)
    if CFG.get("model_variety_note") and (is_mockup_company(company) or any(
            norm(x) in norm(company) for x in CFG.get("model_variety_companies", []))):
        rules += "\n- " + CFG["model_variety_note"]
    # config.json "company_notes": {"aim tech": "..."} — sirf us company ke har message mein
    for key, note in CFG.get("company_notes", {}).items():
        if note and norm(key) in norm(company):
            rules += "\n- " + note.format(company=company)
    return "\n\n".join(blocks) + "\n\n" + rules, files, outs


def who(o):
    """Summary ke liye post ki pehchan: doc ka post number + script ka naam + heading.
    'Post 35 [Post 2] «Liquid BBL Aftercare…»' — taake foran pata chale KAUN si post."""
    d = o["d"]
    label = d.get("section") or d["label"]
    if d.get("carousel") and d.get("slide"):
        label += f" slide {d['slide']}"
    short = o.get("name", "").split(" (")[0]
    if short and norm(short) != norm(label):
        label += f" [{short}]"
    if o["type"].startswith("mockup"):
        label += " mockup"
    head = re.sub(r"[:\s]+", " ", (d.get("content") or "").split("\n")[0]).strip()
    if not head and d.get("ig_shot"):
        head = "Instagram post"
    return f"{label} «{head[:45]}{'…' if len(head) > 45 else ''}»" if head else label


def drop_duplicate(o, outs):
    """Is file ki kisi doosri image jaisi hoobahoo (same bytes) -> yeh post bani hi nahi: file hatao, False/True."""
    import hashlib
    digest = lambda p: hashlib.md5(Path(p).read_bytes()).hexdigest()
    mine = digest(o["file"])
    for other in outs:
        if other is not o and other.get("file") and Path(other["file"]) != Path(o["file"]) \
                and Path(other["file"]).exists() and digest(other["file"]) == mine:
            log(f"  ✘ {o['name']}: yeh {other['name']} wali image hi hai (ChatGPT ne dobara di) — nahi gini")
            HUB.stuck_events += 1
            try:
                Path(o["file"]).unlink()
            except Exception:
                pass
            o.pop("file", None)
            return True
    return False


COMPANY_LOG = OUT_ROOT / "company_log.json"


def doc_items_logged(doc_id):
    """Is file ki kitni posts pehle hi (pichle hafte) ginti mein aa chuki."""
    try:
        data = json.loads(COMPANY_LOG.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return sum(e.get("items", 3) for e in data if doc_id and e.get("doc") == doc_id)


def company_items(company, hours=None):
    """Pichle 12 ghante (limit_window_hours) mein is company ki kitni posts bani — reels bhi ginti mein (aap banate ho magar din ki
    6 mein shamil). Story alag. Purane record (items nahi) = 3 (ek file)."""
    try:
        data = json.loads(COMPANY_LOG.read_text(encoding="utf-8"))
    except Exception:
        return 0
    cut = (datetime.now() - _timedelta(hours=hours or CFG.get("limit_window_hours", 12))).isoformat()
    return sum(e.get("items", 3) for e in data if e.get("company") == norm(company) and e.get("at", "") >= cut)


def day_items():
    return CFG.get("company_max_posts_per_day", 6)


def log_company_unit(company, doc_id, title, part=None, items=3):
    try:
        data = json.loads(COMPANY_LOG.read_text(encoding="utf-8"))
    except Exception:
        data = []
    cut = (datetime.now() - _timedelta(days=7)).isoformat()
    data = [e for e in data if e.get("at", "") >= cut]  # sirf pichla hafta
    if any(e.get("doc") == doc_id and e.get("part") == part for e in data):
        return  # isi file / set ki dobara koshish (adhoori file) -> do dafa nahi ginni (My gym 5 do dafa gini gayi)
    data.append({"company": norm(company), "doc": doc_id, "file": title, "part": part, "items": items,
                 "at": datetime.now().isoformat()})
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    COMPANY_LOG.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def file_sets(outs, per=None):
    """3 se zyada posts wali file -> sets: [("1-3", outs), ("4-6", outs), ("7-10", outs)].
    Carousel = 1 post (saari slides ek set mein). Story usi set mein jis post ke baad doc mein aati hai.
    Aakhri set mein akeli 1 post na bache -> pichle set mein (7-10). 3 ya kam posts -> [(None, outs)]."""
    per = per or CFG.get("posts_per_pass", 3)
    units, order = {}, []
    for o in outs:
        # carousel = 1 post; ek product ke dono mockups = 1 post (Wear Wood 10: 3 product ke mockups 3-3 mein bant gaye)
        key = ("c", o["d"]["carousel"]) if o["d"].get("carousel") else             ("m", id(o["d"])) if str(o.get("type", "")).startswith("mockup") else id(o)
        if key not in units:
            units[key] = []
            order.append(key)
        units[key].append(o)
    # items = posts + reels (doc ki tarteeb mein). Reel set mein jagah leti hai — banti nahi (aap ka kaam)
    items, seen_d = [], set()
    for k in order:
        d = units[k][-1]["d"]
        if id(d) in seen_d:
            continue
        seen_d.add(id(d))
        if not items:
            items += [("reel", j) for j in range(d.get("reels_before", 0))]
        if units[k][0]["type"] != "story":
            items.append(k)
        items += [("reel", k, j) for j in range(d.get("reels_after", 0))]
    for k in order:  # same design ke doosre unit (product post + uske mockups) usi post ke saath
        if units[k][0]["type"] != "story" and k not in items:
            items.insert(items.index(next(x for x in order if units[x][-1]["d"] is units[k][-1]["d"])) + 1, k)
    posts = items
    if len(posts) <= per:
        return [(None, list(outs), len(posts))]
    groups = [posts[i:i + per] for i in range(0, len(posts), per)]
    if len(groups) > 2 and len(groups[-1]) == 1:
        tail = groups.pop()
        groups[-1] += tail
    where, n = {}, 0
    for gi, g in enumerate(groups):
        for k in g:
            where[k] = (n, gi)
            n += 1
    last = 0
    for k in order:  # story -> pichli post ka set
        if k in where:
            last = where[k][1]
        else:
            where[k] = (None, last)
    sets = []
    for gi, g in enumerate(groups):
        a, b = posts.index(g[0]) + 1, posts.index(g[-1]) + 1
        label = f"{a}-{b}" if a != b else str(a)
        sets.append((label, [o for k in order if where[k][1] == gi for o in units[k]], len(g)))
    return sets


async def generate_and_download(waseem, cid, company, designs, out_dir, summary):
    # Enforce company policy here too, including callers that bypass process_doc.
    if not is_mockup_company(company):
        designs = [dict(d, no_mockups=True) for d in designs]
    msg, files, outs = build_design_message(company, designs)
    (out_dir / "chatgpt_message.txt").write_text(msg, encoding="utf-8")  # jo ChatGPT ko gaya
    retry_limit = CFG.get("limit_max_retries", 1)
    limit_hit = None
    names = {id(o): f"{n:02d}_{safe_name(o['name'])}" for n, o in enumerate(outs, 1)}
    # RESUME: is file ki jo images pichli dafa ban chuki (bijli/net/stop se pehle) -> dobara nahi banani
    doc_key = summary.get("doc_id")
    made_before = load_progress().get(doc_key, {}) if doc_key else {}
    for o in outs:
        p = made_before.get(o["name"])
        if p and Path(p).exists():
            o["file"] = Path(p)
    already_made = sum(1 for o in outs if o.get("file"))
    if already_made:
        log(f"  {already_made} image(s) pichli dafa ban chuki thin — wahan se aage (dobara nahi bana raha)")
    # 3 se zyada posts (Dental 10 posts): ek dafa mein sirf ek set (1-3 + story), phir 2 aur companies,
    # phir agla set (4-6) — ChatGPT ek chat mein lagataar atak jata hai
    sets = file_sets(outs) if not summary.get("only_post") else [(None, outs, 0)]
    if PART_RE.search(summary.get("file", "")):  # bari file ka hissa (nayi doc "... (7-10)") = poori ek file
        sets = [(None, outs, sum(x[2] for x in sets))]
    need = sets[0][2]
    if len(sets) > 1:
        todo_sets = [x for x in sets if any(not o.get("file") for o in x[1])]
        if todo_sets:
            lbl, outs, need = todo_sets[0]
            summary["part"], summary["parts_left"] = lbl, len(todo_sets) - 1
            log(f"  Bari file ({len(sets)} sets: {', '.join(x[0] for x in sets)}) — is dafa sirf posts {lbl}"
                + (f"; baqi {len(todo_sets) - 1} set(s) doosri companies ke baad" if len(todo_sets) > 1 else " (aakhri set)"))
    summary["items"] = need
    # isi file ki chhooti images dobara (Wear Wood 10 ki bottle): posts pehle hi gini ja chuki -> had nahi, dobara ginti nahi
    if doc_items_logged(summary.get("doc_id")) >= sum(x[2] for x in sets):
        summary["redo"] = True
    used = company_items(company) if not summary.get("only_post") and not summary.get("redo") else 0
    if used and used + need > day_items():  # company ki aaj ki 6 posts (reel samait) puri -> agle din
        raise DayLimit(company, need)

    async def send_with_limit_retry(message, files):
        for attempt in range(retry_limit + 1):
            try:
                try:
                    return await waseem.send(message, files)
                except AttachFailed as e:
                    # image nahi lagi -> page saaf (dobara khol kar) ek dafa phir koshish
                    log(f"  ({e} — chat dobara khol kar phir koshish)")
                    await waseem.goto(f"{CHATGPT}/c/{cid}")
                    return await waseem.send(message, files)
            except ChatLimit as e:
                if attempt == retry_limit:
                    raise
                mins = CFG.get("limit_wait_minutes", 2)
                log(f"  {e} — {mins} minute baad retry ({attempt + 1}/{retry_limit})")
                notify("Waseem ChatGPT limit", f"{e}. {mins} minute baad retry.", sticky=False)
                await run_sleep(mins * 60)
                await waseem.goto(f"{CHATGPT}/c/{cid}")

    group = []  # batch mein gayi images (limit report ke liye)
    # bari file (10 posts / carousels — regular 3 post + 1 story se zyada): har message ke baad ChatGPT ko
    # aaram (3 min) + hard refresh, phir agla set — warna 'too many requests' / Try again / atak jata hai
    big_file = len([o for o in outs if not o.get("file")]) > CFG.get("rest_after_images", 4)
    sent_once = []

    async def rest_break(carousel=False, single=False):
        # carousel / alag message (story, chhooti post): pehle message ke baad hamesha aaram + hard refresh
        # (jaldi jaldi requests -> ChatGPT 'Try again' / garbar)
        if sent_once and (big_file or carousel or single):
            mins = CFG.get("rest_between_sets_minutes", 3) if big_file else CFG.get("rest_before_carousel_minutes", 1)
            await chatgpt_rest(mins, "bari file — agla set hard refresh ke baad" if big_file
                               else "carousel se pehle, phir hard refresh" if carousel
                               else "agle message se pehle, phir hard refresh")
            try:
                await waseem.goto(f"{CHATGPT}/c/{cid}")
            except Exception as e:
                log(f"  (refresh: {e})")
        sent_once.append(1)
    uploaded = {d["website_brand"]["logo"] for d in designs if d.get("website_brand")}
    refs = style_refs(company)
    mem = CHAT_MEMORY.get("waseem", {}).get(norm(company)) or {}
    sent_before = set(mem.get("refs", [])) if mem.get("id") == cid else set()
    old_refs = [r for r in refs if reference_memory_key(company, r) in sent_before]  # is chat mein pehle ja chuki -> dobara upload nahi
    uploaded |= set(old_refs)
    if refs:
        log(f"  Style reference: {len(refs)} aap ke approved designs"
            + (f" ({len(old_refs)} pehle se chat mein, dobara upload nahi)" if old_refs else ""))
    new_refs_all = [r for r in refs if r not in uploaded]
    if new_refs_all:
        # references ALAG message mein (generation message mein products ke saath milkar ek image na ban jaye)
        try:
            n = "1" if len(new_refs_all) == 1 else f"1 to {len(new_refs_all)}"
            await send_with_limit_retry(style_reference_message(company, n), new_refs_all)
            await waseem.wait_done(CFG["timeouts_minutes"]["text_reply"])
            uploaded |= set(new_refs_all)
            remember_chat("waseem", company, cid, mem.get("title") or company, refs=[reference_memory_key(company, r) for r in new_refs_all])
            log("  Style reference alag message mein bhej diye")
        except Exception as e:
            log(f"  (style reference nahi gaye: {e} — un ke baghair)")
            refs = []

    switched = []

    async def maybe_switch_chat():
        """File ke beech ChatGPT baar baar atka (3 dafa: 10-min hang / Try again / wahi image) -> chat bhaari:
        FAURAN nayi chat (instructions + style refs) aur baqi images wahin. Purani 'OLD - ...'."""
        nonlocal cid
        if switched or HUB.stuck_events < CFG.get("chat_switch_mid_file", 3) or summary.get("only_post"):
            return
        switched.append(1)
        try:
            cid = await refresh_company_chat(waseem, company, cid, company)
        except ChatLimit:
            raise
        except Exception as e:
            log(f"  (beech mein nayi chat nahi bani: {type(e).__name__}: {e} — isi chat mein jaari)")
            return
        HUB.stuck_events = 0
        uploaded.clear()  # nayi chat mein photos / logo dobara lagenge
        uploaded.update(d["website_brand"]["logo"] for d in designs if d.get("website_brand"))
        if refs:
            try:
                n = "1" if len(refs) == 1 else f"1 to {len(refs)}"
                await send_with_limit_retry(style_reference_message(company, n), refs)
                await waseem.wait_done(CFG["timeouts_minutes"]["text_reply"])
                uploaded.update(refs)
                remember_chat("waseem", company, cid, company, refs=[reference_memory_key(company, r) for r in refs])
            except Exception as e:
                log(f"  (nayi chat mein style reference nahi gaye: {e})")
        log("  Baqi images nayi chat mein")

    # BATCH (aap ka apna tareeqa — ChatGPT ki image limit kam lagti hai):
    #   posts 3-3 ke sets mein (max_posts_per_message), saari stories EK message, har carousel EK message (saari slides),
    #   saare product mockups EK message (5 products = 10 images). Ek message mein max 10 images.
    #   Jo image batch mein na bane -> neeche alag message mein (koi image chhootti nahi).
    if CFG.get("batch_mode", True) and not limit_hit:
        cap = max(1, CFG.get("max_images_per_message", 10))
        todo = [o for o in outs if not o.get("file")]
        kinds = {}
        for o in todo:
            d = o["d"]
            if o["type"].startswith("mockup"):
                key = "mockups"
            elif d.get("carousel"):
                key = f"carousel{d['carousel']}"
            elif d.get("product_path") or d.get("before_after") == "pair":
                continue  # product wali post / before-after comparison: apni photos ke saath alag message
            elif o["type"] == "story":
                continue  # story hamesha akeli, apne alag message mein (9:16)
            else:
                key = "posts"
            kinds.setdefault(key, []).append(o)
        batches = []
        # carousel aakhir mein (posts pehle) -> carousel se pehle aaram + hard refresh
        kinds = dict(sorted(kinds.items(), key=lambda kv: kv[0].startswith("carousel")))
        for key, items in kinds.items():
            if key.startswith("carousel"):
                batches.append((key, items))  # carousel ki SAARI slides EK message mein (10 ki had nahi)
                continue
            size = cap - cap % 2 if key == "mockups" else cap  # ek product ke dono mockups ek hi batch mein
            # 3 se zyada posts (regular file 3 post + 1 story) -> 3, 3, 3, 1 sets; ChatGPT 3 se zyada par atakta hai
            if key == "posts" and len(items) >= CFG.get("split_posts_from", 4):
                size = max(1, min(size, CFG.get("max_posts_per_message", 3)))
            batches += [(key, items[i:i + size]) for i in range(0, len(items), size)]
        for key, batch in batches:
            if limit_hit:
                break
            if len(batch) == 1:
                continue  # sirf ek image -> neeche normal single message
            carousel = key.startswith("carousel")
            if any(not o.get("file") for o in batch):
                await maybe_switch_chat()
                await rest_break(carousel)
            try:
                # carousel skip nahi hota: jo slides na banen -> usi carousel ka "continue" message (2 dafa tak)
                for attempt in range(1 + (CFG.get("carousel_extra_tries", 2) if carousel else 0)):
                    todo_b = [o for o in batch if not o.get("file")]
                    if not todo_b:
                        break
                    if attempt:  # 'Try again' / slides chhoot gayin -> fauran dobara nahi: aaram + hard refresh
                        await maybe_switch_chat()  # bohot atka -> baqi slides nayi chat mein
                        await chatgpt_rest(CFG.get("rest_before_retry_minutes", 2), "baqi slides se pehle, phir hard refresh")
                        try:
                            await waseem.goto(f"{CHATGPT}/c/{cid}")
                        except Exception as e:
                            log(f"  (refresh: {e})")
                    before = set(await page_image_refs(waseem, cid))
                    msg_b, files_b, _ = build_design_message(company, designs, outputs=todo_b, already=uploaded)
                    if refs:
                        msg_b += "\n\n" + CFG["style_ref_again"].format(company=company)
                    order = ", ".join(o["name"] for o in todo_b)
                    if key == "mockups":
                        n_prod = len({id(o["d"]) for o in todo_b})
                        head = CFG["batch_mockups_note"].format(n=len(todo_b), products=n_prod)
                    elif carousel and attempt:
                        head = CFG["batch_carousel_continue_note"].format(n=len(todo_b), total=len(batch))
                    elif carousel and todo_b[0]["d"].get("ig_album"):  # Instagram album: saare pages ke screenshots
                        head = CFG["instagram_album_note"].format(n=len(todo_b), company=company)
                    elif carousel:
                        head = CFG["batch_carousel_note"].format(n=len(todo_b))
                    else:
                        head = CFG["batch_posts_note"].format(n=len(todo_b), what=("stories" if key == "stories" else "posts"),
                                                              size=("9:16" if key == "stories" else "4:5"))
                    msg_b = head + "\n\n" + msg_b + "\n\n" + CFG["batch_order_note"].format(n=len(todo_b), order=order)
                    if carousel and todo_b[0]["d"].get("ig_album"):
                        # Instagram album: aap ka apna chhota message — lambi hidayat se ChatGPT ulajhta tha
                        # (slides dobara, Python se slides, ghanta bhar)
                        msg_b = CFG["instagram_album_short"].format(n=len(todo_b), company=company)
                        # aap ka tareeqa: screenshot nahi, har slide ka TEXT (screenshot ki branding copy na ho)
                        shots = [s for o in todo_b for s in (o["d"].get("ig_shot") or [])[:1]]
                        try:
                            texts = [await asyncio.to_thread(slide_text, s) for s in shots]
                        except Exception as e:
                            texts = []
                            log(f"  (slide OCR nahi chala: {e} — screenshots bhej raha hoon)")
                        if len(texts) == len(todo_b) and all(len(t.split()) >= 3 for t in texts):
                            for o, t in zip(todo_b, texts):
                                o["expect_text"] = t
                            msg_b = CFG["instagram_album_text"].format(n=len(todo_b), company=company) + "\n\n" + \
                                "\n\n".join(f"Slide {k}:\n{t}" for k, t in enumerate(texts, 1))
                            files_b = [f for f in files_b if f not in set(shots)]
                            log(f"  Carousel: {len(todo_b)} slides ka text (OCR) bheja — screenshots nahi")
                        elif texts:
                            log("  (kisi slide ka text OCR se nahi nikla — is carousel ke screenshots bhej raha hoon)")
                        if attempt:
                            msg_b = CFG["batch_carousel_continue_note"].format(n=len(todo_b), total=len(batch)) \
                                + "\n\n" + msg_b
                    tag = f"_retry{attempt}" if attempt else ""
                    (out_dir / f"chatgpt_message_{names[id(todo_b[0])]}_batch{tag}.txt").write_text(msg_b, encoding="utf-8")
                    log(f"  Waseem: {len(todo_b)} images ({key}) EK message mein" + (f" (baqi slides, koshish {attempt + 1})" if attempt else ""))
                    sent_at = await send_with_limit_retry(msg_b, files_b)
                    for o in todo_b:
                        o["tries"] = o.get("tries", 0) + 1
                    uploaded |= set(files_b)
                    group += [o for o in todo_b if all(o is not g for g in group)]
                    ids = await wait_for_images(waseem, cid, before, len(todo_b), sent_at=sent_at)
                    if len(ids) > len(todo_b):
                        ids = ids[-len(todo_b):]
                    for o, fid in zip(todo_b, ids):  # ChatGPT aam tor par order mein banata hai (neeche OCR se check)
                        try:
                            o["file"] = await download_generated(waseem, cid, fid, out_dir / names[id(o)])
                        except Exception as e:  # ek image save na ho -> poori file fail nahi; woh alag message mein
                            log(f"  ✘ {o['name']}: image save nahi hui ({e or type(e).__name__}) — alag message mein dobara")
                            o.pop("file", None)
                            continue
                        drop_duplicate(o, outs)  # wahi image dobara -> yeh post nahi bani, alag message mein
                    if carousel:
                        for o in todo_b:  # writer ki carousel: slide ka apna heading
                            if not o.get("expect_text") and not o["d"].get("ig_shot"):
                                o["expect_text"] = o["d"].get("content") or ""
                        await reorder_by_text(todo_b, out_dir, names)
                    for o in todo_b:
                        if o.get("file"):
                            save_progress(doc_key, o["name"], o["file"])
                    if len(ids) < len(todo_b):
                        log(f"  {len(todo_b) - len(ids)} image batch mein nahi bani — "
                            + ("usi carousel ka agla message" if carousel else "woh alag message mein banwa raha hoon"))
            except AttachFailed as e:
                log(f"  ({e} — yeh images alag alag message mein)")
                await waseem.goto(f"{CHATGPT}/c/{cid}")
            except ChatLimit as e:
                limit_hit = str(e)
                log(f"  {e} — is file ki baqi images pending")
                notify("ChatGPT limit", f"{company}: {e}. Baqi images pending.", sticky=False)
    singles = [o for o in outs if not o.get("file")]
    if singles:
        log(f"  Waseem: {len(singles)} image(s) alag alag message mein")
    max_tries = CFG.get("max_tries_per_image", 2)
    for output in singles:
        if limit_hit:  # limit retry ke baad bhi -> baqi images pending (jo bani hain woh save hongi)
            summary.setdefault("image_fail", []).append(f"{who(output)}: ChatGPT limit — nahi bani")
            continue
        if output.get("tries", 0) >= max_tries:  # aap ka rule: 2 dafa nahi bani -> khud banao, aage chalo
            continue
        await maybe_switch_chat()
        await rest_break(single=True)
        try:
            before = set(await page_image_refs(waseem, cid))
            single_msg, single_files, _ = build_design_message(company, designs, outputs=[output], already=uploaded)
            # purane approved designs (style_refs folder): pehle message ke saath ek dafa upload, phir hawala
            if refs:  # references pehle alag message mein ja chuke -> sirf hawala
                single_msg += "\n\n" + CFG["style_ref_again"].format(company=company)
            if output["d"].get("before_after") == "pair":  # comparison khud dono photos ek image mein hai
                single_msg += ("\n\nGenerate ONLY the single comparison image requested in THIS message. "
                               "Do not repeat previous images.")
            else:
                single_msg += ("\n\nGenerate ONLY the single image requested in THIS message. Do not generate other mockups "
                               "or repeat previous images. One scene, one standalone file; no panels or collage.")
            (out_dir / f"chatgpt_message_{names[id(output)]}.txt").write_text(single_msg, encoding="utf-8")
            sent_at = await send_with_limit_retry(single_msg, single_files)
            output["tries"] = output.get("tries", 0) + 1
            uploaded |= set(single_files)
            ids = await wait_for_images(waseem, cid, before, 1, sent_at=sent_at,
                                        retry_request=lambda: send_with_limit_retry(single_msg, single_files))
            if not ids and output["d"].get("carousel") and output["tries"] < max_tries:  # slide: ek aur koshish (2 tak)
                log(f"  {output['name']}: carousel slide nahi bani — ek aur dafa bhej raha hoon")
                before = set(await page_image_refs(waseem, cid))
                sent_at = await send_with_limit_retry(single_msg, single_files)
                output["tries"] += 1
                ids = await wait_for_images(waseem, cid, before, 1, sent_at=sent_at,
                                            retry_request=lambda: send_with_limit_retry(single_msg, single_files))
            if not ids:
                summary.setdefault("image_fail", []).append(f"{who(output)}: ChatGPT ne image nahi banayi")
                log(f"  ✘ {output['name']}: image nahi bani — agli image par ja raha hoon")
                continue
            if len(ids) > 1:  # ChatGPT ne 2 variants de diye -> aakhri wali (sab se nayi) lo
                log(f"  ({output['name']}: ChatGPT ne {len(ids)} images di — aakhri wali li)")
            # har image foran save (baad mein limit lag jaye to bhi yeh mehfooz)
            try:
                output["file"] = await download_generated(waseem, cid, ids[-1], out_dir / names[id(output)])
            except Exception as e:
                summary.setdefault("image_fail", []).append(f"{who(output)}: image bani magar save nahi hui ({e or type(e).__name__})")
                log(f"  ✘ {output['name']}: image save nahi hui — agli image")
                continue
            if drop_duplicate(output, outs):
                summary.setdefault("image_fail", []).append(f"{who(output)}: ChatGPT ne pichli image hi dobara di")
                continue
            save_progress(doc_key, output["name"], output["file"])  # bijli jaye to bhi yaad rahe
        except AttachFailed as e:
            summary.setdefault("image_fail", []).append(f"{who(output)}: image attach nahi hui ({e})")
            log(f"  ✘ {output['name']}: image attach nahi hui — bina image ke nahi bheja, agli par")
            await waseem.goto(f"{CHATGPT}/c/{cid}")  # adhoora message box saaf
        except ChatLimit as e:
            limit_hit = str(e)
            log(f"  {e} — is file ki baqi images pending; jo bani hain woh save hongi")
            notify("ChatGPT limit", f"{company}: {e}. Baqi images pending, agli file par ja raha hoon.", sticky=False)
            summary.setdefault("image_fail", []).append(f"{who(output)}: ChatGPT limit — nahi bani")
    if limit_hit:
        for o in group:
            if not o.get("file"):
                summary.setdefault("image_fail", []).append(f"{who(o)}: ChatGPT limit — nahi bani")
    else:
        for o in outs:
            if not o.get("file") and o.get("tries", 0) >= max_tries:
                tag = who(o)
                summary["image_fail"] = [x for x in summary.get("image_fail") or [] if not str(x).startswith(tag)]
                summary.setdefault("manual_todo", []).append(f"{tag}: {o['tries']} dafa try ke baad bhi nahi bani")
                log(f"  ✘ {o['name']}: {o['tries']} dafa nahi bani — aap khud banao (file aage chalegi)")

    out_dir.mkdir(parents=True, exist_ok=True)
    qa_dir = out_dir / "_rejected"  # ghalat nikli images (sirf record ke liye)
    results = [o for o in outs if o.get("file")]  # har image apni hi post se judi (index se nahi)
    if not results:
        if limit_hit:
            raise ChatLimit(limit_hit)
        raise RuntimeError("ChatGPT ne koi image nahi banayi")
    log(f"  {len(results)} images download ho gayin — ab check (max {CFG['qa']['max_seconds']}s)")

    # ---- QA (1 minute max, sirf text): ghalat -> EK dafa dobara; phir bhi ghalat -> aap ko batao ----
    pending = [] if limit_hit else list(results)
    for attempt in range(CFG["qa"]["max_retries"] + 1):
        if not pending:
            break
        bad, unverified = await qa_batch(waseem, pending, company)
        for note in unverified:
            log(f"  (QA: {note} — image save ho rahi hai, aap khud dekh lena)")
            summary.setdefault("qa_unverified", []).append(note)
        if not bad:
            break
        for o, issues in bad:
            log(f"  ✘ {o['name']} ghalat: {'; '.join(issues)}")
            qa_dir.mkdir(exist_ok=True)
            shutil.copy(o["file"], qa_dir / f"{o['file'].stem}_try{attempt + 1}{o['file'].suffix}")
        if attempt == CFG["qa"]["max_retries"]:
            for o, issues in bad:  # 2 dafa ghalat -> aap khud banaoge
                summary.setdefault("qa_fail", []).append(f"{who(o)}: {'; '.join(issues)}")
                notify(f"2 dafa ghalat: {o['name']}", f"{company}: {'; '.join(issues)[:250]} — aap khud bana lena", sticky=False)
                o["qa_fail"] = True
            break
        # company chat (tab khuli hai) -> sirf ghalat wali dobara
        if await waseem.conv_id() != cid:
            await waseem.goto(f"{CHATGPT}/c/{cid}")
        pending = []
        for o, issues in bad:
            try:
                before = set(await page_image_refs(waseem, cid))
                retry_msg, retry_files, _ = build_design_message(company, designs, outputs=[o], already=uploaded)
                retry_msg += "\n\n" + CFG["qa"]["regenerate_message"].format(name=o["name"], issues="; ".join(issues))
                sent_at = await send_with_limit_retry(retry_msg, retry_files)
                ids = await wait_for_images(waseem, cid, before, 1, sent_at=sent_at,
                                            retry_request=lambda: send_with_limit_retry(retry_msg, retry_files))
            except (ChatLimit, AttachFailed) as e:
                ids = []
                log(f"  {e}")
            if ids:
                o["file"] = await download_generated(waseem, cid, ids[-1], out_dir / f"{o['file'].stem}")
                pending.append(o)
            else:
                summary.setdefault("qa_fail", []).append(f"{who(o)}: ghalat bani, dobara nahi bani")
                o["qa_fail"] = True
    for o in results:
        if not o.get("qa_fail"):
            log(f"  ✔ {o['name']}")

    # ---- final images -> Downloads\<date>\<company>\ (sirf images, stories bhi) ----
    # bari file ka set -> folder "10 (1-3)" (file number + set, andar sub-folder nahi)
    final = final_dir(company, summary.get("file", "") + (f" ({summary['part']})" if summary.get("part") else ""))
    for o in results:
        if o.get("file") and not o.get("qa_fail"):
            dest = final / f"{o['name'].split(' (')[0]}{o['file'].suffix}"
            n, same = 2, False
            while dest.exists():  # same din same company ki doosri file -> naam mein number
                if dest.read_bytes() == o["file"].read_bytes():
                    same = True  # wahi image pehle se (resume / dobara) -> "(2)" copy nahi
                    break
                dest = final / f"{o['name'].split(' (')[0]} ({n}){o['file'].suffix}"
                n += 1
            if not same:
                shutil.copy(o["file"], dest)
    # Downloads mein SIRF ChatGPT ki final images (content/summary/records sab Output mein rehte hain)
    try:
        if not any(final.iterdir()):
            final.rmdir()  # koi image nahi -> khaali folder na rahe
    except Exception:
        pass
    log(f"  Final images: {final}")
    summary["final_dir"] = str(final)
    summary["made"] = sum(1 for o in results if o.get("file") and not o.get("qa_fail"))


def is_mockup_company(company):
    """Product links par 2 mockups sirf in companies ke liye (config: mockup_companies, e.g. Wear Wood)."""
    key = norm(company).replace(" ", "")
    return any(norm(x).replace(" ", "") in key for x in CFG.get("mockup_companies", []))


def reference_store():
    return BASE / "company_references.json"


def custom_reference_entry(company):
    company = company_alias(company) or company
    try:
        return company_references.load(reference_store()).get(company_references.company_key(company))
    except Exception:
        return None


def reference_memory_key(company, path):
    """Chat memory mein ref ki pehchan: custom = poora path (alag folders mein '1.jpg' do baar ho sakta hai)."""
    return f"custom:{Path(path).resolve()}" if custom_reference_entry(company) else Path(path).name


def style_reference_message(company, n):
    message = CFG["style_ref_message"].format(n=n, company=company)
    if custom_reference_entry(company):
        message += ("\nThese are the owner's selected references. Use ONLY this reference set for visual style; "
                    "disregard previous style reference images and generated designs. Keep the company's written branding instructions.")
    return message


def dashboard_references(company=None, paths=None):
    try:
        if company is not None:
            company = company_alias(company) or company
            company_references.save(reference_store(), company, json.loads(paths))
            CUSTOM_REF_PICK.pop(norm(company), None)
        entries = company_references.load(reference_store())
        names = {entry["company"] for entry in entries.values()}
        names.update(CFG.get("company_aliases", {}).values())
        for chats in CHAT_MEMORY.values():
            names.update(chats.keys())
        for root in CFG.get("style_refs_dirs", ["style_refs"]):
            root = Path(root) if Path(root).is_absolute() else BASE / root
            if root.is_dir():
                names.update(p.name for p in root.iterdir() if p.is_dir())
        return {"ok": True, "entries": entries, "companies": sorted(names, key=str.casefold),
                "message": "References save ho gaye. Agli file se apply honge."}
    except (ValueError, OSError) as error:
        return {"ok": False, "message": str(error)}


def dashboard_open_folder(folder):
    """Downloads / final images folder Windows Explorer mein kholo — sirf Downloads / Output ke andar."""
    import os
    if not folder:
        return {"ok": False, "message": "Folder ka path nahi mila"}
    path = Path(folder).expanduser()
    if not path.is_absolute():
        return {"ok": False, "message": "Folder ka poora path chahiye"}
    try:
        path = path.resolve()
    except OSError:
        return {"ok": False, "message": "Folder path resolve nahi hua"}
    roots = [Path.home() / "Downloads", OUT_ROOT]
    if CFG.get("final_dir"):
        roots.append(Path(CFG["final_dir"]).expanduser())
    if not any(path.is_relative_to(r.resolve()) for r in roots):
        return {"ok": False, "message": "Sirf Downloads / output folder khol sakte hain"}
    if not path.is_dir():
        return {"ok": False, "message": "Folder nahi mila — shayad move ya delete ho gaya hai"}
    try:
        os.startfile(str(path), "explore")
    except OSError as e:
        return {"ok": False, "message": f"Explorer nahi khula: {e}"}
    return {"ok": True, "message": "Folder Windows Explorer mein khol diya"}


CUSTOM_REF_PICK = {}  # norm(company) -> (file, [paths]) — ek file mein har jagah WAHI random set


def style_refs(company):
    """Aap ke apne approved designs (D:/Azhar/October/Posts/<Company>/ ya style_refs/<Company>/) — sab se naye 4.
    Folder ka naam thoda alag ho to bhi milta hai: 'E liquid'='Eliquid', 'Dental Care 4U'='dental care for you'.
    Dashboard custom references (max 10): har FILE ke liye un mein se random 4 (file ke andar wahi 4)."""
    entry = custom_reference_entry(company)
    if entry and entry.get("paths"):
        import random
        pool = [Path(x) for x in entry["paths"] if Path(x).is_file()]
        if pool:
            n = min(len(pool), int(CFG.get("style_refs_max", 4)))
            key, file = norm(company_alias(company) or company), HUB.current.get("file")
            cached = CUSTOM_REF_PICK.get(key)
            if cached and cached[0] == file and all(p in pool for p in cached[1]):
                return list(cached[1])
            picked = random.sample(pool, n)
            CUSTOM_REF_PICK[key] = (file, picked)
            log(f"  Custom references ({len(pool)} mein se {n}): " + ", ".join(p.name for p in picked))
            return list(picked)
        log(f"  (custom references ki files nahi milin — folder wale designs use ho rahe hain)")
    import difflib

    def key(text):
        return re.sub(r"[^a-z0-9]+", "", text.lower().replace("for you", "4u"))
    want = key(company)
    best, best_score = None, 0.0
    for root in CFG.get("style_refs_dirs", ["style_refs"]):
        root = Path(root) if Path(root).is_absolute() else BASE / root
        if not root.is_dir():
            continue
        for folder in root.iterdir():
            f = key(folder.name)
            if not folder.is_dir() or len(f) < 3:
                continue
            if f == want:
                score = 1.0
            elif len(f) >= 5 and (f in want or want in f):
                score = 0.9
            else:
                score = difflib.SequenceMatcher(None, want, f).ratio()
            if score > best_score:
                best, best_score = folder, score
    if not best or best_score < CFG.get("style_refs_min_match", 0.8):
        return []
    imgs = [p for p in best.iterdir() if p.is_file() and IMAGE_EXT_RE.search(p.name)]
    imgs.sort(key=lambda p: p.stat().st_mtime, reverse=True)  # aap ke sab se naye designs
    return imgs[:CFG.get("style_refs_max", 4)]


def safe_name(label):
    return re.sub(r"[^\w\- ]+", "", label).strip().replace(" ", "_")


async def wait_for_images(waseem, cid, before, expected, sent_at=None, retry_request=None):
    """Jab tak `expected` nayi images na ban jayen (ya ChatGPT ruk jaye) intezar. Return naye file ids."""
    t = CFG["timeouts_minutes"]
    end = run_time() + max(t["image_generation"], 4 * expected + 5) * 60  # batch: har image ~2-4 min
    reply_since = sent_at if sent_at is not None else time.time() - 5
    last_count, last_change, nudged, new_ids, limit_waits = -1, run_time(), False, [], 0
    retry_clicks = 0  # ChatGPT ka "Try again" (image glitch) kitni dafa dabaya
    idle_since = None  # ChatGPT kab se LAGATAAR ruka hua hai (Thinking ke beech stop button pal bhar gayab hota hai)
    # Page par MERE AAKHRI message ke baad wali generated images dekho (ginti nahi — lambi chat mein ChatGPT
    # purane turns page se hata deta hai). Jo image ek dafa dikhi, yaad rehti hai (nudge ke baad bhi).
    seen = []
    def image_key(ref):
        match = re.search(r"file[_-][A-Za-z0-9]+", ref)
        return match.group(0) if match else ref
    excluded = {image_key(ref) for ref in before}

    def remember(refs):
        keys = excluded | {image_key(ref) for ref in seen}
        for ref in refs:
            key = image_key(ref)
            if key not in keys:
                seen.append(ref)
                keys.add(key)

    last_api = run_time()

    async def poll():
        try:
            r = await HUB.exec(waseem.role, waseem.tab, "turn_images") or {}
            remember(r.get("imgs", []))
        except Exception as e:  # page se na ho sake -> API backup
            log(f"    (page check: {e} — API se)")
            conv = await waseem.conversation(cid)
            remember(image_ids_after(conv, sent_at) if sent_at is not None else image_file_ids(conv))
        return list(seen)
    stuck_mins = CFG.get("stuck_refresh_minutes", 10)
    refreshes, max_refreshes = 0, CFG.get("max_refreshes", 2)
    hard_end = run_time() + stuck_mins * 60 * (max_refreshes + 1) + 300  # aakhri hadd
    while run_time() < hard_end and (run_time() < end or await waseem.generating()):
        await run_sleep(CFG.get("image_poll_seconds", 15))  # kam requests (429 se bachao)
        # ChatGPT atak gaya? (10 min se koi nayi image nahi) -> tab refresh; image ban chuki ho to wahi milegi
        if run_time() - last_change > stuck_mins * 60 and len(new_ids) < expected:
            if refreshes >= max_refreshes:
                log(f"  ChatGPT {refreshes} refresh ke baad bhi atka — yeh image chhod kar aage")
                break
            refreshes += 1
            log(f"  ChatGPT {stuck_mins} minute se atka hai — tab refresh ({refreshes}/{max_refreshes})")
            HUB.stuck_events += 1
            notify("ChatGPT atak gaya", f"{stuck_mins} minute se image nahi aayi — tab refresh kar raha hoon.", sticky=False)
            try:
                await waseem.goto(f"{CHATGPT}/c/{cid}")
            except Exception as e:
                log(f"  (refresh: {e})")
            last_change = run_time()
            continue
        try:  # page dekh kar (jaise insaan screen dekhta hai) — har 15s API request nahi
            new_ids = await poll()
        except Exception as e:
            log(f"  (page check retry) {e}")
            continue
        # background window mein ChatGPT nayi image page par render nahi karta -> ChatGPT kaam khatam kar chuka
        # ho aur page par image na dikhe to EK API request se dekho (mere message ke baad bani images)
        if (len(new_ids) < expected and sent_at is not None
                and run_time() - last_api >= 40 and not await waseem.generating()):
            last_api = run_time()
            try:
                ids = image_ids_after(await waseem.conversation(cid), sent_at)
                remember(ids)
                new_ids = list(seen)
            except Exception as e:
                log(f"  (API check: {e})")
        if len(new_ids) != last_count:
            last_count, last_change = len(new_ids), run_time()
            log(f"  Images ready: {len(new_ids)}/{expected}")
        busy = await waseem.generating()
        thinking = False  # sirf nudge ke faisle ke liye (image khatam hone ka faisla stop button se hi)
        if not busy:  # Thinking model: "Thinking / Creating image" likha ho to abhi kaam chal raha hai
            try:
                tail = (await HUB.exec(waseem.role, waseem.tab, "page_text_tail", 400) or "")[-400:]
                thinking = bool(re.search(r"Thinking|Creating image|Generating|Reasoning", tail))
            except Exception:
                pass
        idle_since = None if (busy or thinking) else (idle_since or run_time())
        if len(new_ids) >= expected and not busy:
            # generation ke dauran page par preview / adhoori images bhi dikhti hain (Belle 15: Post 2 = Post 1,
            # Post 3 = Post 2). Kaam khatam -> ChatGPT ki apni list (sahi order, sirf final images) lo.
            if sent_at is not None:
                try:
                    final = [i for i in image_ids_after(await waseem.conversation(cid), sent_at)
                             if image_key(i) not in excluded]
                    if len(final) >= expected:
                        return final
                except Exception as e:
                    log(f"  (final images API check: {e} — page wali list)")
            break
        # ChatGPT kabhi image bana kar neeche "Try again" de deta hai (server glitch) — 18 minute intezar nahi:
        # image dikh rahi ho to woh le lo; na dikhe to "Try again" dabao (2 dafa tak)
        if not busy:
            try:
                tr = await HUB.exec(waseem.role, waseem.tab, "turn_retry", False, timeout=30) or {}
            except Exception:
                tr = {}
            if tr.get("found"):
                if new_ids:
                    log(f"  ChatGPT ne 'Try again' dikhaya — jo {len(new_ids)} image(s) ban kar dikh rahi hain woh save kar raha hoon")
                    HUB.stuck_events += 1
                    break
                if retry_clicks < 2:
                    retry_clicks += 1
                    log(f"  ChatGPT par 'Try again' — khud click kar raha hoon ({retry_clicks}/2)")
                    HUB.stuck_events += 1
                    try:
                        await HUB.exec(waseem.role, waseem.tab, "turn_retry", True, timeout=30)
                    except Exception as e:
                        log(f"  (Try again click nahi hua: {e})")
                    last_change = run_time()
                    end = max(end, run_time() + t["image_generation"] * 60)
                    continue
                log("  'Try again' 2 dafa dabaya, image phir bhi nahi — aage (yeh image alag message mein)")
                break
        if not busy and run_time() - last_change > t["idle_after_images"] * 60 and len(new_ids) < expected:
            # ChatGPT ki image limit lagi? -> ruko, phir dobara bolo
            try:  # sirf jab image nahi aa rahi: page ka text dekho (limit ka message?) — API nahi
                reply = await HUB.exec("waseem", waseem.tab, "page_text_tail", 1500) or ""
            except Exception:
                reply = ""
            if is_chat_limit_notice(reply[-600:]):
                if limit_waits >= CFG.get("limit_max_retries", 1):
                    raise ChatLimit("ChatGPT ki limit retry ke baad bhi lagi hai")
                limit_waits += 1
                mins = CFG.get("limit_wait_minutes", 2)
                log(f"  ChatGPT limit lagi — {mins} minute ruk raha hoon")
                notify("ChatGPT limit lagi", f"Images ki limit — script {mins} minute ruk kar khud dobara karegi.", sticky=False)
                await run_sleep(mins * 60)
                reply_since = await retry_request() if retry_request else await waseem.send(CFG["nudge_message"])
                last_change, end = run_time(), run_time() + t["image_generation"] * 60
                continue
        if not busy and not thinking and run_time() - last_change > t["idle_after_images"] * 60 \
                and run_time() - idle_since > CFG.get("idle_confirm_seconds", 90):
            if nudged:
                break  # ek dafa yaad dila diya, jo bana wo le lo
            # Verify the server record immediately before asking for another image.
            # A broken detector/network is not evidence that generation failed.
            if sent_at is not None:
                try:
                    remember(image_ids_after(await waseem.conversation(cid), sent_at))
                    new_ids = list(seen)
                    if len(new_ids) >= expected:
                        break
                except Exception as e:
                    log(f"  Image verify nahi hui ({e}) — duplicate generation request nahi bheji")
                    break
            log("  Images poori nahi bani — ChatGPT ko dobara bol raha hoon")
            nudged = True
            reply_since = await retry_request() if retry_request else await waseem.send(CFG["nudge_message"])
            last_change = run_time()
    return new_ids


def is_generated_img(img):
    """Page ki image ChatGPT ne banayi hai? (asli page check: alt 'Generated image N' vs 'User attachment';
    kai images ki gallery ke thumbnails ka alt khaali hota hai lekin size poori hoti hai)"""
    alt = img.get("alt") or ""
    if (img.get("role") or "").lower() == "user" or re.search(r"user attachment|upload", alt, re.I):
        return False
    if re.search(r"generated", alt, re.I):
        return True
    if not alt and (img.get("w") or 0) >= 700 and (img.get("h") or 0) >= 700:
        return True  # gallery thumbnail
    return bool(re.search(r"estuary|oaiusercontent|file_", img.get("src") or "", re.I)) and (img.get("w") or 0) >= 300


async def page_image_refs(waseem, cid=None):
    """Chat page par nazar aane wali generated images (API nahi -> 'too many requests' nahi)."""
    try:
        imgs = await HUB.exec("waseem", waseem.tab, "page_images") or []
        refs = [i["src"] for i in imgs if is_generated_img(i)]
        return list(dict.fromkeys(refs))
    except Exception as e:  # page se na mile to API backup
        log(f"    (page images nahi parhi: {e} — API se)")
        return image_file_ids(await waseem.conversation(cid)) if cid else []


async def download_generated(waseem, cid, fid, dest_noext):
    """ChatGPT ki bani image save. Kai tareeqe (ek atke to doosra) — har ek ki choti time-limit:
    1) page se seedha (fetch)  2) API ka download link  3) screen par dikhti image (canvas — 'right click save' jaisa)
    4) Chrome download. (HR Strategy 8: image dikhi magar fetch 10 minute atka raha -> ab 90 second.)"""
    # ChatGPT image save ho rahi ho to Gmail scan wait kare (CPU / focus / tab pe asar na pade)
    HUB.designer_busy = True
    errors = []

    def save_b64(b64, ctype, url):
        path = Path(str(dest_noext) + (_ext_for(ctype, url) or ".png"))
        path.write_bytes(base64.b64decode(b64))
        return path
    try:
        page_src = fid if fid.startswith(("http", "/", "blob:")) else None
        if page_src:  # 1) page ki image ka link -> seedha page se (API nahi)
            try:
                r = await HUB.exec("waseem", waseem.tab, "fetch_b64", page_src, timeout=90)
                if r and r["status"] == 200 and len(r["b64"]) > 8000:
                    return save_b64(r["b64"], r["contentType"], page_src)
                errors.append(f"fetch {r and r['status']}")
            except Exception as e:
                errors.append(f"fetch: {e or type(e).__name__}")
        m = re.search(r"(file[_-][A-Za-z0-9]+)", fid)
        if m:  # 2) API ka download link
            try:
                info = await waseem.api(f"/backend-api/files/download/{m.group(1)}?conversation_id={cid}&inline=false")
                url = info["download_url"]
                r = await HUB.exec("waseem", waseem.tab, "fetch_b64", url, timeout=90)
                if r["status"] != 200:
                    r = await HUB.call("waseem", "fetch", url=url if not url.startswith("/") else CHATGPT + url, timeout=90)
                if r["status"] == 200 and len(r["b64"]) > 8000:
                    return save_b64(r["b64"], r["contentType"], url)
                errors.append(f"api {r['status']}")
            except Exception as e:
                errors.append(f"api: {e or type(e).__name__}")
        if page_src:  # 3) screen par dikhti image -> canvas (right click save jaisa)
            try:
                r = await HUB.exec("waseem", waseem.tab, "img_canvas_b64", page_src, timeout=60) or {}
                if r.get("b64") and len(r["b64"]) > 8000:
                    log("    (image screen se uthayi — download link atka hua tha)")
                    return save_b64(r["b64"], "image/png", ".png")
                errors.append(f"canvas: {r.get('error')}")
            except Exception as e:
                errors.append(f"canvas: {e or type(e).__name__}")
            if page_src.startswith("http"):  # 4) Chrome download ("Save image as")
                try:
                    src = await HUB.call("waseem", "download_url", url=page_src, timeout=120)
                    log("    (image Chrome download se save ki)")
                    return _from_download(src, dest_noext)
                except Exception as e:
                    errors.append(f"chrome download: {e or type(e).__name__}")
        raise RuntimeError("generated image save nahi hui — " + "; ".join(errors))
    finally:
        HUB.designer_busy = False


RATIOS = {"post": (4, 5), "story": (9, 16), "mockup_placed": (4, 5), "mockup_use": (4, 5)}


def file_number(company, file_name):
    """Downloads ka sub-folder: 'Wear Wood 8' -> '8', 'Booth scout 12-10-26' -> '12-10-26', 'Single Parent 13th(4 posts)' -> '13th'.
    Alias file (Ficus Financial 6 -> Beyond the Budget chat) -> poora file naam. Number nahi -> None (seedha company folder)."""
    if not file_name:
        return None
    name = clean_title(file_name)
    own = company_from_title(name)
    if norm(own) != norm(company):
        return name
    m = re.search(r"\d[\w-]*", name[len(own):])
    return m.group(0).strip("-") if m else None


def part_sections(doc, sets):
    """Doc ke sections ko sets (1-3, 4-6...) mein baanto. Reel / story / done wale section pichli post ke saath."""
    set_of = {}
    for gi, (_, outs, _) in enumerate(sets):
        for o in outs:
            d = o["d"]
            set_of[d.get("section") if d.get("carousel") else d["label"]] = gi
    parts, cur = [[] for _ in sets], 0
    for s in split_sections(doc):
        cur = set_of.get(s["label"], cur)
        parts[cur].append(s)
    return parts


def part_html(sections):
    """Sections -> Google Doc mein paste ke liye HTML (label aur bold lines bold)."""
    import html as _html
    rows = []
    for s in sections:
        label = s["label"] + (f" | {s['desc']}" if s.get("desc") else "")
        rows.append(f"<p><b>{_html.escape(label)}</b></p>")
        for line, bold in zip(s["lines"], s["bold"]):
            if not line:
                continue
            t = _html.escape(line)
            rows.append(f"<p><b>{t}</b></p>" if bold else f"<p>{t}</p>")
    text = "\n".join(re.sub(r"<[^>]+>", "", _html.unescape(r)) for r in rows)
    return "".join(rows), text


async def create_part_doc(title, sections):
    """Designer profile mein nayi Google Doc: naam + posts paste. Return doc id. Paste ka sabot: doc dobara parh kar."""
    html, text = part_html(sections)
    # naam bananay ke waqt hi (script se baad mein naam badalna Docs nahi maanta — Dental 1-3 "Untitled" reh gaya)
    tab = await HUB.call("designer", "open_tab", active=True,
                         url="https://docs.google.com/document/create?title=" + quote(title))
    doc_id = None
    for _ in range(40):
        await run_sleep(1.5)
        info = await HUB.call("designer", "tab_info", tabId=tab) or {}
        m = re.search(r"/document/d/([\w-]{20,})", info.get("url", ""))
        if m and await HUB.exec("designer", tab, "gdoc_ready"):
            doc_id = m.group(1)
            break
    if not doc_id:
        await close_quietly(tab)
        raise RuntimeError("nayi Google Doc nahi khuli")
    try:
        return await _fill_part_doc(tab, doc_id, title, sections, html, text)
    except Exception:
        await close_quietly(tab)  # khali doc khuli na rahe (warna woh line mein aa kar "khali" file banti hai)
        raise


async def close_quietly(tab):
    try:
        await HUB.call("designer", "close_tab", tabId=tab, timeout=20)
    except Exception:
        pass


async def _fill_part_doc(tab, doc_id, title, sections, html, text):
    for _ in range(5):  # naam sach mein laga? (Dental 1-3: "Untitled document" reh gaya tha) -> tab ka title dekho
        await HUB.call("designer", "focus_tab", tabId=tab)
        await HUB.exec("designer", tab, "gdoc_set_title", title)
        await run_sleep(3)
        info = await HUB.call("designer", "tab_info", tabId=tab) or {}
        if info.get("title", "").startswith(title):
            break
    else:
        raise RuntimeError(f"nayi doc ka naam '{title}' nahi laga")
    want = len(sections[0]["label"]) if sections else 1
    for attempt in range(2):
        await HUB.exec("designer", tab, "gdoc_paste", html, text)
        await run_sleep(5)
        try:
            got = "\n".join(l for l, _ in await fetch_doc(tab, doc_id))
        except Exception:
            got = ""
        if norm(sections[0]["label"]) in norm(got) if sections else got.strip():
            return doc_id
        log(f"    (doc mein paste nazar nahi aaya{' — dobara' if not attempt else ''})")
    raise RuntimeError(f"nayi doc '{title}' mein posts paste nahi hui (len {want})")


SPLITS_FILE = OUT_ROOT / "splits.json"


def load_splits():
    try:
        return json.loads(SPLITS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_splits(data):
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    SPLITS_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


async def trim_original_doc(tab, doc_id, first_sections, moved_labels):
    """Asal bari file mein sirf set 1 (posts 1-3 + story) rakho — baqi posts ab apni nayi docs mein hain.
    Pakka na ho sake to asal doc waisi hi rehti hai (sirf log), file phir bhi set 1 banati hai."""
    html, text = part_html(first_sections)
    HUB.designer_busy = True
    try:
        await HUB.call("designer", "focus_tab", tabId=tab)
        await run_sleep(2)
        doc_len = len("\n".join(l for l, _ in await fetch_doc(tab, doc_id)))  # poora select hua? isi se check
        r = await HUB.exec("designer", tab, "gdoc_replace_all", html, text, doc_len) or {}
        if not r.get("ok"):
            log(f"  (asal doc chhoti nahi hui: {r.get('error')} — asal doc mein poora content reh gaya)")
            return False
        for _ in range(4):  # Docs save hone mein kuch second lagte hain
            await run_sleep(6)
            got = norm("\n".join(l for l, _ in await fetch_doc(tab, doc_id)))
            first_ok = not first_sections or norm(first_sections[0]["label"]) in got
            if first_ok and not any(norm(lbl) in got for lbl in moved_labels if len(norm(lbl)) > 4):
                log("  Asal doc mein ab sirf set 1 (posts 1-3 + story) — baqi nayi docs mein")
                return True
        log("  ⚠ asal doc check: content poora nahi badla — doc khud dekh lein")
        notify("Asal doc check karein", "Bari file ki asal doc chhoti karte waqt content theek nahi badla — doc dekh lein.")
    except Exception as e:
        log(f"  (asal doc chhoti nahi hui: {e})")
    finally:
        HUB.designer_busy = False
    return False


async def split_doc_into_parts(folder, doc, sets):
    """Bari file (Dental 10 posts) ke AGLE sets -> nayi Google Docs '<file> (4-6)', '(7-10)' — har ek alag file
    (record alag, folder alag, beech mein doosri companies). Return [(title, doc id)]."""
    parts = part_sections(doc, sets)[1:]  # pehla set asal file hi banati hai
    sets = sets[1:]
    made = []
    log(f"  Bari file: agle sets ({', '.join(s[0] for s in sets)}) nayi Google Docs mein")
    HUB.designer_busy = True
    try:
        for (lbl, _, _), secs in zip(sets, parts):
            if not secs:
                continue
            title = f"{folder} ({lbl})"
            doc_id = await create_part_doc(title, secs)
            made.append((title, doc_id))
            log(f"    Nayi doc bani: {title}")
    finally:
        HUB.designer_busy = False
    return made


def final_dir(company, file_name=None):
    """Downloads\\<date>\\<company>\\<file number>\\ — ek company ki kai files alag alag folder mein."""
    base = Path(CFG.get("final_dir") or (Path.home() / "Downloads")).expanduser()
    d = base / datetime.now().strftime("%Y-%m-%d") / re.sub(r'[<>:"/\\|?*]', "", company).strip()
    part = PART_RE.search(file_name or "")
    sub = file_number(company, PART_RE.sub("", file_name) if part else file_name)
    if part:  # bari file ka hissa (nayi doc "... (4-6)") -> ek hi folder "10 (4-6)" (andar sub-folder nahi)
        sub = f"{sub} ({part.group(1)})" if sub else part.group(1)
    if sub:
        d = d / re.sub(r'[<>:"/\\|?*]', "", sub).strip(" .")
    d.mkdir(parents=True, exist_ok=True)
    return d


async def qa_batch(waseem, outs, company):
    """Saari images ek saath check (1 minute max). Sirf: size + text sahi / extra text to nahi.
    Return ([(o, issues)] ghalat wali, [unverified notes]).
    QA confirm na ho sake to file FAIL nahi hoti — images save hoti hain, summary mein 'khud dekh lo'."""
    from PIL import Image
    bad, unverified = {}, []
    for o in outs:
        w, h = Image.open(o["file"]).size
        rw, rh = RATIOS.get(o["type"], (4, 5))
        if abs(w / h - rw / rh) > CFG["qa"]["ratio_tolerance"]:
            bad.setdefault(id(o), (o, []))[1].append(f"wrong size {w}x{h} — must be {rw}:{rh}")
    if CFG["qa"]["vision_check"] and outs:
        lines = []
        for i, o in enumerate(outs, 1):
            d = o["d"]
            if o["type"].startswith("mockup"):
                exp = "NO text at all (product mockup) — only text already printed on the product itself is OK"
            elif d.get("brief"):
                exp = "only the text this brief asks for: " + d["content"]
            elif d.get("ig_shot") and not d["content"]:
                exp = "text from an Instagram post (just check nothing extra / no other company name)"
            else:
                exp = d["content"]
            lines.append(f"Image {i}: expected text =\n{exp}")
        prompt = CFG["qa"]["batch_prompt"].format(company=company, n=len(outs), expected="\n\n".join(lines))
        company_tab = waseem.tab
        qa_tab = None
        try:
            # QA alag nayi tab mein (company chat wali tab khuli rahe -> dobara banwana foran)
            qa_tab = await HUB.call("waseem", "open_tab", url=CHATGPT + "/?temporary-chat=true", active=True)
            waseem.tab = qa_tab
            for _ in range(60):
                if await HUB.exec("waseem", qa_tab, "has", SEL["composer"]):
                    break
                await run_sleep(1)
            await run_sleep(2)
            sent_at = await waseem.send(prompt, [o["file"] for o in outs])
            limit = run_time() + CFG["qa"]["max_seconds"]
            res = None
            while run_time() < limit:
                await run_sleep(5)
                # jawab page ke text se (API nahi) — jaise insaan screen par parhta hai
                tail = await HUB.exec("waseem", waseem.tab, "page_text_tail", 6000) or ""
                m = re.search(r"QAJSON\s*(\{.*\})", tail, re.S)
                if m and not await waseem.generating():
                    try:
                        res = json.loads(m.group(1))
                        break
                    except Exception:
                        pass
            if res is None:  # page se na mila -> sirf EK dafa API backup
                reply = await waseem.latest_reply(sent_at)
                m = re.search(r"\{.*\}", reply, re.S)
                if m:
                    try:
                        res = json.loads(m.group(0))
                    except Exception:
                        pass
            if res is None:
                unverified.append(f"QA {CFG['qa']['max_seconds']}s mein jawab nahi aaya")
            else:
                seen = set()
                for r in res.get("results", []):
                    try:
                        i = int(r.get("image", 0)) - 1
                    except (TypeError, ValueError):
                        continue
                    if not 0 <= i < len(outs) or not isinstance(r.get("ok"), bool):
                        continue
                    seen.add(i)
                    issues = [str(x) for x in r.get("issues") or ["text galat"]]
                    if any(re.search(r"not attached|not accessible|cannot be verified|can't see|no image|"
                                     r"not visible|unable to (view|see)|unverified", x, re.I) for x in issues):
                        unverified.append(f"{outs[i]['name']}: QA ko image nazar nahi aayi")
                        continue
                    if r.get("ok") is False:
                        o = outs[i]
                        bad.setdefault(id(o), (o, []))[1].extend(issues)
                missed = [outs[i]["name"] for i in range(len(outs)) if i not in seen]
                if missed:
                    unverified.append("QA ne in ka result nahi diya: " + ", ".join(missed))
        except Exception as e:
            unverified.append(f"QA nahi ho saka: {e}")
        finally:  # QA tab band, wapis company chat
            waseem.tab = company_tab
            if qa_tab:
                try:
                    await HUB.call("waseem", "close_tab", tabId=qa_tab)
                    await HUB.call("waseem", "focus_tab", tabId=company_tab)
                except Exception:
                    pass
    return list(bad.values()), unverified


# ---------------------------------------------------------------- runner ----

def split_instagram_carousels(designs):
    """Instagram link ki kai slides -> ALAG carousel (har slide ki apni image, saari slides EK carousel message).
    Ek slide wali Instagram post baqi posts ke saath hi batch mein rehti hai."""
    out, next_c = [], max((d.get("carousel") or 0 for d in designs), default=0)
    for d in designs:
        shots = d.get("ig_shot") or []
        if len(shots) < 2 or d.get("carousel") or d["kind"] != "post":
            out.append(d)
            continue
        next_c += 1
        for k, shot in enumerate(shots, 1):
            out.append(dict(d, ig_shot=[shot], carousel=next_c, slide=k, ig_album=len(shots), label=f"{d['label']} - Slide {k}",
                            content=d["content"] if k == 1 else "", section=d.get("section", d["label"])))
    return out


async def process_doc(tab_id, doc_id, company, terms, out_dir, summary, prepared_doc=None):
    # Designer busy = Gmail scan ruke (tab aage aa kar download/screenshot bigaar sakti hai).
    # ChatGPT (Waseem) wait / generate ke dauran Designer free -> Gmail nayi files line mein khol sakta hai.
    HUB.designer_busy = True
    try:
        doc = prepared_doc if prepared_doc is not None else await prepare_file(tab_id, doc_id, out_dir)
    finally:
        HUB.designer_busy = False
    out_dir.mkdir(parents=True, exist_ok=True)
    designs, reels, done_posts = parse_doc(doc)
    if not designs and not reels and not done_posts:
        log("  Format samajh nahi aaya — ChatGPT (Wajid temporary chat) se doc parhwa raha hoon")
        try:
            designs, reels, done_posts = await parse_with_chatgpt(doc)
        except Exception as e:
            log(f"  ChatGPT se bhi nahi parha gaya: {e}")
    try:  # mubham headings (format step) -> summary mein "check karo"
        review = json.loads((out_dir / "format_review.json").read_text(encoding="utf-8"))
        if review.get("warnings"):
            summary["format_check"] = review["warnings"]
    except Exception:
        pass
    # koi post chupke se na chhoote: jo section design/reel/done kisi mein nahi aaya -> summary mein naam ke saath
    sections = split_sections(doc)
    if sections and len(designs) + len(reels) + len(done_posts) < len(sections):
        used = {d.get("section", d["label"]) for d in designs}
        for x in reels + done_posts:
            head = str(x).split(":")[0].strip()
            used.add(head)
            used.add(re.sub(r"\s*\([^)]*\)\s*$", "", head).strip())  # "POST 17 (REEL COVER)" -> "POST 17"
        dropped = [s["label"] for s in sections if s["label"] not in used
                   and not any(str(x).startswith(s["label"]) for x in reels + done_posts)]
        if dropped:
            def first_text(label):  # doc mein us post ki pehli asal line -> pehchan aasaan
                s = next(x for x in sections if x["label"] == label)
                line = next((l for l in s["lines"] if l and not URL_RE.fullmatch(l.strip())), "")
                line = re.sub(r"[:\s]+", " ", URL_RE.sub("", line)).strip()[:45]
                return f"{label} «{line}»" if line else label
            summary["dropped"] = [first_text(x) for x in dropped]
            log(f"  ⚠ In posts ka content nahi samjha gaya (summary mein): {', '.join(dropped)}")
    if summary.get("only_post"):  # apply after fallback too; never run the whole file by accident
        want = norm(summary["only_post"])
        designs = [d for d in designs if want == norm(d["label"])
                   or (d.get("carousel") and want == norm(d.get("section", "")))]  # carousel = saari slides
        if len({d.get("section", d["label"]) if d.get("carousel") else d["label"] for d in designs}) != 1:
            raise RuntimeError(f"Test selector '{summary['only_post']}': exactly 1 post chahiye, {len(designs)} mili")
        log(f"  (Test: sirf '{summary['only_post']}' -> 1 post)")
    summary["reels"], summary["done"] = reels, done_posts
    summary["sections"] = doc_post_count(designs, reels, done_posts)  # checker: baad mein dobara gin kar milao
    if designs and reels:  # pehli post se pehle wali reels
        designs[0]["reels_before"] = len(reels) - sum(d.get("reels_after", 0) for d in designs)
    # 3 se zyada posts -> Designer mein nayi Google Docs (1-3, 4-6, 7-10) — har hissa alag file ban kar banega
    # asal file khud pehla set (1-3) banti hai; sirf AGLE sets (4-6, 7-10) nayi docs mein (khaali/Untitled doc kabhi nahi)
    if CFG.get("split_big_docs", True) and not summary.get("only_post") and not PART_RE.search(summary.get("file", "")):
        sets = file_sets(expand_outputs(designs))
        if len(sets) > 1:
            splits = load_splits()
            already_started = load_progress().get(doc_id) or load_state().get(doc_id, {}).get("status") == "partial"
            if doc_id not in splits and not already_started:
                splits[doc_id] = await split_doc_into_parts(summary["file"], doc, sets)
                save_splits(splits)
                if splits[doc_id]:  # baqi posts nayi docs mein chali gayin -> asal doc mein sirf set 1 (+ story)
                    await trim_original_doc(tab_id, doc_id, part_sections(doc, sets)[0],
                                            [s["label"] for p in part_sections(doc, sets)[1:] for s in p])
            if doc_id in splits:  # (dobara chale to nayi docs dobara nahi banti)
                keep = {id(o["d"]) for o in sets[0][1]}
                designs = [d for d in designs if id(d) in keep]
                summary["part"] = sets[0][0]
                summary["split_into"] = [t for t, _ in splits[doc_id]]
                log(f"  Yeh file sirf posts {sets[0][0]} banayegi; baqi: {', '.join(summary['split_into'])}")
    if reels:
        log(f"  {len(reels)} reel skip (aap khud banaoge)")
    if done_posts:
        log(f"  {len(done_posts)} post pe 'done' likha hai — skip")
    if not designs:
        if reels or done_posts:
            summary["designs"] = 0
            log("  Is file mein banane wali koi nayi post nahi (sab reel / done)")
            return
        raise RuntimeError(f"Doc mein koi post/story nahi mili — doc ka text yahan dekho: {out_dir / 'doc_text.txt'}")
    posts = sum(d["kind"] == "post" for d in designs)
    log(f"  {posts} post, {len(designs) - posts} story: " + ", ".join(
        d["kind"] + ("+img" if d["image_url"] else "") + ("+instagram" if d["ig_url"] else "") for d in designs))
    summary["designs"] = len(designs)

    # Resolve branding before downloading source assets; missing website -> skip promptly.
    # Waseem (ChatGPT) — Designer free, Gmail scan chal sakta hai.
    doc_text = "\n".join(l for l, _ in doc)
    waseem = Chat("waseem")
    cid, website_brand = await resolve_company_chat(waseem, company, terms, doc_text, out_dir)
    if website_brand:
        summary["branding_website"] = website_brand["website"]
        for d in designs:
            d["website_brand"] = website_brand

    HUB.designer_busy = True  # Freepik / Drive / screenshots — Gmail tab aage na aaye
    try:
        ig_cache = {}  # instagram url -> screenshots (carousel slides ek hi post share karti hain)
        img_dir = out_dir / "source_images"
        img_dir.mkdir(parents=True, exist_ok=True)
        for n, d in enumerate(designs, 1):
            if d["image_url"]:
                try:
                    d["image_path"] = await download_image(d["image_url"], img_dir / f"image{n}")
                    log(f"  Image {n} download: {d['image_path'].name}")
                except Exception as e:
                    # file mein image di hai to wahi zaroori -> bina image ke post nahi banegi
                    log(f"  Image {n} download FAIL ({e}) — yeh post skip (image zaroori hai)")
                    summary.setdefault("image_fail", []).append(f"{d['label']}: {d['content'][:50]} -> {d['image_url']}")
                    d["skip"] = True
            if d.get("after_url") and not d.get("skip"):  # comparison: AFTER photo bhi zaroori
                try:
                    d["after_path"] = await download_image(d["after_url"], img_dir / f"image{n}_after")
                    log(f"  Image {n} (after) download: {d['after_path'].name}")
                except Exception as e:
                    log(f"  Image {n} (after) download FAIL ({e}) — yeh post skip (dono images zaroori)")
                    summary.setdefault("image_fail", []).append(f"{d['label']}: AFTER image -> {d['after_url']}")
                    d["skip"] = True
            if not d.get("product_url") and not d["image_url"] and d.get("maybe_products"):
                for u in d["maybe_products"]:
                    if await is_product_page(u):
                        d["product_url"] = u
                        log(f"  Design {n}: product page mila -> {u}")
                        break
            if d.get("product_url"):
                try:
                    d["product_path"] = await download_image(d["product_url"], img_dir / f"product{n}")
                    mock = is_mockup_company(company)
                    log(f"  Product {n} ki image download: {d['product_path'].name}"
                        + (" (2 mockups banenge)" if mock else " (sirf 1 design, mockups nahi)"))
                except Exception as e:
                    log(f"  Product {n} image download fail ({e}) — is product ki post pending")
                    d["skip"] = True
                    summary.setdefault("image_fail", []).append(f"{d['label']}: product photo download nahi hui")
            for u in d.get("ref_links") or []:  # Molen: brief ka link parh kar idea
                try:
                    d["ref_text"] = await fetch_reference_text(u)
                    if d["ref_text"]:
                        log(f"  Design {n}: reference page parh liya ({len(d['ref_text'])} chars)")
                        break
                except Exception as e:
                    log(f"  Design {n}: reference link nahi khula ({e})")
            if d["ig_url"]:
                try:
                    if d["ig_url"] not in ig_cache:  # carousel ki saari slides ek hi Instagram post -> ek dafa
                        ig_cache[d["ig_url"]] = await screenshot_instagram(d["ig_url"], img_dir / f"instagram{n}.png")
                        log(f"  Instagram {n}: {len(ig_cache[d['ig_url']])} slide screenshot")
                    shots = ig_cache[d["ig_url"]]
                    if d.get("ig_slide"):  # carousel slide k -> Instagram ki slide k
                        k = d["ig_slide"]
                        d["ig_shot"] = [shots[k - 1]] if k <= len(shots) else []
                        if not d["ig_shot"]:
                            d["text_from_image"] = True  # Instagram mein itni slides nahi -> image ka text
                    else:
                        d["ig_shot"] = shots
                except Exception as e:
                    log(f"  Instagram {n} screenshot fail ({e})")
                    if not d["content"] and not d.get("image_url"):
                        d["skip"] = True
                        summary.setdefault("image_fail", []).append(f"{d['label']}: Instagram screenshot nahi bana, text bhi nahi")
                    elif not d["content"]:
                        d["text_from_image"] = True

        designs = split_instagram_carousels(designs)
        # before/after carousel: ek slide ki photo na mile to doosri akeli nahi banti (dono images zaroori)
        ba_skip = {d.get("carousel") for d in designs if d.get("skip") and d.get("before_after")}
        for d in designs:
            if d.get("before_after") and d.get("carousel") in ba_skip and not d.get("skip"):
                d["skip"] = True
                summary.setdefault("image_fail", []).append(f"{d['label']}: before/after ki doosri image nahi mili — dono zaroori")
        designs = [d for d in designs if not d.get("skip")]
        if not is_mockup_company(company):
            for d in designs:  # mockups sirf chuni hui companies (Wear Wood); baqi: product photo heading wali post mein
                d["no_mockups"] = True
        if not designs:
            raise RuntimeError("Saari posts ki images download fail — kuch nahi bheja")
    finally:
        HUB.designer_busy = False  # generate wait mein Gmail OK; download_generated khud busy set karega
    # har nayi company / file: ChatGPT hard refresh (purani tab band, chat nayi tab mein) — phir message
    log("  ChatGPT hard refresh (nayi tab) — phir is file ke messages")
    await waseem.goto(f"{CHATGPT}/c/{cid}")
    await generate_and_download(waseem, cid, company, designs, out_dir, summary)


def move_story_issues(summary):
    """Story ka masla file ko incomplete nahi karta (story aap khud bana lete ho; posts zaroori hain).
    qa_fail / image_fail / dropped mein se story wali entries -> summary['story_skipped']. Return: koi mili?"""
    # sirf pehchan wala hissa dekho (heading «…» mein "story" lafz ho to bhi post hi rahe)
    is_story = lambda x: bool(re.search(r"\bstor(y|ies)\b", str(x).split("«")[0].split(":")[0], re.I))
    moved = []
    for key in ("qa_fail", "image_fail", "dropped"):
        items = summary.get(key) or []
        moved += [x for x in items if is_story(x)]
        if items:
            summary[key] = [x for x in items if not is_story(x)]
    if moved:
        summary.setdefault("story_skipped", []).extend(moved)
    return bool(moved)


def fail_hint(text):
    """Image fail ki wajah -> aap ko kya karna hai (seedhi baat)."""
    t = str(text).lower()
    if "limit" in t:
        return "ChatGPT ki limit thi -> START dobara dabao (sirf yeh banegi)"
    if "http" in t or "download" in t or "screenshot" in t:
        return "di hui image nahi mili -> link check karo, ya image khud laga kar banao"
    if "attach" in t:
        return "image ChatGPT par attach nahi hui -> START dobara dabao"
    return "START dobara dabao (sirf baqi images banengi), ya khud bana lo"


def folder_of(title):
    """Google Docs tab title -> Output folder / file naam."""
    folder = re.sub(r"\s*-\s*Google Docs\s*$", "", title)
    return re.sub(r'[<>:"/\\|?*]', "", re.sub(r"\.(docx?|pdf)$", "", folder, flags=re.I)).strip()


def write_file_report(out_dir, summary):
    """Dashboard ke liye har file ka aakhri hal: done / incomplete / error / skipped + masle (post number + heading)."""
    err = summary.get("error")
    # khaali error ("") bhi error hai — pehle isay "done" gin liya jata tha (HR Strategy 8: 0 images, phir bhi done)
    status = "done" if err is None else ("incomplete" if str(err).startswith("File incomplete") else "error")
    if summary.get("skipped"):
        status = "skipped"
    data = {k: summary.get(k) for k in ("file", "designs", "made", "error", "image_fail", "qa_fail", "dropped",
                                         "story_skipped", "reels", "done", "final_dir", "format_check",
                                         "qa_unverified", "manual_reason", "manual_todo")}
    data.update(status=status, finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "file_report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                                                  encoding="utf-8")
    except Exception as e:
        log(f"  (file_report save nahi hua: {e})")


def write_report(report, day_dir):
    """Aakhir mein saaf summary: kaun si file poori, kaun si adhoori, aur KAUN si post (doc ka number + heading)
    mein kya masla hai aur aap ko kya karna hai."""
    incomplete = lambda r: r["error"] or r.get("qa_fail") or r.get("image_fail") or r.get("dropped")
    skipped = [r for r in report if r.get("skipped")]
    good = [r for r in report if not incomplete(r) and not r.get("skipped")]
    bad = [r for r in report if incomplete(r) and not r.get("skipped")]
    n_posts = sum(len(r.get(k) or []) for r in report for k in ("image_fail", "qa_fail", "dropped"))
    n_story = sum(len(r.get("story_skipped") or []) for r in report)
    n_reels = sum(len(r["reels"]) for r in report)
    bar = "=" * 70
    lines = [bar, f"SUMMARY  {datetime.now():%d-%m-%Y %H:%M}", bar,
             f"Files: {len(report)}   |   POORI: {len(good)}   |   MASLA: {len(bad)}"
             + (f"   |   SKIP: {len(skipped)}" if skipped else ""),
             f"Aap ke karne ke kaam:  {n_posts} post(s)  +  {n_story} story  +  {n_reels} reel(s)", ""]

    def made(r):
        if r.get("made") is None:
            return ""
        return f"   ({r['made']} images bani)"

    if bad:
        lines += ["-" * 70, "MASLA WALI FILES  (yahan har post ka doc number + heading likha hai)", "-" * 70]
        for r in bad:
            lines.append(f"> {r['file']}{made(r)}")
            for x in r.get("image_fail", []):
                lines += [f"     X NAHI BANI      {x}", f"                      -> {fail_hint(x)}"]
            for x in r.get("qa_fail", []):
                lines += [f"     X GHALAT BANI    {x}", "                      -> 2 dafa ghalat bani: khud bana lo"]
            for x in r.get("dropped", []):
                lines += [f"     ? SAMAJH NAHI AAYI  {x}",
                          "                      -> doc mein is post ka heading/link check karo, phir START"]
            if r["error"] and not (r.get("image_fail") or r.get("qa_fail") or r.get("dropped")):
                lines += [f"     X FILE NAHI BANI  {r['error']}",
                          "                      -> wajah theek karo (ya START dobara), phir bhi ho to khud banao"]
            if r.get("manual_reason"):
                lines.append(f"     ! BRANDING        {r['manual_reason']}")
            lines.append("")
    extra = [r for r in report if r.get("story_skipped") or r.get("manual_todo") or r["reels"] or r.get("format_check")
             or r.get("qa_unverified") or r["done"]]
    if extra:
        lines += ["-" * 70, "AAP KHUD BANAO / DEKH LO", "-" * 70]
        for r in extra:
            lines.append(f"> {r['file']}")
            lines += [f"     STORY (khud banao)   {x}" for x in r.get("story_skipped", [])]
            lines += [f"     POST (khud banao)    {x}" for x in r.get("manual_todo", [])]
            lines += [f"     REEL (khud banao)    {x}" for x in r["reels"]]
            lines += [f"     HEADING CHECK KARO   {x}" for x in r.get("format_check", [])]
            lines += [f"     IMAGE KHUD DEKH LO   {x}  (QA confirm nahi hua)" for x in r.get("qa_unverified", [])]
            lines += [f"     PEHLE SE DONE (skip) {x}" for x in r["done"]]
            lines.append("")
    if skipped:
        lines += ["-" * 70, "SKIP KI (dashboard se) — DONE nahi, agli START par phir aayengi", "-" * 70]
        lines += [f"  SKIP  {r['file']}{made(r)}" for r in skipped] + [""]
    if good:
        lines += ["-" * 70, "POORI FILES  (sab images Downloads mein)", "-" * 70]
        for r in good:
            lines.append(f"  OK  {r['file']}{made(r)}" + (f"   ->  {r['final_dir']}" if r.get("final_dir") else ""))
            if r.get("branding_website"):
                lines.append(f"        (branding website se: {r['branding_website']})")
    for l in lines:
        log(l)
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"SUMMARY_{datetime.now():%H-%M}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Summary file: {path}")
    return {"files": len(report), "bad": len(bad), "posts": n_posts, "stories": n_story, "reels": n_reels}


def cleanup_old_outputs(days=None):
    """Output\\<date>\\ folders aur logs\\<date>.log jo `days` (config: keep_output_days, 7) se purane -> delete.
    Sirf date wale folders — state / chat memory / progress / Downloads kabhi nahi chhede jate."""
    days = CFG.get("keep_output_days", 7) if days is None else days
    if not days or days <= 0:
        return []
    cutoff = _date.today() - _timedelta(days=days)
    removed = []
    for item in OUT_ROOT.iterdir() if OUT_ROOT.exists() else []:
        try:
            if item.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.name):
                if _date.fromisoformat(item.name) < cutoff:
                    shutil.rmtree(item)
                    removed.append(item.name)
        except Exception as e:
            log(f"  (purana folder {item.name} delete nahi hua: {e})")
    logs = OUT_ROOT / "logs"
    for f in logs.glob("*.log") if logs.exists() else []:
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem) and _date.fromisoformat(f.stem) < cutoff:
                f.unlink()
        except Exception:
            pass
    if removed:
        log(f"Purane Output folders delete ({days} din se purane): {', '.join(removed)}")
    return removed


RUN_MARKER = OUT_ROOT / "run_active.json"


def set_run_marker(**fields):
    try:
        data = json.loads(RUN_MARKER.read_text(encoding="utf-8")) if RUN_MARKER.exists() else {}
    except Exception:
        data = {}
    if not data and "active" not in fields:
        return  # koi run marker nahi (test / sirf ek file) -> kuch nahi
    data.update(fields)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    RUN_MARKER.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def add_marker_doc(tab):
    try:
        data = json.loads(RUN_MARKER.read_text(encoding="utf-8"))
        if all(d["url"] != tab["url"] for d in data.get("docs", [])):
            data.setdefault("docs", []).append({"url": tab["url"], "title": tab["title"]})
            RUN_MARKER.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def clear_run_marker():
    try:
        RUN_MARKER.unlink()
    except FileNotFoundError:
        pass


def start_run(only=None, only_post=None):
    # Reserve synchronously: rapid START clicks / Gmail / auto-resume cannot create two workers.
    if HUB.running or (HUB.run_task is not None and not HUB.run_task.done()):
        return False
    HUB.running = True
    HUB.pause_requested = HUB.paused = False
    HUB.paused_seconds = 0.0
    HUB.pause_started = None
    HUB.pause_signal.clear()
    HUB.resume_signal.set()
    HUB.run_task = asyncio.create_task(run_all(only, only_post))
    return True


async def auto_resume():
    """Server shuru: pichla run adhoora (bijli gayi / PC restart) aur pause nahi tha -> khud START."""
    try:
        data = json.loads(RUN_MARKER.read_text(encoding="utf-8"))
    except Exception:
        return
    if not data.get("active") or data.get("paused"):
        if data.get("paused"):
            log("Pichla run PAUSE par tha — khud shuru nahi kar raha (START dabayen).")
        return
    log("Pichla run adhoora reh gaya tha (bijli / restart) — profiles judne ka intezar, phir khud shuru...")
    for _ in range(180):  # 15 minute tak Chrome profiles ka intezar
        if all(r in HUB.clients for r in ROLES):
            break
        await asyncio.sleep(5)
    else:
        log("Chrome profiles nahi judi — khud shuru nahi hua. Profiles khol kar START dabayen.")
        return
    await asyncio.sleep(10)
    if HUB.running:
        return
    try:  # bijli ke baad Chrome ne files ki tabs wapis na kholi hon -> khud kholo
        open_urls = {t["url"].split("#")[0] for t in await HUB.call("designer", "list_doc_tabs")}
        for doc in data.get("docs", []):
            if HUB.running:  # beech mein START / Gmail ne run shuru kar diya
                return
            if doc["url"].split("#")[0] not in open_urls:
                await HUB.call("designer", "open_tab", url=doc["url"], active=False)
                log(f"  File dobara kholi: {doc['title']}")
    except Exception as e:
        log(f"  (files dobara kholne mein masla: {e})")
    if HUB.running:
        return
    notify("Kaam khud dobara shuru", "Bijli / restart ke baad adhoora kaam wahin se chal raha hai.", sticky=False)
    log("AUTO-RESUME: adhoora kaam wahin se shuru (ban chuki images dobara nahi banengi).")
    start_run()


async def run_all(only=None, only_post=None):
    """only = file naam ka hissa -> sirf woh file (test ke liye)"""
    owner = HUB.run_task
    if owner is not None and owner is not asyncio.current_task() and not owner.done():
        return  # doosra worker kabhi nahi (same tab / same chat par do run = duplicate images)
    if HUB.run_task is not asyncio.current_task():
        HUB.pause_requested = HUB.paused = False
        HUB.paused_seconds = 0.0
        HUB.pause_started = None
        HUB.pause_signal = asyncio.Event()
        HUB.resume_signal = asyncio.Event()
        HUB.resume_signal.set()
    HUB.running = True
    HUB.run_task = asyncio.current_task()
    await HUB.publish_status()
    try:
        missing = [r.upper() for r in ROLES if r not in HUB.clients]
        if missing:
            log(f"Yeh profiles connected nahi: {', '.join(missing)} — un Chrome profiles ko kholo phir START.")
            return
        opt = [r.upper() for r in OPTIONAL_ROLES if r not in HUB.clients]
        if opt:
            log(f"(Note: {', '.join(opt)} connected nahi — un ke links wali images nahi aayengi)")
        state = load_state()
        tabs = await HUB.call("designer", "list_doc_tabs")
        tabs = list(reversed(tabs))  # extension returns tab order left-to-right; start at the right edge
        # ek hi file 2 tabs mein khuli ho -> sirf ek dafa line mein
        uniq = {}
        for t in tabs:
            m = re.search(r"/document/d/([^/]+)", t["url"])
            if not m:  # abhi khul rahi tab (URL poora nahi) -> agli check mein khud aa jayegi; run crash na ho
                log(f"  (tab abhi khul rahi hai: {t.get('title') or t['url'][:60]} — thori der baad line mein)")
                continue
            uniq.setdefault(m.group(1), t)
        if len(uniq) < len(tabs):
            log(f"({len(tabs) - len(uniq)} file 2 dafa khuli thi — har file sirf ek dafa banegi)")
        tabs = list(uniq.values())
        if only:
            tabs = [t for t in tabs if only.lower() in t["title"].lower()]
            log(f"(Test: sirf '{only}' wali file)")
        if not tabs and only:
            log("Designer profile mein koi Google Doc tab khuli nahi hai.")
            return
        log(f"{len(tabs)} files mili hain. Right wali tab se left ki taraf ek ek kar ke shuru.")
        cleanup_old_outputs()
        if not only and not only_post:  # bijli jaye to agli dafa khud resume ho sake
            set_run_marker(active=True, paused=False, docs=[{"url": t["url"], "title": t["title"]} for t in tabs])
        today = datetime.now().strftime("%Y-%m-%d")
        report = []  # aakhir ki summary
        prepared, preparation_errors = {}, {}
        log("Pehle saari pending files ki working copies format/check hongi; phir images.")
        async def prep(tab):
            doc_id = re.search(r"/document/d/([^/]+)", tab["url"]).group(1)
            if state.get(doc_id, {}).get("status") == "done" and not only_post:
                return
            folder = re.sub(r"\s*-\s*Google Docs\s*$", "", tab["title"])
            folder = re.sub(r'[<>:"/\\|?*]', "", re.sub(r"\.(docx?|pdf)$", "", folder, flags=re.I)).strip()
            try:
                prepared[doc_id] = await prepare_file(tab["tabId"], doc_id, OUT_ROOT / today / folder)
            except Exception as e:
                preparation_errors[doc_id] = str(e)
                log(f"  Format error: {folder}: {e}")

        def doc_id_of(tab):
            match = re.search(r"/document/d/([^/]+)", tab.get("url", ""))
            return match.group(1) if match else None

        HUB.designer_busy = True  # saari files ki working copies (Designer docs parhta hai)
        try:
            for tab in tabs:
                await prep(tab)
        finally:
            HUB.designer_busy = False
        queue, seen, i = list(tabs), {doc_id_of(t) for t in tabs}, 0
        net_retries = {}
        retry_later = set()  # 0 images wali files: baqi sab ke baad ek dafa dobara
        rest_next = False  # pichli file ChatGPT par bani -> agli se pehle aaram
        # bari file ka set bana -> agla set tab jab 2 aur files ban jayen (ya 30 min koi file na aaye)
        waiting, released, files_done = [], set(), 0
        rechecks = []  # (doc_id, tab_id, folder, kitni posts bani, files_done) — 2 files baad dobara gin kar check
        recent = []  # aakhri bani companies (company gap)

        def tab_company(t):
            c = company_from_title(t["title"])
            return company_alias(c) or c
        wait_after = CFG.get("big_file_after_files", 2)
        wait_max = CFG.get("big_file_max_wait_minutes", 30) * 60
        watch_until, reported = None, 0
        while True:
            for r in [r for r in rechecks if files_done - r[4] >= CFG.get("recheck_after_files", 2)]:
                rechecks.remove(r)
                await recheck_file(*r[:4])
            for w in list(waiting):  # (tab, files_done jab ruki, kab ruki, company — agar din ki limit)
                if w[3]:
                    ready = company_items(w[3][0]) + w[3][1] <= day_items()
                else:
                    ready = files_done - w[1] >= wait_after or run_time() - w[2] >= wait_max
                if ready:
                    waiting.remove(w)
                    released.add(doc_id_of(w[0]))
                    queue.insert(0, w[0])
                    log(f"Ab baari: {folder_of(w[0]['title'])}" + (f" (company ki {CFG.get('limit_window_hours', 12)} ghante ki limit khul gayi)" if w[3]
                                                                   else " (bari file ka agla set)"))
            HUB.waiting_notes = {norm(folder_of(w[0]["title"])): (
                "baad mein — company ki 12 ghante ki 6 posts (reels samait) puri" if w[3] else "bari file — agla set doosri companies ke baad")
                for w in waiting}
            HUB.waiting_limit_only = all(w[3] for w in waiting)
            if not queue and not only:
                # line khaali: summary do, phir har ~8 sec nayi kholi files dekhte raho (live)
                if len(report) > reported:
                    write_report(report, OUT_ROOT / today)
                    reported = len(report)
                    mins = CFG.get("watch_idle_minutes", 30)
                    log(f"Abhi tak ki saari files ho gayin. Nayi files ka intezar ({mins} min tak, har {CFG.get('watch_poll_seconds', 8)}s)...")
                    notify("Files ho gayin", f"{len(report)} files ka kaam ho gaya. Nayi file kholo to script khud shuru karegi.")
                    watch_until = run_time() + mins * 60
                elif watch_until is None:
                    log("Koi pending file nahi — nayi files ka intezar...")
                    watch_until = run_time() + CFG.get("watch_idle_minutes", 30) * 60
                if run_time() >= watch_until and not waiting:
                    break
                HUB.watching = True
                await run_sleep(CFG.get("watch_poll_seconds", 8))
            if (i or not queue) and not only and not HUB.no_new_files:
                # chalte run mein nayi kholi gayi files bhi line mein (right se left)
                try:
                    for t in reversed(await HUB.call("designer", "list_doc_tabs")):
                        did = doc_id_of(t)
                        if did in seen or state.get(did, {}).get("status") == "done":
                            continue
                        seen.add(did)
                        log(f"Nayi file mili: {t['title']} — line mein laga di")
                        add_marker_doc(t)
                        HUB.designer_busy = True
                        try:
                            await prep(t)
                        finally:
                            HUB.designer_busy = False
                        queue.append(t)
                except Exception as e:
                    log(f"  (nayi files check nahi ho saki: {e})")
            if not queue:
                if only and not waiting:
                    break
                if only:
                    await run_sleep(CFG.get("watch_poll_seconds", 8))
                continue  # watch mode: upar wala block hi khatam karta hai
            tab = queue.pop(0)
            i += 1
            HUB.queue_view = [folder_of(t["title"]) for t in queue]
            reload_config()
            total = i + len(queue)
            m = re.search(r"/document/d/([^/]+)", tab["url"])
            doc_id, title = m.group(1), tab["title"]
            st = state.get(doc_id, {})
            if st.get("status") == "partial" and doc_id not in released and not only_post:
                # restart ke baad bhi: pichle set ke baad kitni aur files bani? kam -> abhi nahi
                since = sum(1 for k, v in state.items() if k != doc_id and v.get("status") == "done"
                            and str(v.get("at", "")) > str(st.get("at", "")))
                age = (datetime.now() - datetime.fromisoformat(st["at"])).total_seconds() if st.get("at") else wait_max
                if since < wait_after and age < wait_max:
                    waiting.append((tab, files_done - since, run_time() - age, None))
                    log(f"[{i}/{total}] {folder_of(title)}: agla set {wait_after} aur files ke baad "
                        f"(ya {wait_max // 60} min) — ChatGPT ko is chat se aaram")
                    i -= 1
                    continue
            if state.get(doc_id, {}).get("status") == "done" and not only_post:
                log(f"[{i}/{total}] Pehle ho chuki: {title} — skip")
                continue
            company, terms = company_from_title(title), search_terms(title)
            alias = company_alias(company)
            if alias:  # file ek naam ki, chat/branding doosri company ki (Ficus Financial -> Beyond the Budget)
                log(f"  (alias: '{company}' ki file -> '{alias}' ki chat)")
                company, terms = alias, search_terms(alias)
            gap = CFG.get("company_gap_files", 2)
            if not only and not only_post and gap and norm(company) in recent[-gap:]                     and any(norm(tab_company(t)) not in recent[-gap:] for t in queue):
                # ek company ki file ke baad 2 doosri companies, phir isi ki agli (Dental ke hisse lagataar nahi)
                queue.append(tab)
                log(f"[{i}/{total}] {folder_of(title)}: {company} abhi bani — pehle {gap} doosri companies, phir yeh")
                i -= 1
                continue
            if not only and not only_post and doc_id not in released and not doc_items_logged(doc_id)                     and company_items(company) >= day_items():
                waiting.append((tab, files_done, run_time(), (company, 1)))
                log(f"[{i}/{total}] {folder_of(title)}: {company} ki {CFG.get('limit_window_hours', 12)} ghante mein {day_items()} posts "
                    f"(reels samait) ban chuki — yeh agle din (had khulte hi khud)")
                i -= 1
                continue
            log(f"[{i}/{total}] {title}  ->  company: {company}  (search: {' | '.join(terms)})")
            folder = folder_of(title)
            if norm(folder) in HUB.skip:  # dashboard se skip (yaad rehta hai jab tak aap unskip na karein)
                log(f"[{i}/{total}] SKIP (dashboard se): {title}")
                report.append({"file": folder, "reels": [], "done": [], "designs": 0, "error": None, "skipped": True})
                write_file_report(OUT_ROOT / today / folder, report[-1])
                continue
            summary = {"file": folder, "reels": [], "done": [], "designs": 0, "error": None, "only_post": only_post,
                       "doc_id": doc_id}
            if rest_next:  # pichli file ChatGPT par bani -> aaram, phir yeh file nayi (hard refresh) chat tab mein
                await chatgpt_rest(CFG.get("rest_between_files_minutes", 2), f"agli file '{folder}' se pehle")
                rest_next = False
            await wait_for_internet()  # net nahi -> yahin ruko (file fail nahi)
            report.append(summary)
            HUB.current = {"file": folder, "company": company, "i": i, "total": total, "since": time.time()}
            HUB.stuck_events = 0
            HUB.watching = False
            try:
                if doc_id in preparation_errors:
                    raise RuntimeError(preparation_errors[doc_id])
                try:
                    await HUB.call("designer", "tab_info", tabId=tab["tabId"])  # tab abhi khuli hai?
                except Exception:  # tab band kar di gayi -> link se dobara kholo
                    log("  (file ki tab band thi — dobara khol raha hoon)")
                    tab["tabId"] = await HUB.call("designer", "open_tab", url=tab["url"], active=True)
                await process_doc(tab["tabId"], doc_id, company, terms, OUT_ROOT / today / folder, summary,
                                  prepared_doc=prepared[doc_id])
                if move_story_issues(summary):  # sirf story reh gayi -> aap khud banaoge, file DONE
                    log(f"  Story skip (aap khud banaoge): {'; '.join(summary['story_skipped'])}")
                if summary.get("qa_fail") or summary.get("image_fail") or summary.get("dropped"):
                    raise RuntimeError("File incomplete — failed/missing images summary mein hain; DONE mark nahi ki")
                if summary.get("designs") and not summary.get("made"):  # 0 images bani -> DONE kabhi nahi
                    raise RuntimeError("File incomplete — 0 images bani; DONE mark nahi ki")
                if summary.get("parts_left"):  # bari file ka ek set bana -> baqi sets doosri companies ke baad
                    released.discard(doc_id)
                    waiting.append((tab, files_done, run_time(), None))
                    if not only_post:
                        state[doc_id] = {"status": "partial", "title": title, "at": datetime.now().isoformat(),
                                         "part": summary["part"]}
                    log(f"[{i}/{total}] SET {summary['part']} BAN GAYA: {title} — baqi {summary['parts_left']} set(s) "
                        f"{wait_after} aur files ke baad (ya {wait_max // 60} min) — DONE tab jab saare sets ban jayen")
                else:
                    if not only_post:  # sirf ek post ka test -> file ko "done" mat karo
                        clear_progress(doc_id)  # file poori -> adhoori images ka record khatam
                        state[doc_id] = {"status": "done", "title": title, "at": datetime.now().isoformat(),
                                         "reels_skipped": summary["reels"]}
                        if summary.get("split_into"):
                            state[doc_id]["split_into"] = summary["split_into"]
                        rechecks.append((doc_id, tab["tabId"], folder, summary.get("sections"), files_done))
                    n_reels = len(summary.get("reels") or [])
                    log(f"[{i}/{total}] DONE: {title}" + (f" — {n_reels} reel aap khud banao" if n_reels else ""))
            except DocSplit as e:
                # asal bari file khud nahi banti — hisse nayi docs (tabs) hain, agli baari line mein aa jayengi
                report.remove(summary)
                summary["skipped"] = True
                if not only_post:
                    state[doc_id] = {"status": "done", "title": title, "at": datetime.now().isoformat(),
                                     "split": [d for _, d in e.parts], "reels_skipped": []}
                log(f"[{i}/{total}] {folder}: {len(e.parts)} nayi docs mein baant di "
                    f"({', '.join(t for t, _ in e.parts)}) — har ek alag file ban kar banegi")
            except DayLimit as e:
                released.discard(doc_id)
                waiting.append((tab, files_done, run_time(), (e.company, e.need)))
                report.remove(summary)
                summary["skipped"] = True
                log(f"[{i}/{total}] {folder}: {e.company} ki aaj {company_items(e.company)} posts ban chuki (had "
                    f"{day_items()}, reels samait) — yeh {'set' if summary.get('part') else 'file'} ({e.need}) agle din")
            except FileSkipped:
                summary["skipped"] = True
                log(f"[{i}/{total}] SKIP (dashboard se, beech mein): {title} — jo images ban chuki woh mehfooz; DONE nahi")
            except Exception as e:
                if not await internet_ok() and net_retries.get(doc_id, 0) < CFG.get("net_retries_per_file", 5):
                    # beech mein internet gaya -> error nahi; net aane par YAHI file dobara (ban chuki images yaad hain)
                    net_retries[doc_id] = net_retries.get(doc_id, 0) + 1
                    report.remove(summary)
                    log(f"  Internet chala gaya ({e}) — net aate hi '{folder}' wahin se dobara")
                    await wait_for_internet()
                    queue.insert(0, tab)
                    i -= 1
                    continue
                if isinstance(e, WebsiteBrandingUnavailable):
                    summary["manual_reason"] = str(e)
                err_text = str(e) or f"{type(e).__name__} (timeout / koi jawab nahi)"
                log(f"  ERROR: {err_text}")
                traceback.print_exc()
                notify(f"ERROR: {folder}", err_text[:300] + " — script agli file par ja rahi hai")
                summary["error"] = err_text
                if not only_post:
                    state[doc_id] = {"status": "error", "title": title, "error": err_text}
                # koi image nahi bani -> baqi SAARI companies ke baad isi run mein EK dafa dobara
                # (ban chuki images yaad hain — sirf baqi banengi)
                if not summary.get("made") and doc_id not in retry_later and not only_post                         and not isinstance(e, (WebsiteBrandingUnavailable, ChatLimit)):
                    retry_later.add(doc_id)
                    queue.append(tab)
                    report.remove(summary)
                    log(f"  '{folder}' ki koi image nahi bani — baqi files ke baad dobara banegi")
            # records (source images, prompts, QA) sirf Output\<date>\<file>\ mein — Downloads mein nahi
            save_state(state)
            write_file_report(OUT_ROOT / today / folder, summary)  # dashboard: is file ka aakhri hal
            rest_next = bool(summary.get("designs"))  # ChatGPT par kaam hua -> agli file se pehle aaram
            if HUB.stuck_events >= CFG.get("chat_refresh_after_stucks", 2) and not only_post:
                # is chat mein baar baar atka -> agli dafa isi company ki NAYI chat (purani 'OLD - ...')
                mark_chat_refresh(company)
                log(f"  {company}: is file mein ChatGPT {HUB.stuck_events} dafa atka — agli file se pehle nayi chat banegi")
            if summary.get("made") and not only_post:
                recent.append(norm(company))  # company gap: 2 doosri companies ke baad hi isi ki agli file
            if summary.get("made") and not only_post and not summary.get("redo"):
                log_company_unit(company, doc_id, folder, summary.get("part"), summary.get("items", 3))
            if summary.get("made") and not summary.get("parts_left"):
                files_done += 1  # bari file ke intezar ki ginti
            HUB.current = {}
            HUB.designer_busy = False
            try:
                await HUB.call("designer", "focus_tab", tabId=tab["tabId"])
            except Exception:
                pass
        log("SAB FILES KHATAM. Script complete.")
        if report and (only or len(report) > reported):
            c = write_report(report, OUT_ROOT / today)
            notify("Post Automation: kaam khatam",
                   f"{c['files']} files: {c['files'] - c['bad']} poori, {c['bad']} mein masla. "
                   f"Aap ke kaam: {c['posts']} post, {c['stories']} story, {c['reels']} reel (SUMMARY file dekho).")
    except Exception as e:
        log(f"ERROR: {e}")
        traceback.print_exc()
    finally:
        HUB.current = {}
        HUB.queue_view = []
        HUB.watching = False
        HUB.designer_busy = False
        if not only and not only_post:
            clear_run_marker()  # kaam poora / normal band -> agli dafa khud start nahi
        HUB.running = False
        HUB.run_task = None
        HUB.pause_requested = HUB.paused = False
        HUB.pause_started = None
        HUB.pause_signal.clear()
        HUB.resume_signal.set()
        await HUB.publish_status()


def company_of_folder(name):
    """Dashboard: file folder ka naam -> company (alias ke saath: Ficus Financial -> Beyond the Budget)."""
    company = company_from_title(name)
    return company_alias(company) or company


def http_reply(connection, body, ctype="application/json; charset=utf-8", status=200):
    if not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False, default=str)
    r = connection.respond(status, body)
    del r.headers["Content-Type"]
    r.headers["Content-Type"] = ctype
    r.headers["Cache-Control"] = "no-store"
    return r


def restart_server(shutdown_only=False):
    """Dashboard se: server band (ya band kar ke 3 second baad START_SERVER.bat se dobara)."""
    import os
    import subprocess
    if not shutdown_only:
        # alag .bat (quote / & ka masla nahi): 3 second ruko (purana server port chhode) -> START_SERVER.bat
        bat = BASE / "RESTART_SERVER.bat"
        start_bat = BASE / "START_SERVER.bat"  # poora path (folder naam mein spaces)
        bat.write_bytes(f'@echo off\r\ncd /d "{BASE}"\r\nping -n 4 127.0.0.1 >nul\r\ncall "{start_bat}"\r\n'.encode("mbcs"))
        subprocess.Popen(["cmd", "/c", str(bat)], cwd=BASE, creationflags=subprocess.CREATE_NEW_CONSOLE)
    log("Server " + ("band ho raha hai (dashboard se)" if shutdown_only else "restart ho raha hai (dashboard se)"))
    asyncio.get_event_loop().call_later(0.8, os._exit, 0)


async def dashboard_action(do):
    busy = HUB.running and not HUB.paused
    if do == "start":
        if HUB.running:
            await HUB.resume() if HUB.pause_requested else log("Pehle se chal raha hai.")
        else:
            start_run(None, None)
        return {"ok": True, "message": "START bhej diya"}
    if do == "stop":
        if not HUB.running:
            return {"ok": False, "message": "Abhi kuch chal hi nahi raha"}
        await HUB.request_pause()
        return {"ok": True, "message": "STOP: current step khatam kar ke rukega"}
    if do in ("restart", "shutdown"):
        if busy:
            return {"ok": False, "message": "Kaam chal raha hai — pehle STOP dabao, 'Ruk gaya' aane do, phir dobara"}
        restart_server(shutdown_only=(do == "shutdown"))
        return {"ok": True, "message": "Server restart ho raha hai — 10 second mein page khud wapis aa jayega"
                if do == "restart" else "Server band ho gaya"}
    return {"ok": False, "message": f"Unknown action: {do}"}


SKIP_FILE = OUT_ROOT / "skip.json"  # dashboard skip restart ke baad bhi yaad (date docs / Molen tax har dafa)


def load_skips():
    try:
        HUB.skip |= set(json.loads(SKIP_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass


def save_skips():
    try:
        SKIP_FILE.write_text(json.dumps(sorted(HUB.skip), ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def dashboard_skip(do, file):
    """Dashboard ka Skip button: sirf woh file chhodo jis par aap ne Skip dabaya (baqi sab banti hain).
    Skip yaad rehta hai (restart ke baad bhi) jab tak aap 'unskip' na karein."""
    if not file:
        return {"ok": False, "message": "File ka naam nahi mila"}
    if do == "skip":
        HUB.skip.add(norm(file))
        save_skips()
        running = norm(HUB.current.get("file", "")) == norm(file)
        log(f"Dashboard: '{file}' skip " + ("— agle safe point par chhod kar agli file" if running else "— baari aane par"))
        return {"ok": True, "message": f"'{file}' skip hogi" + (" (abhi wali — agle safe point par)" if running else "")}
    HUB.skip.discard(norm(file))
    save_skips()
    log(f"Dashboard: '{file}' skip wapis — yeh file banegi")
    return {"ok": True, "message": f"'{file}' ab banegi"}


async def http_route(connection, request):
    """Extension pehle /health check karti hai. Browser: / = dashboard, /api/* = data. WebSocket waise hi."""
    from urllib.parse import parse_qsl
    if (request.headers.get("Upgrade") or "").lower() == "websocket":
        return None  # extensions ka WebSocket — dashboard ka hissa nahi
    path, _, qs = request.path.partition("?")
    q = dict(parse_qsl(qs))
    try:
        if path == "/health":
            return connection.respond(200, "ok\n")
        if path in ("/", "/dashboard"):
            return http_reply(connection, (BASE / "dashboard.html").read_text(encoding="utf-8"),
                              "text/html; charset=utf-8")
        if path == "/api/state":
            since, asince = int(q.get("log") or 0), int(q.get("alert") or 0)
            lines = [{"n": n, "line": l} for n, l in HUB.log_lines if n > since]
            today = dashboard_api.today_summary(OUT_ROOT, company_of_folder, load_state(), HUB.current.get("file"))
            return http_reply(connection, {
                "server": HUB.run_status() | {"current": HUB.current, "uptime": int(time.time() - HUB.started_at),
                                              "queue": queue_list(),
                                              "current_skip": norm(HUB.current.get("file", "")) in HUB.skip,
                                              "roles": list(ROLES) + list(OPTIONAL_ROLES), "port": PORT},
                "today": today, "log": lines[-600:], "log_seq": HUB.log_seq,
                "alerts": [a for a in HUB.alerts if a["id"] > asince], "alert_seq": HUB.alert_seq})
        if path == "/api/docs":
            # START se pehle: Designer mein khuli Google Docs (jis tarteeb se banengi: right se left) + skip / done
            if HUB.running or "designer" not in HUB.clients:
                return http_reply(connection, {"docs": [], "available": False})
            try:
                tabs = list(reversed(await HUB.call("designer", "list_doc_tabs", timeout=10)))
            except Exception:
                return http_reply(connection, {"docs": [], "available": False})
            state = load_state()
            docs = []
            for t in tabs:
                m = re.search(r"/document/d/([^/]+)", t["url"])
                f = folder_of(t["title"])
                docs.append({"file": f, "skip": norm(f) in HUB.skip,
                             "done": bool(m) and state.get(m.group(1), {}).get("status") == "done"})
            return http_reply(connection, {"docs": docs, "available": True})
        if path == "/api/references":
            return http_reply(connection, dashboard_references())
        if path == "/api/gmail":
            return http_reply(connection, gmail_watch.api_state())
        if path == "/api/history":
            return http_reply(connection, dashboard_api.history(OUT_ROOT, company_of_folder, load_state(),
                                                                HUB.current.get("file")))
        if path == "/api/action":
            if request.headers.get("X-Dashboard") != "1":  # sirf apna dashboard (doosri website nahi)
                return http_reply(connection, {"ok": False, "message": "forbidden"}, status=403)
            if q.get("do") == "save_references":
                return http_reply(connection, dashboard_references(q.get("company", ""), q.get("paths", "[]")))
            if q.get("do") == "open_folder":
                return http_reply(connection, dashboard_open_folder(q.get("path", "")))
            if q.get("do", "").startswith("gmail_"):
                return http_reply(connection, gmail_watch.api_action(q["do"], q))
            if q.get("do") in ("skip", "unskip"):
                return http_reply(connection, dashboard_skip(q["do"], q.get("file", "").strip()))
            return http_reply(connection, await dashboard_action(q.get("do", "")))
    except Exception as e:
        return http_reply(connection, {"ok": False, "message": str(e)}, status=500)
    return None


def queue_list():
    """Dashboard 'Line mein files': run ki line + Designer mein nayi kholi (abhi line mein nahi) + bari file ka intezar."""
    out = [{"file": f, "skip": norm(f) in HUB.skip} for f in HUB.queue_view]
    if not HUB.running:
        return out
    have = {norm(x["file"]) for x in out} | {norm(HUB.current.get("file", ""))}
    state = load_state()
    for t in HUB.open_docs:
        m = re.search(r"/document/d/([^/]+)", t.get("url", ""))
        f = folder_of(t.get("title", ""))
        st = state.get(m.group(1), {}).get("status") if m else None
        if not m or st == "done" or norm(f) in have:
            continue
        have.add(norm(f))
        out.append({"file": f, "skip": norm(f) in HUB.skip,
                    "note": HUB.waiting_notes.get(norm(f)) or ("bari file — agla set baad mein" if st == "partial"
                                                               else "nayi — agli baari mein line mein")})
    return out


def run_idle():
    """Koi kaam baqi nahi: run band, ya line khaali + sirf 12 ghante ki limit wali files ruki hain."""
    if not HUB.running:
        return True
    return (HUB.watching and not HUB.queue_view and not HUB.current and HUB.waiting_limit_only
            and HUB.rest_until <= time.time())


async def night_shutdown():
    """config 'night_stop_at' ("2026-10-20 04:00", PC ka waqt): us waqt Gmail search + nayi files band,
    jo files line mein hain woh poori banti hain, phir PC shut down (2 minute ki warning; 'shutdown /a' se ruk sakta)."""
    stopped = False
    idle_checks = 0
    while True:
        await asyncio.sleep(60)
        at = CFG.get("night_stop_at")
        if not at:
            stopped, idle_checks = False, 0
            continue
        try:
            when = datetime.strptime(at, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if datetime.now() < when:
            continue
        if not stopped:
            stopped = True
            HUB.no_new_files = True
            try:
                gmail_watch.api_action("gmail_toggle", {"on": "0"})
            except Exception as e:
                log(f"(Gmail band nahi hua: {e})")
            log(f"RAAT {at}: Gmail search aur nayi files band — jo line mein hain woh ban kar PC band hoga")
            notify("Raat ka waqt", "Gmail search + nayi files band. Baqi files ban kar PC shut down hoga.")
        idle_checks = idle_checks + 1 if run_idle() else 0
        if idle_checks >= CFG.get("night_idle_minutes", 3):  # 3 minute lagataar khaali -> pakka khatam
            log("Saari files ban gayin — PC 2 minute mein shut down (rokna ho to: shutdown /a)")
            notify("PC shut down", "Saara kaam khatam — PC 2 minute mein band hoga.")
            write_cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
            write_cfg["night_stop_at"] = ""  # ek dafa ka (agli raat dobara nahi)
            (BASE / "config.json").write_text(json.dumps(write_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            CFG["night_stop_at"] = ""
            import subprocess
            subprocess.Popen(["shutdown", "/s", "/t", "120", "/c", "Post Automation: saara kaam khatam"])
            return


async def watch_open_docs():
    """Har 20s Designer ki khuli Docs tabs dekho (sirf list — koi tab focus/click nahi): file chalte hue bhi
    aap ki nayi kholi file dashboard ki line mein fauran dikhe; run use agli baari mein line mein lagata hai."""
    while True:
        await asyncio.sleep(CFG.get("open_docs_poll_seconds", 20))
        if "designer" not in HUB.clients:
            continue
        try:
            HUB.open_docs = await HUB.call("designer", "list_doc_tabs", timeout=15) or []
        except Exception:
            pass


async def main():
    async with serve(handler, "127.0.0.1", PORT, max_size=None, ping_interval=20, ping_timeout=60,
                     process_request=http_route):
        print(f"Server chal raha hai (port {PORT}). Yeh window band NA karna.")
        print("Chaaron Chrome profiles kholo -> Designer extension ka START dabao.\n")
        print(f"DASHBOARD: http://127.0.0.1:{PORT}/   (OPEN_DASHBOARD.bat se bhi khulta hai)\n")
        cleanup_old_outputs()  # 7 din se purane Output\\<date> folders (space)
        if PORT == 8765:
            load_skips()  # pichli dafa ki skip ki hui files (date docs / Molen tax)
        asyncio.create_task(auto_resume())  # bijli / restart ke baad adhoora kaam khud
        gmail_watch.setup(sys.modules[__name__])
        asyncio.create_task(watch_open_docs())  # nayi kholi files dashboard par fauran
        asyncio.create_task(night_shutdown())  # raat ka waqt: nayi files band, kaam khatam -> PC band
        asyncio.create_task(gmail_watch.loop())  # dashboard ki Gmail list (OFF ho to kuch nahi karta)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
