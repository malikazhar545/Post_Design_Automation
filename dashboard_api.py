"""Dashboard ka data: Output\\<date>\\<file>\\ folders se date-wise record (kab, kis company ki kitni posts/stories),
aur har file ka aakhri hal (file_report.json). Server isay /api/... par deta hai — dashboard.html dikhata hai.
Purane din (7 din baad Output saaf ho jata hai) Output\\history.json mein mehfooz rehte hain."""
import json
import re
from datetime import datetime
from pathlib import Path

IMG_RE = re.compile(r"^\d{2}_(?P<name>.+)\.(png|jpe?g|webp)$", re.I)
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KINDS = ("post", "story", "carousel", "mockup")


def kind_of(name):
    """Image file ka naam -> post / story / carousel (slide) / mockup."""
    n = name.lower()
    if "story" in n:
        return "story"
    if "mockup" in n:
        return "mockup"
    if "carousel" in n or "slide" in n or re.search(r"(^|_)(before|after)(_|$)", n):
        return "carousel"
    return "post"


def empty_totals():
    return {k: 0 for k in KINDS} | {"files": 0, "done": 0, "issues": 0, "reels": 0}


def summary_reels(day_dir):
    """Purane din (file_report.json se pehle): SUMMARY_*.txt se har file ki reels (aap ne khud banani).
    Purana format: 'File  ->  ..., N reel skip' + '- [reel skip] x';  naya: '> File' + 'REEL (khud banao)  x'."""
    out = {}
    for path in sorted(Path(day_dir).glob("SUMMARY_*.txt")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        cur, seen = None, {}
        for line in lines:
            m = re.match(r"^(\S.*?)\s{2}->\s{2}", line) or re.match(r"^> (.+?)(?:\s{3}\(.*\))?$", line)
            if m:
                cur = m.group(1).strip()
                seen.setdefault(cur, [])
                continue
            m = re.match(r"^\s+- \[reel skip\] (.+)$", line) or re.match(r"^\s+REEL \(khud banao\)\s+(.+)$", line)
            if m and cur:
                seen[cur].append(m.group(1).strip())
        for f, reels in seen.items():  # baad wali summary pehle wali ko badal deti hai
            if reels or f not in out:
                out[f] = reels
    return out


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default



def _downloads_roots():
    """config final_dir + Downloads — Open Folder ke candidates."""
    roots = []
    cfg = read_json(Path(__file__).resolve().parent / "config.json", {})
    if cfg.get("final_dir"):
        roots.append(Path(cfg["final_dir"]).expanduser())
    roots.append(Path.home() / "Downloads")
    out, seen = [], set()
    for r in roots:
        try:
            key = str(r.resolve()).lower()
        except OSError:
            key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def resolve_open_folder(folder, company, report=None):
    """Har created file: report.final_dir → Downloads\\date\\company\\… → Output folder."""
    # Sirf Downloads wala folder — Output folder KABHI nahi. Folder hata diya ho to bhi wahi path
    # (Open Folder phir 'nahi mila' batata hai).
    report = report or {}
    raw = report.get("final_dir")
    if raw:
        return str(Path(raw))
    day = folder.parent.name if DAY_RE.match(folder.parent.name or "") else None
    company_safe = re.sub(r'[<>:"/\\|?*]', "", company or "").strip()
    if day and company_safe:
        rest = folder.name
        for w in company_safe.split():
            if rest.lower().startswith(w.lower()):
                rest = rest[len(w):].lstrip(" -_")
            else:
                break
        rest = re.sub(r'[<>:"/\\|?*]', "", rest).strip(" .")
        for root in _downloads_roots():
            base = root / day / company_safe
            if not base.is_dir():
                continue
            if rest:
                cand = base / rest
                try:
                    if cand.is_dir() and any(cand.iterdir()):
                        return str(cand.resolve())
                except OSError:
                    pass
            try:
                kids = [c for c in base.iterdir() if c.is_dir()]
            except OSError:
                kids = []
            nonempty = []
            for c in kids:
                try:
                    if any(c.iterdir()):
                        nonempty.append(c)
                except OSError:
                    pass
            if rest:
                num = re.search(r"\d+", rest)
                if num:
                    hit = [c for c in nonempty if num.group(0) in c.name]
                    if len(hit) == 1:
                        return str(hit[0].resolve())
            if len(nonempty) == 1:
                return str(nonempty[0].resolve())
            try:
                if any(base.iterdir()):
                    return str(base.resolve())
            except OSError:
                pass
    return None


def scan_file(folder, company_fn, state_by_title, current_folder=None, old_reels=None):
    """Ek file folder: kitni images (qism-wise), kab bani, kya hal hai, kya masle."""
    counts = {k: 0 for k in KINDS}
    times = []
    for f in folder.iterdir():
        m = IMG_RE.match(f.name)
        if m and f.is_file():
            counts[kind_of(m.group("name"))] += 1
            times.append(f.stat().st_mtime)
    report = read_json(folder / "file_report.json", {})
    reels = report.get("reels") if "reels" in report else (old_reels or {}).get(folder.name, [])
    if not times and not report and not reels:
        return None
    status = report.get("status")
    if folder.name == current_folder:
        status = "running"
    elif not status:
        st = state_by_title.get(folder.name.lower())
        status = "done" if st == "done" else ("error" if st == "error" else "unknown")
    issues = []
    for key, label in (("image_fail", "Nahi bani"), ("qa_fail", "Ghalat bani"), ("dropped", "Samajh nahi aayi")):
        for x in dict.fromkeys(report.get(key) or []):  # retry mein wahi masla dobara likha gaya -> ek hi dafa dikhao
            issues.append({"type": label, "text": x})
    if report.get("error") and not issues:
        issues.append({"type": "File error", "text": report["error"]})
    todo = [{"type": "Story khud banao", "text": x} for x in report.get("story_skipped") or []]
    todo += [{"type": "Post khud banao (2 dafa nahi bani)", "text": x} for x in report.get("manual_todo") or []]
    todo += [{"type": "Reel khud banao", "text": x} for x in reels or []]
    company = company_fn(folder.name)
    return {"file": folder.name, "company": company, "counts": counts, "status": status,
            "first": min(times) if times else None, "last": max(times) if times else None,
            "issues": issues, "todo": todo, "reels": len(reels or []),
            "final_dir": resolve_open_folder(folder, company, report),
            "finished": report.get("finished"), "generation_retry_at": report.get("generation_retry_at")}


def scan_day(day_dir, company_fn, state_by_title, current_folder=None):
    files, old_reels = [], summary_reels(day_dir)
    for folder in sorted(p for p in day_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
        try:
            rec = scan_file(folder, company_fn, state_by_title, current_folder, old_reels)
        except Exception:
            rec = None
        if rec:
            files.append(rec)
    return files


def resolve_company_folder(date, company, files):
    """Company-level Open Folder: Downloads\\date\\company (warna pehli file ke folder ka parent)."""
    company_safe = re.sub(r'[<>:"/\\|?*]', "", company or "").strip()
    if date and company_safe and DAY_RE.match(date):
        for root in _downloads_roots():
            base = root / date / company_safe
            try:
                if base.is_dir() and any(base.iterdir()):
                    return str(base.resolve())
            except OSError:
                pass
    for f in files or []:
        fd = f.get("final_dir")
        if not fd:
            continue
        p = Path(fd)  # Downloads\date\company\<file> -> us company ka Downloads folder
        return str(p.parent if company_safe and p.parent.name.lower() == company_safe.lower() else p)
    return None


def summarize(date, files):
    totals, companies = empty_totals(), {}
    for f in files:
        key = re.sub(r"\s+", " ", f["company"]).strip().lower()  # "Beyond the budget" == "Beyond the Budget"
        c = companies.setdefault(key, {"company": f["company"], "totals": empty_totals(), "files": []})
        c["files"].append(f)
        for t in (totals, c["totals"]):
            for k in KINDS:
                t[k] += f["counts"][k]
            t["files"] += 1
            t["done"] += f["status"] == "done"
            t["issues"] += len(f["issues"])
            t["reels"] += f.get("reels", 0)
    for c in companies.values():
        c["company_dir"] = resolve_company_folder(date, c["company"], c["files"])
    return {"date": date, "totals": totals,
            "companies": sorted(companies.values(), key=lambda c: -sum(c["totals"][k] for k in KINDS))}


def state_titles(state):
    """state.json (doc_id -> {status, title}) -> folder naam -> status."""
    out = {}
    for v in (state or {}).values():
        title = re.sub(r"\s*-\s*Google Docs\s*$", "", v.get("title", ""))
        title = re.sub(r'[<>:"/\\|?*]', "", re.sub(r"\.(docx?|pdf)$", "", title, flags=re.I)).strip()
        out[title.lower()] = v.get("status")
    return out


def today_summary(out_root, company_fn, state, current_folder=None):
    date = datetime.now().strftime("%Y-%m-%d")
    day = Path(out_root) / date
    files = scan_day(day, company_fn, state_titles(state), current_folder) if day.is_dir() else []
    return summarize(date, files)


def history(out_root, company_fn, state, current_folder=None):
    """Saare din (naye pehle). Output mein jo din hain dobara gine jate hain; mit chuke din history.json se."""
    out_root = Path(out_root)
    cache_path = out_root / "history.json"
    cache = read_json(cache_path, {})
    titles = state_titles(state)
    for day in out_root.iterdir() if out_root.is_dir() else []:
        if day.is_dir() and DAY_RE.match(day.name):
            files = scan_day(day, company_fn, titles, current_folder)
            if files:
                cache[day.name] = summarize(day.name, files)
    try:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return [cache[d] for d in sorted(cache, reverse=True)]
