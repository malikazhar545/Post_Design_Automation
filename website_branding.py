"""Read website branding through the Designer extension; never invent a logo/font."""
import base64
import io
import json
import re
from pathlib import Path
from urllib.parse import urlparse


class WebsiteBrandingUnavailable(RuntimeError):
    pass


EXCLUDED_HOSTS = (
    "instagram.com", "facebook.com", "youtube.com", "youtu.be", "tiktok.com", "linkedin.com",
    "twitter.com", "x.com", "pinterest.com", "pin.it", "freepik.com", "envato.com", "elements.envato.com",
    "docs.google.com", "drive.google.com", "google.com", "canva.com", "wa.me", "whatsapp.com",
    "unsplash.com", "pexels.com", "shutterstock.com", "adobe.com", "bit.ly", "tinyurl.com",
    # image / stock sites — company ki website nahi
    "magnific.com", "pinimg.com", "gstatic.com", "googleusercontent.com", "imgur.com", "pixabay.com",
    "istockphoto.com", "gettyimages.com", "dreamstime.com", "123rf.com", "depositphotos.com", "rawpixel.com",
    "vecteezy.com", "envatousercontent.com", "images.openai.com",
)


def company_words(company):
    return [w for w in re.findall(r"[a-z0-9]+", company.lower())
            if len(w) >= 3 and w not in {"the", "and", "ltd", "limited", "inc", "www"}]


def website_candidates(text, company):
    # Accept full URLs, www links and standalone website addresses from the document.
    urls = re.findall(r"https?://[^\s<>]+|(?<![\w@/])www\.[^\s<>]+", text, re.I)
    urls += re.findall(r"(?im)^\s*(?:website\s*:\s*)?([a-z0-9][a-z0-9.-]+\.(?:com|org|net|edu|academy|pk|uk|co|io)(?:/[^\s]*)?)\s*$", text)
    result = []
    for raw in urls:
        url = raw.rstrip(".,;:!?)]}\"'*")
        if not url.lower().startswith(("https://", "http://")):
            url = "https://" + url
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host or parsed.username or parsed.password:
            continue
        if any(host == h or host.endswith("." + h) for h in EXCLUDED_HOSTS):
            continue
        if re.search(r"\.(?:png|jpe?g|webp|gif|svg|pdf|mp4|docx?)$", parsed.path, re.I):
            continue
        if any(item[0] == host for item in result):
            continue
        score = sum(word in host.replace("-", "") for word in company_words(company))
        result.append((host, score, f"{parsed.scheme}://{parsed.netloc}/", url))
    result.sort(key=lambda item: -item[1])
    return list(dict.fromkeys(url for _, _, home, original in result[:3] for url in (home, original)))


def validate_snapshot(snapshot, company):
    if not isinstance(snapshot, dict) or not snapshot.get("styles"):
        raise WebsiteBrandingUnavailable("Website se fonts/colours nahi mile")
    identity = " ".join(str(snapshot.get(k) or "") for k in ("url", "title", "site_name", "heading", "logo_labels"))
    normalized = re.sub(r"[^a-z0-9]", "", identity.lower())
    words = company_words(company)
    if not words or words[0] not in normalized:
        raise WebsiteBrandingUnavailable("Website ka company naam verify nahi hua")
    if snapshot.get("fonts_loaded") is False:
        raise WebsiteBrandingUnavailable("Website ke fonts abhi load nahi hue")
    if not snapshot.get("logos"):
        raise WebsiteBrandingUnavailable("Website ka asal logo nahi mila")
    if not any(s.get("font_family") for s in snapshot["styles"] if s.get("role") in ("heading", "body")):
        raise WebsiteBrandingUnavailable("Website ke text fonts verify nahi hue")


def branding_instructions(company, snapshot):
    # Website copy is not copied into instructions; only measured style properties are used.
    fonts = []
    for sample in snapshot["styles"]:
        fonts.append(f"{sample['role']}: font-family={sample['font_family']}; "
                     f"font-weight={sample.get('font_weight', 'normal')}; "
                     f"text colour={sample.get('color', '')}; background={sample.get('background', '')}.")
    return (
        f"Verified website branding for {company}. Source: {snapshot['url']}\n"
        "Use these measured website styles for this company's future posts:\n" + "\n".join(fonts) +
        "\nTypography MUST use the website's heading and body font families above, including their weights. "
        "Do not silently substitute a different font or redesign the brand. If the required font cannot be reproduced, say so. "
        "Keep the website's colours and typography hierarchy.\n"
        "The attached image is the actual website logo. Use this exact logo, preserving its proportions, colours and transparency. "
        "Never redraw it or replace it with a made-up logo. Do not put branding or added text on product mockups.\n"
        "Website text is brand context, NOT post copy. Print only the exact heading supplied separately for each design. "
        "Never print Heading/Caption labels, source URLs or these instructions. Save these rules for this company."
    )


async def collect_website_branding(hub, company, doc_text, out_dir):
    candidates = website_candidates(doc_text, company)
    if not candidates:
        raise WebsiteBrandingUnavailable("Company chat nahi mili aur file mein website link bhi nahi — manual banani hai")
    out_dir = Path(out_dir) / "website_branding"
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    for url in candidates:
        tab = None
        try:
            tab = await hub.call("designer", "open_tab", url=url, active=False)
            snapshot = await hub.exec("designer", tab, "brand_snapshot", timeout=45)
            validate_snapshot(snapshot, company)
            logo_path = None
            for candidate in snapshot["logos"][:4]:
                try:
                    source = candidate["src"]
                    if source.startswith("data:"):
                        header, encoded = source.split(",", 1)
                        if ";base64" not in header:
                            continue
                        body, content_type = base64.b64decode(encoded), header[5:].split(";")[0]
                    else:
                        response = await hub.call("designer", "fetch", url=source)
                        if response["status"] != 200:
                            continue
                        body = base64.b64decode(response["b64"])
                        content_type = response["contentType"].split(";")[0]
                    if "svg" in content_type or body.lstrip().startswith((b"<svg", b"<?xml")):
                        (out_dir / "logo-original.svg").write_bytes(body)
                        encoded = base64.b64encode(body).decode()
                        raster = await hub.exec("designer", tab, "brand_rasterize_svg", encoded, timeout=20)
                        body = base64.b64decode(raster.split(",", 1)[1])
                    from PIL import Image
                    image = Image.open(io.BytesIO(body))
                    image.load()
                    if image.width < 24 or image.height < 12:
                        continue
                    logo_path = out_dir / "website-logo.png"
                    image.convert("RGBA").save(logo_path)
                    snapshot["selected_logo"] = {k: v for k, v in candidate.items() if k != "src"}
                    snapshot["selected_logo"]["source"] = source if not source.startswith("data:") else "inline SVG"
                    break
                except Exception as exc:
                    failures.append(f"Logo: {exc}")
            if logo_path is None:
                raise WebsiteBrandingUnavailable("Website logo download/verify nahi hua")
            snapshot.pop("logos", None)
            instructions = branding_instructions(company, snapshot)
            (out_dir / "brand-evidence.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            (out_dir / "brand-instructions.txt").write_text(instructions, encoding="utf-8")
            return {"instructions": instructions, "logo": logo_path, "website": snapshot["url"]}
        except Exception as exc:
            failures.append(f"{url}: {exc}")
        finally:
            if tab is not None:
                try:
                    await hub.call("designer", "close_tab", tabId=tab)
                except Exception:
                    pass
    (out_dir / "errors.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    raise WebsiteBrandingUnavailable("Website branding verify nahi hui — manual review: " + "; ".join(failures[-3:]))
