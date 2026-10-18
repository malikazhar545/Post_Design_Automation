import base64
import io
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

import server
from website_branding import (WebsiteBrandingUnavailable, website_candidates,
                              validate_snapshot, collect_website_branding)
from test_workflow import work_directory


def snapshot():
    return {"url": "https://laima.example/", "title": "Laima Academy", "fonts_loaded": True,
            "styles": [{"role": "heading", "font_family": '"Playfair Display", serif',
                        "font_weight": "600", "color": "rgb(30, 70, 90)", "background": "rgb(255, 255, 255)"},
                       {"role": "body", "font_family": '"Open Sans", sans-serif', "font_weight": "400"}],
            "logos": [{"src": "https://laima.example/logo.png", "label": "Laima Academy", "score": 150}]}


class WebsiteSelectionTests(unittest.TestCase):
    def test_document_website_is_selected_not_social_or_photo(self):
        text = "https://instagram.com/p/example https://freepik.com/photo.jpg https://other.example/article https://laima.example/admissions"
        urls = website_candidates(text, "Laima Academy")
        self.assertEqual(urls[:2], ["https://laima.example/", "https://laima.example/admissions"])
        self.assertNotIn("https://instagram.com/", urls)
        self.assertNotIn("https://freepik.com/", urls)
        self.assertEqual(website_candidates("Website: www.laima-academy.com", "Laima Academy"), ["https://www.laima-academy.com/", "https://www.laima-academy.com"])

    def test_unrelated_website_or_missing_logo_is_rejected(self):
        data = snapshot()
        validate_snapshot(data, "Laima Academy")
        data["url"], data["title"] = "https://other.example/", "Other Academy"
        with self.assertRaises(WebsiteBrandingUnavailable):
            validate_snapshot(data, "Laima Academy")
        data = snapshot()
        data["logos"] = []
        with self.assertRaises(WebsiteBrandingUnavailable):
            validate_snapshot(data, "Laima Academy")


class WebsiteWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_logo_fonts_and_evidence_are_saved_and_owned_tab_closed(self):
        png = io.BytesIO()
        Image.new("RGBA", (120, 60), (0, 60, 90, 255)).save(png, format="PNG")
        hub = AsyncMock()
        async def call(role, cmd, **kwargs):
            if cmd == "open_tab":
                return 123
            if cmd == "fetch":
                return {"status": 200, "contentType": "image/png", "b64": base64.b64encode(png.getvalue()).decode()}
            return True
        hub.call.side_effect = call
        hub.exec.return_value = snapshot()
        with work_directory() as root:
            brand = await collect_website_branding(hub, "Laima Academy", "https://laima.example/", root)
            self.assertTrue(brand["logo"].exists())
            self.assertIn('"Playfair Display", serif', brand["instructions"])
            self.assertIn('"Open Sans", sans-serif', brand["instructions"])
            self.assertTrue((root / "website_branding" / "brand-evidence.json").exists())
            hub.call.assert_any_await("designer", "close_tab", tabId=123)

    async def test_missing_website_does_not_open_tabs(self):
        hub = AsyncMock()
        with work_directory() as root:
            with self.assertRaisesRegex(WebsiteBrandingUnavailable, "website link bhi nahi"):
                await collect_website_branding(hub, "Laima Academy", "Heading: Hello", root)
        hub.call.assert_not_awaited()

    async def test_resolver_searches_both_profiles_before_website(self):
        chat = server.Chat("waseem")
        chat.find_chat = AsyncMock(return_value=(None, None))
        brand = {"instructions": "Measured branding " * 30, "logo": Path("logo.png"), "website": "https://laima.example/"}
        with patch.object(server, "get_instructions_from_wajid", new=AsyncMock(side_effect=server.BrandChatNotFound())) as wajid, \
             patch.object(server, "collect_website_branding", new=AsyncMock(return_value=brand)) as website, \
             patch.object(server, "create_waseem_chat", new=AsyncMock(return_value="new-cid")) as create, \
             patch.object(server, "log"):
            result = await server.resolve_company_chat(chat, "Laima Academy", ["Laima Academy", "Laima"], "document", Path("output"))
        self.assertEqual(result, ("new-cid", brand))
        wajid.assert_awaited_once()
        website.assert_awaited_once()
        self.assertEqual(create.call_args.kwargs["files"], [Path("logo.png")])

    async def test_existing_chat_skips_website_and_connection_error_is_not_missing_chat(self):
        chat = server.Chat("waseem")
        chat.find_chat = AsyncMock(return_value=("existing", "Laima"))
        with patch.object(server, "get_instructions_from_wajid", new=AsyncMock()) as wajid, \
             patch.object(server, "collect_website_branding", new=AsyncMock()) as website, patch.object(server, "log"):
            self.assertEqual(await server.resolve_company_chat(chat, "Laima", ["Laima"], "doc", Path("out")), ("existing", None))
            wajid.assert_not_awaited()
            website.assert_not_awaited()
            chat.find_chat.return_value = (None, None)
            wajid.side_effect = RuntimeError("connection failed")
            with self.assertRaisesRegex(RuntimeError, "connection failed"):
                await server.resolve_company_chat(chat, "Laima", ["Laima"], "doc", Path("out"))
            website.assert_not_awaited()

    async def test_manual_skip_does_not_create_empty_chat(self):
        chat = server.Chat("waseem")
        chat.find_chat = AsyncMock(return_value=(None, None))
        with patch.object(server, "get_instructions_from_wajid", new=AsyncMock(side_effect=server.BrandChatNotFound())), \
             patch.object(server, "create_waseem_chat", new=AsyncMock()) as create, patch.object(server, "log"):
            with self.assertRaises(WebsiteBrandingUnavailable):
                await server.resolve_company_chat(chat, "Laima", ["Laima"], "No links", Path("out"))
            create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
