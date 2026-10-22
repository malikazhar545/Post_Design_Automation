"""Persistent, exact-company reference overrides for the local dashboard."""
import json
import os
from pathlib import Path

MAX_REFERENCES = 10
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".heic"}


def company_key(name):
    return " ".join(name.split()).casefold()


def load(store):
    if not store.exists():
        return {}
    return json.loads(store.read_text(encoding="utf-8"))


def validate_paths(paths):
    if not isinstance(paths, list) or len(paths) > MAX_REFERENCES:
        raise ValueError("Maximum 10 image paths add kar sakte hain.")
    result = []
    for value in paths:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Har reference ka poora image file path chahiye.")
        path = Path(value.strip().strip('"')).expanduser()
        if not path.is_absolute():
            raise ValueError(f"Poora image file path chahiye: {value}")
        if path.suffix.lower() not in IMAGE_EXTENSIONS or not path.is_file():
            raise ValueError(f"Image file nahi mili ya format supported nahi: {value}")
        path = path.resolve()
        with path.open("rb") as image:
            if not image.read(1):
                raise ValueError(f"Image file khaali hai: {value}")
        if path in result:
            raise ValueError(f"Yeh reference do baar hai: {value}")
        result.append(path)
    return result


def save(store, company, paths):
    name = " ".join(company.split())
    if not name or len(name) > 200:
        raise ValueError("Company ka naam chahiye (maximum 200 characters).")
    validated = validate_paths(paths)
    data = load(store)
    key = company_key(name)
    if validated:
        data[key] = {"company": name, "paths": [str(p) for p in validated]}
    else:
        data.pop(key, None)
    store.parent.mkdir(parents=True, exist_ok=True)
    temporary = store.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, store)
    return data
