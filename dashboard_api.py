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
        issues += [{"type": label, "text": x} for x in report.get(key) or []]
    if report.get("error") and not issues:
        issues.append({"type": "File error", "text": report["error"]})
    todo = [{"type": "Story khud banao", "text": x} for x in report.get("story_skipped") or []]
    todo += [{"type": "Reel khud banao", "text": x} for x in reels or []]
    return {"file": folder.name, "company": company_fn(folder.name), "counts": counts, "status": status,
            "first": min(times) if times else None, "last": max(times) if times else None,
            "issues": issues, "todo": todo, "reels": len(reels or []), "final_dir": report.get("final_dir"),
            "finished": report.get("finished")}


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
