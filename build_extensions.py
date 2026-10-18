"""extension_src se 4 extension folders banata hai (har profile ke liye ek). Code badle to dobara chalao."""
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent
SRC = BASE / "extension_src"
for role in ("designer", "waseem", "wajid", "instagram", "envato"):
    out = BASE / "extensions" / f"Extension_{role.upper()}"
    out.mkdir(parents=True, exist_ok=True)
    for f in ("background.js", "popup.html", "popup.js", "icon.png"):
        shutil.copy(SRC / f, out / f)
    (out / "manifest.json").write_text(
        (SRC / "manifest.json").read_text(encoding="utf-8").replace("__NAME__", role.upper()), encoding="utf-8")
    (out / "role.js").write_text(f"const ROLE = '{role}';\n", encoding="utf-8")
    print("built", out)
