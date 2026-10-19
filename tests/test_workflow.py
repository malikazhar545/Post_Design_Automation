import asyncio
import re
import shutil
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import server

# tests apni settings patch karte hain; config.json dobara parhne se woh na badlen
server.reload_config = lambda: None  # _no_reload
# tests asli chat_memory.json ko kabhi na chhuyen  # _test_memory
server.CHAT_MEMORY_FILE = server.BASE / "tests" / "_runs" / "chat_memory_test.json"
server.CHAT_MEMORY.clear()
server.PROGRESS_FILE = server.BASE / "tests" / "_runs" / "progress_test.json"  # _test_progress
server.RUN_MARKER = server.BASE / "tests" / "_runs" / "run_active_test.json"  # _test_marker
server.write_file_report = lambda *a, **k: None  # tests asli Output mein dashboard records na likhen


@contextmanager
def work_directory():
    parent = (server.BASE / "tests" / "_runs").resolve()
    root = parent / uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        assert root.resolve().is_relative_to(parent) and root.resolve() != parent
        shutil.rmtree(root)


class FormattingTests(unittest.TestCase):
    def test_generate_is_not_a_rate_limit_notice(self):
        for message in ("I will generate the images now.", "Separate mockups generated.",
                        "Please generate a corporate design."):
            self.assertFalse(server.is_chat_limit_notice(message))
        for message in ("You have reached your image generation limit.",
                        "Chat paused until usage resets at 8:38 PM", "Rate limit reached"):
            self.assertTrue(server.is_chat_limit_notice(message))

    def test_labels_are_lossless_and_idempotent(self):
        original = [("First headline — exact!", False), ("https://example.com/products/item", False),
                    ("A caption with its own punctuation, spelling and words.", False),
                    ("Post 2", True), ("Second headline", False),
                    ("https://example.com/products/second", False), ("Another caption.", False)]
        prepared, review = server.prepare_doc_format(original)
        inserted = set(review["inserted_row_indices"])
        self.assertEqual([row for i, row in enumerate(prepared) if i not in inserted], original)
        self.assertFalse(review["warnings"])
        again, second = server.prepare_doc_format(prepared)
        self.assertEqual(again, prepared)
        self.assertEqual(second["inserted_labels"], 0)
        designs, _, _ = server.parse_doc(prepared)
        self.assertEqual([d["content"] for d in designs], [original[0][0], original[4][0]])

    def test_formatted_file_is_unchanged_and_multiline_heading_survives(self):
        rows = [("Post 1", True), ("Heading:", True), ("Line one", False),
                ("Line two", False), ("Caption:", True), ("Caption text.", False),
                ("https://example.com/products/item", False)]
        prepared, review = server.prepare_doc_format(rows)
        self.assertEqual(prepared, rows)
        designs, _, _ = server.parse_doc(prepared)
        self.assertEqual(designs[0]["content"], "Line one\nLine two")
        message, _, _ = server.build_design_message("Example", designs)
        self.assertIn("Line one\nLine two\nEND OF EXACT HEADING", message)
        self.assertNotIn("Caption text.", message)
        self.assertIn("NEVER print these labels", message)

    def test_ambiguous_boundary_requires_review(self):
        _, review = server.prepare_doc_format([("Short line one", False), ("Short line two", False)])
        self.assertTrue(review["warnings"])

    def test_saved_documents_preserve_every_row(self):
        for path in (server.BASE / "Output" / "2026-10-16").glob("*/doc_text.txt"):
            with self.subTest(file=path.parent.name):
                rows = [(s[2:-2], True) if s.startswith("**") and s.endswith("**") else (s, False)
                        for s in path.read_text(encoding="utf-8").splitlines()]
                prepared, review = server.prepare_doc_format(rows)
                inserted = set(review["inserted_row_indices"])
                self.assertEqual([r for i, r in enumerate(prepared) if i not in inserted], rows)
                self.assertFalse(review["warnings"])
                self.assertEqual(server.prepare_doc_format(prepared)[0], prepared)
                designs, reels, done = server.parse_doc(prepared)
                covered = {d.get("section", d["label"]) for d in designs}
                for sec in server.split_sections(prepared):
                    if sec["label"] in covered or any(x.startswith(sec["label"]) for x in reels + done):
                        continue
                    # sirf khaali section (label ke ilawa na text na link) chhoot sakta hai — woh summary mein jata hai
                    real = [l for l in sec["lines"] if l and not server.HEADING_RE.match(l)
                            and not server.CAPTION_RE.match(l) and not re.fullmatch(r"[=\-_*~#\s]{3,}", l)]
                    self.assertFalse(real, f"{sec['label']} chhoot gaya: {real[:2]}")


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_wajid_reuses_last_instructions_and_limit_backup(self):
        instructions = "Use these existing company instructions with the exact saved brand rules. " * 8
        def message(role, text, hidden=False):
            return {"author": {"role": role}, "content": {"content_type": "text", "parts": [text]},
                    "metadata": {"is_visually_hidden_from_conversation": hidden}}
        messages = [message("user", "give me whole instructions of this company"),
                    message("user", "hidden context", True), message("assistant", instructions),
                    message("user", "hidden follow-up", True)]
        chat = server.Chat("wajid")
        chat.find_chat = AsyncMock(return_value=("cid", "E Liquid"))
        chat.goto = AsyncMock()
        chat.conversation = AsyncMock(return_value={})
        chat.send = AsyncMock(side_effect=server.ChatLimit("Chat paused until usage resets"))
        with patch.object(server, "Chat", return_value=chat), \
             patch.object(server, "ordered_messages", return_value=messages), \
             patch.object(server, "log"), patch.object(server, "notify"):
            result = await server.get_instructions_from_wajid("Example", ["Example"], "content")
            self.assertEqual(result, instructions.strip())
            chat.send.assert_not_awaited()
            messages.append(message("user", "Another request"))
            result = await server.get_instructions_from_wajid("Example", ["Example"], "content")
            self.assertEqual(result, instructions.strip())
            chat.send.assert_awaited_once()
            messages.clear()
            with self.assertRaisesRegex(RuntimeError, "purani instructions"):
                await server.get_instructions_from_wajid("Example", ["Example"], "content")

    async def test_active_generation_is_not_cut_off_at_wait_deadline(self):
        chat = server.Chat("waseem")
        chat.generating = AsyncMock(side_effect=[True, True, True, False])
        chat.conversation = AsyncMock(return_value={})
        chat.send = AsyncMock()
        settings = dict(server.CFG["timeouts_minutes"], image_generation=0)
        with patch.dict(server.CFG, {"timeouts_minutes": settings}), \
             patch.object(server.time, "time", return_value=1000), \
             patch.object(server, "image_file_ids", side_effect=[[]] + [["new-image"]] * 20), \
             patch.object(server.asyncio, "sleep", new=AsyncMock()), patch.object(server, "log"):
            result = await server.wait_for_images(chat, "cid", set(), 1)
        self.assertEqual(result, ["new-image"])
        self.assertEqual(chat.generating.await_count, 4)
        chat.send.assert_not_awaited()

    async def test_all_files_prepared_before_generation_and_errors_continue(self):
        tabs = [{"tabId": i, "url": f"https://docs.google.com/document/d/doc{i}/edit",
                 "title": f"Company {i} - Google Docs"} for i in (1, 2)]
        events = []

        async def call(role, command, **kwargs):
            return tabs if command == "list_doc_tabs" else {}

        async def prepare(tab_id, doc_id, out_dir):
            events.append(f"prepare{tab_id}")
            return [("Heading:", True), ("Exact heading", False)]

        async def process(tab_id, *args, **kwargs):
            events.append(f"generate{tab_id}")
            if tab_id == 2:
                raise server.ChatLimit("limit still active")

        with patch.dict(server.HUB.clients, {role: object() for role in server.ROLES}, clear=True), \
             patch.object(server.HUB, "call", side_effect=call), \
             patch.object(server.HUB, "send", new=AsyncMock()), \
             patch.object(server, "prepare_file", side_effect=prepare), \
             patch.object(server, "process_doc", side_effect=process), \
             patch.object(server, "load_state", return_value={}), \
             patch.object(server, "save_state") as saved, \
             patch.object(server, "write_report") as report, \
             patch.object(server, "notify"), patch.object(server, "log"), \
             patch.object(server.traceback, "print_exc"), \
             patch.dict(server.CFG, {"watch_idle_minutes": 0}),              patch.object(server.asyncio, "sleep", new=AsyncMock()) as sleep:
            await server.run_all()
        self.assertEqual(events, ["prepare2", "prepare1", "generate2", "generate1"])
        sleep.assert_not_awaited()
        self.assertEqual(saved.call_args.args[0]["doc2"]["status"], "error")
        self.assertEqual(saved.call_args.args[0]["doc1"]["status"], "done")
        self.assertTrue(report.call_args.args[0][0]["error"])
        self.assertIsNone(report.call_args.args[0][1]["error"])

    async def test_mockups_are_requested_separately_and_saved(self):
        with work_directory() as tmp:
            root = Path(tmp)
            product = root / "product.png"
            product.write_bytes(b"fixture")
            design = server.new_design("post", "Post 25", "A heading")
            design["product_path"] = product
            work, final = root / "work", root / "downloads"
            work.mkdir()
            final.mkdir()
            (work / "source_content.txt").write_text("Writer's content **unchanged**", encoding="utf-8")
            chat = server.Chat("waseem")
            chat.conversation = AsyncMock(return_value={})
            chat.send = AsyncMock()
            chat.goto = AsyncMock()
            events = []

            async def send(message, files):
                events.append(("send", message))

            async def ready(*args, **kwargs):
                events.append(("ready", None))
                return [f"id-{len(events)}"]

            async def download(chat, cid, fid, dest):
                path = Path(str(dest) + ".png")
                path.write_bytes(f"fixture-{fid}".encode())  # har generated image alag
                return path

            chat.send.side_effect = send
            with patch.dict(server.CFG, {"mockup_companies": ["example"], "mockups_in_one_message": False, "batch_mode": False}), \
                 patch.object(server, "image_file_ids", return_value=[]), \
                 patch.object(server, "wait_for_images", side_effect=ready), \
                 patch.object(server, "download_generated", side_effect=download), \
                 patch.object(server, "qa_batch", new=AsyncMock(return_value=([], []))), \
                 patch.object(server, "final_dir", return_value=final), patch.object(server, "log"):
                await server.generate_and_download(chat, "cid", "Example", [design], work, {})
            self.assertEqual([e[0] for e in events], ["send", "ready", "send", "ready"])
            self.assertIn("Mockup 1 (4:5):", events[0][1])
            self.assertNotIn("Mockup 2 (4:5):", events[0][1])
            self.assertIn("Mockup 2 (4:5):", events[2][1])
            self.assertNotIn("Mockup 1 (4:5):", events[2][1])
            self.assertEqual(len(list(final.glob("*.png"))), 2)
            # Downloads mein SIRF ChatGPT images — content/records Output mein
            self.assertEqual(sorted(p.suffix for p in final.iterdir()), [".png", ".png"])

    async def test_rate_limit_wait_is_two_minutes_and_bounded(self):
        with work_directory() as tmp:
            chat = server.Chat("waseem")
            chat.conversation = AsyncMock(return_value={})
            chat.send = AsyncMock(side_effect=server.ChatLimit("paused"))
            chat.goto = AsyncMock()
            with patch.object(server, "image_file_ids", return_value=[]), \
                 patch.object(server.asyncio, "sleep", new=AsyncMock()) as sleep, \
                 patch.object(server, "notify"), patch.object(server, "log"):
                with self.assertRaises(server.ChatLimit):
                    await server.generate_and_download(chat, "cid", "Example",
                        [server.new_design("post", "Post 1", "Exact heading")], Path(tmp), {})
            sleep.assert_awaited_once_with(120)
            self.assertEqual(chat.send.await_count, 2)


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    """Ek post / QA / limit ka masla poori file ka kaam na roke; images apni hi post se judi rahen."""

    def _harness(self, ids_per_call):
        chat = server.Chat("waseem")
        chat.conversation = AsyncMock(return_value={})
        chat.send = AsyncMock(return_value=0)
        chat.goto = AsyncMock()
        calls = iter(ids_per_call)

        async def ready(*args, **kwargs):
            item = next(calls)
            if isinstance(item, Exception):
                raise item
            return item

        async def download(chat, cid, fid, dest):
            path = Path(str(dest) + ".png")
            path.write_bytes(fid.encode())
            return path
        return chat, ready, download

    async def _run(self, designs, ids_per_call, qa=([], [])):
        with work_directory() as tmp:
            work, final = tmp / "work", tmp / "final"
            work.mkdir(), final.mkdir()
            chat, ready, download = self._harness(ids_per_call)
            summary = {}
            error = None
            with patch.object(server, "image_file_ids", return_value=[]), \
                 patch.object(server, "wait_for_images", side_effect=ready), \
                 patch.object(server, "download_generated", side_effect=download), \
                 patch.object(server, "qa_batch", new=AsyncMock(return_value=qa)), \
                 patch.object(server.asyncio, "sleep", new=AsyncMock()), \
                 patch.object(server, "final_dir", return_value=final), \
                 patch.object(server, "run_sleep", new=AsyncMock()), \
                 patch.object(server, "notify"), patch.object(server, "log"):
                try:
                    await server.generate_and_download(chat, "cid", "Example", designs, work, summary)
                except Exception as e:
                    error = e
            saved = {p.stem: p.read_text() for p in final.glob("*.png")}
            return saved, summary, error

    async def test_missing_image_does_not_shift_or_stop_others(self):
        designs = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in (1, 2, 3)]
        saved, summary, error = await self._run(designs, [["a"], [], ["c"]])
        self.assertIsNone(error)
        self.assertEqual(saved, {"Post 1": "a", "Post 3": "c"})  # Post 3 ki image Post 3 hi rahi
        self.assertTrue(any("Post 2" in x for x in summary["image_fail"]))

    async def test_posts_go_in_one_message(self):
        designs = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in (1, 2, 3)]
        with work_directory() as tmp:
            work, final = tmp / "work", tmp / "final"
            work.mkdir(), final.mkdir()
            chat, ready, download = self._harness([["a", "b", "c"]])
            with patch.dict(server.CFG, {"posts_in_one_message": True}), \
                 patch.object(server, "image_file_ids", return_value=[]), \
                 patch.object(server, "wait_for_images", side_effect=ready),                  patch.object(server, "download_generated", side_effect=download),                  patch.object(server, "qa_batch", new=AsyncMock(return_value=([], []))),                  patch.object(server, "final_dir", return_value=final),                  patch.object(server, "notify"), patch.object(server, "log"):
                await server.generate_and_download(chat, "cid", "Example", designs, work, {})
            self.assertEqual(chat.send.await_count, 1)  # teeno posts ek hi message
            self.assertEqual({p.stem: p.read_text() for p in final.glob("*.png")},
                             {"Post 1": "a", "Post 2": "b", "Post 3": "c"})

    async def test_carousel_all_slides_one_message_missing_slides_continue_same_carousel(self):
        designs = []
        for k in range(1, 13):  # 12 slides: 10 ki had se zyada, phir bhi EK message
            d = server.new_design("post", f"Post 1 - Slide {k}", f"Slide text {k}")
            d.update(carousel=1, slide=k, section="Post 1")
            designs.append(d)
        made = [f"s{k}" for k in range(1, 11)]
        with patch.dict(server.CFG, {"batch_mode": True}):
            chat_sends = []
            saved, summary, error = await self._run_capture(designs, [made, ["s11", "s12"]], chat_sends)
        self.assertIsNone(error)
        self.assertEqual(len(saved), 12)
        self.assertEqual(len(chat_sends), 2)  # pehla message saari 12 slides, doosra sirf baqi 2
        self.assertIn("Carousel 1 Slide 12", chat_sends[0])
        self.assertIn("Continue the SAME carousel", chat_sends[1])
        self.assertNotIn("Carousel 1 Slide 3 (4:5):", chat_sends[1])
        self.assertFalse(summary.get("image_fail"))

    async def test_company_rules_only_for_their_company(self):
        cfg = {"model_variety_note": "PEOPLE / MODELS: new face", "model_variety_companies": ["wear wood"],
               "company_notes": {"single parent": "NEVER use pink"}}
        with patch.dict(server.CFG, cfg):
            msg = lambda co: server.build_design_message(co, [server.new_design("post", "Post 1", "Heading")])[0]
            self.assertIn("PEOPLE / MODELS", msg("Wear Wood"))
            self.assertIn("NEVER use pink", msg("the single parent"))
            self.assertNotIn("PEOPLE / MODELS", msg("HR Strategy Studio"))
            self.assertNotIn("NEVER use pink", msg("HR Strategy Studio"))

    async def test_ten_posts_go_in_sets_of_three(self):
        designs = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in range(1, 11)]
        sends = []
        with patch.dict(server.CFG, {"batch_mode": True, "max_posts_per_message": 3}):
            saved, summary, error = await self._run_capture(
                designs, [["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"], ["10"]], sends)
        self.assertIsNone(error)
        self.assertEqual(len(saved), 10)
        self.assertEqual(len(sends), 4)  # 3 + 3 + 3 + 1
        self.assertIn("Create these 3 posts", sends[0])
        self.assertIn("Post 10", sends[3])
        self.assertNotIn("Post 4 (4:5)", sends[0])

    async def test_big_file_rests_and_refreshes_between_sets(self):
        designs = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in range(1, 8)]
        sends = []
        with patch.dict(server.CFG, {"batch_mode": True, "max_posts_per_message": 3, "split_posts_from": 4,
                                     "rest_after_images": 4, "rest_between_sets_minutes": 3}):
            saved, summary, error = await self._run_capture(designs, [["1", "2", "3"], ["4", "5", "6"], ["7"]], sends)
        self.assertEqual(len(saved), 7)
        self.assertEqual(len(sends), 3)  # 3 + 3 + 1

    async def test_carousel_goes_after_posts_with_rest_and_refresh(self):
        designs = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in (1, 2)]
        for k in (1, 2):
            d = server.new_design("post", f"Post 3 - Slide {k}", f"Slide {k}")
            d.update(carousel=1, slide=k, section="Post 3")
            designs.insert(0, d)  # carousel doc mein pehle ho tab bhi posts ke baad jaye
        sends, rest = [], AsyncMock()
        with patch.dict(server.CFG, {"batch_mode": True, "rest_after_images": 4, "rest_before_carousel_minutes": 1}), \
             patch.object(server, "chatgpt_rest", new=rest):
            saved, summary, error = await self._run_capture(designs, [["p1", "p2"], ["s1", "s2"]], sends)
        self.assertEqual(len(sends), 2)
        self.assertIn("Heading 1", sends[0])     # posts pehle
        self.assertIn("carousel", sends[1].lower())  # carousel baad mein
        rest.assert_awaited_once()
        self.assertEqual(rest.await_args.args[0], 1)  # carousel se pehle 1 minute

    async def test_five_posts_still_one_message(self):
        designs = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in range(1, 6)]
        sends = []
        with patch.dict(server.CFG, {"batch_mode": True, "max_posts_per_message": 3, "split_posts_from": 6}):
            saved, summary, error = await self._run_capture(designs, [["1", "2", "3", "4", "5"]], sends)
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(saved), 5)

    async def test_instagram_album_whole_carousel_note(self):
        d = server.new_design("post", "Post 1", "")
        d["ig_shot"] = [Path("ig_s1.png"), Path("ig_s2.png"), Path("ig_s3.png")]
        designs = server.split_instagram_carousels([d])
        sends = []
        with patch.dict(server.CFG, {"batch_mode": True}):
            saved, summary, error = await self._run_capture(designs, [["s1", "s2", "s3"]], sends)
        self.assertEqual(len(sends), 1)  # poora album EK message
        self.assertIn("whole carousel content in image screenshots", sends[0])
        self.assertEqual(len(saved), 3)

    async def test_same_image_twice_is_not_counted_and_instagram_post_stays_in_batch(self):
        designs = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in (1, 2, 3)]
        ig = server.new_design("post", "Post 4", "")
        ig["ig_shot"] = [Path("ig.png")]
        designs.append(ig)
        sends = []
        with patch.dict(server.CFG, {"batch_mode": True, "split_posts_from": 10}):
            # batch (Instagram post bhi isi mein): Post 1 ki image 2 dafa (Post 2 ki jagah) -> sirf Post 2 alag message
            saved, summary, _ = await self._run_capture(designs, [["a", "a", "c", "ig"], ["b"]], sends)
        self.assertEqual(saved, {"Post 1": "a", "Post 2": "b", "Post 3": "c", "Post 4": "ig"})
        self.assertIn("Attached image 1 is a screenshot of an Instagram post", sends[0])  # Instagram post batch mein
        self.assertEqual(len(sends), 2)
        self.assertFalse(summary.get("image_fail"))

    async def _run_capture(self, designs, ids_per_call, sends):
        with work_directory() as tmp:
            work, final = tmp / "work", tmp / "final"
            work.mkdir(), final.mkdir()
            chat, ready, download = self._harness(ids_per_call)

            async def send(text, files=()):
                sends.append(text)
                return 0
            chat.send = send
            summary = {}
            with patch.object(server, "image_file_ids", return_value=[]), \
                 patch.object(server, "page_image_refs", new=AsyncMock(return_value=[])), \
                 patch.object(server, "style_refs", return_value=[]), \
                 patch.object(server, "wait_for_images", side_effect=ready), \
                 patch.object(server, "download_generated", side_effect=download), \
                 patch.object(server, "qa_batch", new=AsyncMock(return_value=([], []))), \
                 patch.object(server, "final_dir", return_value=final), \
                 patch.object(server, "run_sleep", new=AsyncMock()), \
                 patch.object(server, "notify"), patch.object(server, "log"):
                await server.generate_and_download(chat, "cid", "Example", designs, work, summary)
            return {p.stem: p.read_text() for p in final.glob("*.png")}, summary, None

    async def test_product_photo_attached_to_every_mockup(self):
        with work_directory() as tmp:
            work, final = tmp / "work", tmp / "final"
            work.mkdir(), final.mkdir()
            product = tmp / "product.jpg"
            product.write_bytes(b"x")
            design = server.new_design("post", "Post 1", "Heading")
            design["product_path"] = product
            chat, ready, download = self._harness([["m1"], ["m2"]])
            with patch.dict(server.CFG, {"mockup_companies": ["example"], "mockups_in_one_message": False, "batch_mode": False}), \
                 patch.object(server, "image_file_ids", return_value=[]), \
                 patch.object(server, "wait_for_images", side_effect=ready), \
                 patch.object(server, "download_generated", side_effect=download), \
                 patch.object(server, "qa_batch", new=AsyncMock(return_value=([], []))), \
                 patch.object(server, "final_dir", return_value=final), \
                 patch.object(server, "run_sleep", new=AsyncMock()), \
                 patch.object(server, "notify"), patch.object(server, "log"):
                await server.generate_and_download(chat, "cid", "Example", [design], work, {})
            first, second = chat.send.await_args_list
            self.assertEqual(first.args[1], [product])   # Mockup 1: photo upload
            self.assertEqual(second.args[1], [product])   # har request mein apna product
            self.assertNotIn("earlier message in this chat", second.args[0])

    async def test_non_mockup_company_product_is_normal_post_with_heading(self):
        with work_directory() as tmp:
            work, final = tmp / "work", tmp / "final"
            work.mkdir(), final.mkdir()
            product = tmp / "product.jpg"
            product.write_bytes(b"x")
            design = server.new_design("post", "Post 22", "A FRAGRANCE FOR YOUR NEXT CHAPTER.")
            design["product_path"] = product  # generation itself must enforce the company policy
            chat, ready, download = self._harness([["p"]])
            with patch.object(server, "style_refs", return_value=[]), \
                 patch.object(server, "image_file_ids", return_value=[]), \
                 patch.object(server, "wait_for_images", side_effect=ready), \
                 patch.object(server, "download_generated", side_effect=download), \
                 patch.object(server, "qa_batch", new=AsyncMock(return_value=([], []))), \
                 patch.object(server, "final_dir", return_value=final), \
                 patch.object(server, "run_sleep", new=AsyncMock()), \
                 patch.object(server, "notify"), patch.object(server, "log"):
                await server.generate_and_download(chat, "cid", "Opulent Wellness", [design], work, {})
            (only,) = chat.send.await_args_list
            self.assertIn("A FRAGRANCE FOR YOUR NEXT CHAPTER.", only.args[0])   # heading jati hai
            self.assertNotIn("Mockup", only.args[0])
            self.assertEqual(only.args[1], [product])

    async def test_style_refs_uploaded_once(self):
        with work_directory() as tmp:
            work, final = tmp / "work", tmp / "final"
            work.mkdir(), final.mkdir()
            ref = tmp / "old_design.png"
            ref.write_bytes(b"x")
            designs = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in (1, 2)]
            chat, ready, download = self._harness([["a"], ["b"]])
            chat.wait_done = AsyncMock()
            with patch.object(server, "style_refs", return_value=[ref]), \
                 patch.object(server, "image_file_ids", return_value=[]), \
                 patch.object(server, "wait_for_images", side_effect=ready), \
                 patch.object(server, "download_generated", side_effect=download), \
                 patch.object(server, "qa_batch", new=AsyncMock(return_value=([], []))), \
                 patch.object(server, "final_dir", return_value=final), \
                 patch.object(server, "run_sleep", new=AsyncMock()), \
                 patch.object(server, "remember_chat"), \
                 patch.object(server, "notify"), patch.object(server, "log"):
                await server.generate_and_download(chat, "cid", "Example", designs, work, {})
            refs_msg, post1, post2 = chat.send.await_args_list
            self.assertEqual(refs_msg.args[1], [ref])            # references ALAG message mein
            self.assertIn("Do NOT generate any image now", refs_msg.args[0])
            for post in (post1, post2):                          # posts mein references attach nahi
                self.assertEqual(post.args[1], [])
                self.assertIn("style references uploaded earlier", post.args[0])

    async def test_style_refs_not_reuploaded_to_same_chat(self):
        with work_directory() as tmp:
            work, final = tmp / "work", tmp / "final"
            work.mkdir(), final.mkdir()
            ref = tmp / "384.jpg"
            ref.write_bytes(b"x")
            chat, ready, download = self._harness([["a"]])
            memory = {"waseem": {"example": {"id": "cid", "title": "Example", "refs": ["384.jpg"]}}}
            with patch.dict(server.CHAT_MEMORY, memory, clear=True), \
                 patch.object(server, "style_refs", return_value=[ref]), \
                 patch.object(server, "image_file_ids", return_value=[]), \
                 patch.object(server, "wait_for_images", side_effect=ready), \
                 patch.object(server, "download_generated", side_effect=download), \
                 patch.object(server, "qa_batch", new=AsyncMock(return_value=([], []))), \
                 patch.object(server, "final_dir", return_value=final), \
                 patch.object(server, "run_sleep", new=AsyncMock()), \
                 patch.object(server, "notify"), patch.object(server, "log"):
                await server.generate_and_download(chat, "cid", "Example",
                                                   [server.new_design("post", "Post 1", "H")], work, {})
            (only,) = chat.send.await_args_list
            self.assertEqual(only.args[1], [])  # pichle run mein ja chuki -> dobara upload nahi
            self.assertIn("style references uploaded earlier", only.args[0])

    async def test_resume_skips_images_already_made(self):
        with work_directory() as tmp:
            work, final = tmp / "work", tmp / "final"
            work.mkdir(), final.mkdir()
            old1, old2 = tmp / "old1.png", tmp / "old2.png"
            old1.write_text("p1"), old2.write_text("p2")
            self.addCleanup(setattr, server, "PROGRESS_FILE", server.PROGRESS_FILE)
            server.PROGRESS_FILE = tmp / "progress.json"
            server.save_progress("doc9", "Post 1 (4:5)", old1)
            server.save_progress("doc9", "Post 2 (4:5)", old2)
            designs = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in (1, 2, 3)]
            chat, ready, download = self._harness([["p3"]])
            with patch.object(server, "style_refs", return_value=[]), \
                 patch.object(server, "image_file_ids", return_value=[]), \
                 patch.object(server, "wait_for_images", side_effect=ready), \
                 patch.object(server, "download_generated", side_effect=download), \
                 patch.object(server, "qa_batch", new=AsyncMock(return_value=([], []))), \
                 patch.object(server, "final_dir", return_value=final), \
                 patch.object(server, "run_sleep", new=AsyncMock()), \
                 patch.object(server, "notify"), patch.object(server, "log"):
                await server.generate_and_download(chat, "cid", "Example", designs, work, {"doc_id": "doc9"})
            (only,) = chat.send.await_args_list                    # sirf Post 3 bheji gayi
            self.assertIn("Post 3 (4:5)", only.args[0])
            self.assertEqual({p.stem: p.read_text() for p in final.glob("*.png")},
                             {"Post 1": "p1", "Post 2": "p2", "Post 3": "p3"})
            self.assertIn("Post 3 (4:5)", server.load_progress()["doc9"])  # nayi bhi yaad

    async def test_product_two_mockups_one_message_posts_separate(self):
        with work_directory() as tmp:
            work, final = tmp / "work", tmp / "final"
            work.mkdir(), final.mkdir()
            product = tmp / "product.jpg"
            product.write_bytes(b"x")
            prod = server.new_design("post", "Post 1", "Heading 1")
            prod["product_path"] = product
            posts = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in (2, 3)]
            chat, ready, download = self._harness([["m1", "m2"], ["p2", "p3"]])
            with patch.dict(server.CFG, {"mockup_companies": ["wear wood"], "batch_mode": True}), \
                 patch.object(server, "style_refs", return_value=[]), \
                 patch.object(server, "image_file_ids", return_value=[]), \
                 patch.object(server, "page_image_refs", new=AsyncMock(return_value=[])), \
                 patch.object(server, "wait_for_images", side_effect=ready), \
                 patch.object(server, "download_generated", side_effect=download), \
                 patch.object(server, "qa_batch", new=AsyncMock(return_value=([], []))), \
                 patch.object(server, "final_dir", return_value=final), \
                 patch.object(server, "run_sleep", new=AsyncMock()), \
                 patch.object(server, "notify"), patch.object(server, "log"):
                await server.generate_and_download(chat, "cid", "Wear Wood", [prod] + posts, work, {})
            mock_msg, posts_msg = chat.send.await_args_list           # mockups EK message, posts EK message
            self.assertIn("Mockup 1", mock_msg.args[0]); self.assertIn("Mockup 2", mock_msg.args[0])
            self.assertIn("Create 2 mockups for these 1 products", mock_msg.args[0])
            self.assertEqual(mock_msg.args[1], [product])
            self.assertIn("Post 1 (4:5)", posts_msg.args[0]); self.assertIn("Post 2 (4:5)", posts_msg.args[0])
            self.assertIn("Create these 2 posts in my branding", posts_msg.args[0])
            self.assertEqual({p.stem: p.read_text() for p in final.glob("*.png")},
                             {"Mockup 1": "m1", "Mockup 2": "m2", "Post 1": "p2", "Post 2": "p3"})

    async def test_default_each_post_separate_message(self):
        designs = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in (1, 2, 3)]
        saved, summary, error = await self._run(designs, [["a"], ["b"], ["c"]])
        self.assertIsNone(error)
        self.assertEqual(saved, {"Post 1": "a", "Post 2": "b", "Post 3": "c"})

    async def test_two_variants_takes_latest(self):
        saved, summary, error = await self._run([server.new_design("post", "Post 1", "H")], [["old", "new"]])
        self.assertIsNone(error)
        self.assertEqual(saved, {"Post 1": "new"})

    async def test_limit_mid_file_keeps_finished_images(self):
        designs = [server.new_design("post", f"Post {n}", f"Heading {n}") for n in (1, 2)]
        saved, summary, error = await self._run(designs, [["a"], server.ChatLimit("paused")])
        self.assertIsNone(error)
        self.assertEqual(saved, {"Post 1": "a"})
        self.assertTrue(any("Post 2" in x for x in summary["image_fail"]))

    async def test_unverified_qa_still_saves_images(self):
        saved, summary, error = await self._run([server.new_design("post", "Post 1", "H")], [["a"]],
                                                qa=([], ["QA 60s mein jawab nahi aaya"]))
        self.assertIsNone(error)
        self.assertEqual(saved, {"Post 1": "a"})
        self.assertEqual(summary["qa_unverified"], ["QA 60s mein jawab nahi aaya"])
        self.assertNotIn("qa_fail", summary)


class StuckRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def _wait(self, image_after_refresh):
        clock = {"t": 1000.0}

        async def fake_sleep(sec):
            clock["t"] += sec
        chat = server.Chat("waseem")
        chat.generating = AsyncMock(return_value=True)  # hamesha "bana raha hai" (atka hua)
        chat.goto = AsyncMock()
        state = {"refreshed": 0}

        async def goto(url):
            state["refreshed"] += 1
        chat.goto.side_effect = goto

        def ids(conv):
            return ["img-1"] if image_after_refresh and state["refreshed"] else []
        chat.conversation = AsyncMock(return_value={})
        with patch.object(server.time, "time", side_effect=lambda: clock["t"]), \
             patch.object(server.asyncio, "sleep", side_effect=fake_sleep), \
             patch.object(server, "image_file_ids", side_effect=ids), \
             patch.object(server, "image_ids_after", side_effect=lambda conv, since: ids(conv)), \
             patch.object(server, "notify"), patch.object(server, "log"):
            result = await server.wait_for_images(chat, "cid", set(), 1, sent_at=clock["t"])
        return result, state["refreshed"], clock["t"] - 1000.0

    async def test_stuck_refreshes_after_10_minutes_then_gets_image(self):
        result, refreshed, elapsed = await self._wait(image_after_refresh=True)
        self.assertEqual(result, ["img-1"])
        self.assertEqual(refreshed, 1)
        self.assertGreaterEqual(elapsed, 600)

    async def test_page_misses_image_api_check_finds_it(self):
        clock = {"t": 1000.0}

        async def fake_sleep(sec):
            clock["t"] += sec
        chat = server.Chat("waseem")
        chat.generating = AsyncMock(return_value=False)  # ChatGPT kaam khatam kar chuka
        conv = {"mapping": {
            "old": {"message": {"author": {"role": "tool"}, "create_time": 500.0,
                                "content": {"parts": [{"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_old"}]}}},
            "new": {"message": {"author": {"role": "tool"}, "create_time": 1050.0,
                                "content": {"parts": [{"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_new"}]}}}}}
        chat.conversation = AsyncMock(return_value=conv)
        with patch.object(server.time, "time", side_effect=lambda: clock["t"]), \
             patch.object(server.asyncio, "sleep", side_effect=fake_sleep), \
             patch.object(server.HUB, "exec", new=AsyncMock(return_value={"imgs": []})), \
             patch.object(server, "notify"), patch.object(server, "log"):
            result = await server.wait_for_images(chat, "cid", set(), 1, sent_at=1000.0)
        self.assertEqual(result, ["file_new"])  # page par nahi dikhi, API se mili; purani nahi li

    async def test_stuck_gives_up_after_two_refreshes(self):
        result, refreshed, elapsed = await self._wait(image_after_refresh=False)
        self.assertEqual(result, [])
        self.assertEqual(refreshed, 2)
        self.assertLess(elapsed, 40 * 60)  # 3 x 10 min ke qareeb, hamesha nahi atka


class InternetDropTests(unittest.IsolatedAsyncioTestCase):
    async def test_net_drop_retries_same_file_not_error(self):
        tab = {"tabId": 1, "url": "https://docs.google.com/document/d/docA/edit", "title": "Alpha - Google Docs"}
        net = {"up": True}
        calls = []

        async def call(role, command, **kwargs):
            return [tab] if command == "list_doc_tabs" else {}

        async def process(tab_id, *args, **kwargs):
            calls.append(net["up"])
            if len(calls) == 1:
                net["up"] = False  # beech mein net gaya
                raise RuntimeError("page load timeout")

        async def internet_ok():
            ok = net["up"]
            net["up"] = True  # agli check par wapas aa gaya
            return ok

        saved = {}
        with patch.dict(server.HUB.clients, {role: object() for role in server.ROLES}, clear=True), \
             patch.object(server.HUB, "call", side_effect=call), \
             patch.object(server.HUB, "send", new=AsyncMock()), \
             patch.object(server, "prepare_file", new=AsyncMock(return_value=[])), \
             patch.object(server, "process_doc", side_effect=process), \
             patch.object(server, "internet_ok", side_effect=internet_ok), \
             patch.object(server, "load_state", return_value={}), \
             patch.object(server, "save_state", side_effect=lambda s: saved.update(s)), \
             patch.object(server, "write_report"), patch.object(server, "notify"), patch.object(server, "log"), \
             patch.dict(server.CFG, {"watch_idle_minutes": 0}), \
             patch.object(server.asyncio, "sleep", new=AsyncMock()):
            await server.run_all()
        self.assertEqual(len(calls), 2)                       # wahi file dobara chali
        self.assertEqual(saved["docA"]["status"], "done")      # error mark nahi hui


class BrandingFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_chat_no_website_uses_approved_designs(self):
        waseem = server.Chat("waseem")
        waseem.find_chat = AsyncMock(return_value=(None, None))
        refs = [server.Path("a.jpg"), server.Path("b.jpg")]
        with patch.object(server, "get_instructions_from_wajid", side_effect=server.BrandChatNotFound("x")), \
             patch.object(server, "collect_website_branding",
                          side_effect=server.WebsiteBrandingUnavailable("website nahi")), \
             patch.object(server, "style_refs", return_value=refs), \
             patch.object(server, "create_waseem_chat", new=AsyncMock(return_value="newcid")) as create, \
             patch.object(server, "remember_chat") as remember, patch.object(server, "log"):
            cid, brand = await server.resolve_company_chat(waseem, "John Lee Entrepreneur", ["John Lee"], "doc", "out")
        self.assertEqual(cid, "newcid")
        self.assertEqual(create.await_args.kwargs["files"], refs)   # approved designs se branding
        remember.assert_called_once()

    async def test_no_chat_no_website_no_designs_is_manual(self):
        waseem = server.Chat("waseem")
        waseem.find_chat = AsyncMock(return_value=(None, None))
        with patch.object(server, "get_instructions_from_wajid", side_effect=server.BrandChatNotFound("x")), \
             patch.object(server, "collect_website_branding",
                          side_effect=server.WebsiteBrandingUnavailable("website nahi")), \
             patch.object(server, "style_refs", return_value=[]), patch.object(server, "log"):
            with self.assertRaises(server.WebsiteBrandingUnavailable):
                await server.resolve_company_chat(waseem, "Nobody Co", ["Nobody"], "doc", "out")

    def test_image_sites_are_not_company_websites(self):
        import website_branding as wb
        doc = "https://www.magnific.com/free-photo/gym_1.htm https://johnlee.com/about"
        self.assertEqual([u for u in wb.website_candidates(doc, "John Lee") if "magnific" in u], [])


class NameVariantTests(unittest.IsolatedAsyncioTestCase):
    async def test_booth_scout_finds_boothscout_chat(self):
        chat = server.Chat("wajid")
        chat.tab = 1
        chat.goto = AsyncMock()
        queries = []

        async def api(path, *args, **kwargs):
            q = server.re.search(r"query=([^&]*)", path).group(1)
            queries.append(q)
            items = [{"conversation_id": "bs1", "title": "Boothscout"}] if q in ("Boothscout", "Booth") else []
            return {"items": items}
        chat.api = AsyncMock(side_effect=api)
        with patch.dict(server.CHAT_MEMORY, {}, clear=True), \
             patch.object(server.HUB, "exec", new=AsyncMock(return_value=[])), \
             patch.object(server, "remember_chat"), patch.object(server, "log"), \
             patch.object(server, "run_sleep", new=AsyncMock()):
            found = await chat.find_chat(server.search_terms("Booth scout 12-10-26.docx - Google Docs"),
                                         "Booth scout", "doc")
        self.assertEqual(found, ("bs1", "Boothscout"))
        self.assertIn("Boothscout", queries)

    async def test_any_branding_error_falls_back_to_approved_designs(self):
        waseem = server.Chat("waseem")
        waseem.find_chat = AsyncMock(return_value=(None, None))
        refs = [server.Path("x.jpg")]
        with patch.object(server, "get_instructions_from_wajid", new=AsyncMock(return_value="I" * 400)), \
             patch.object(server, "create_waseem_chat",
                          new=AsyncMock(side_effect=[RuntimeError("Nayi Waseem chat ka id nahi mila"), "cid2"])), \
             patch.object(server, "style_refs", return_value=refs), \
             patch.object(server, "remember_chat"), patch.object(server, "log"):
            cid, _ = await server.resolve_company_chat(waseem, "Single Parent", ["Single Parent"], "doc", "out")
        self.assertEqual(cid, "cid2")


class ImagePromptTests(unittest.TestCase):
    DOC = ("Company post: Create a clean, modern infographic with six clearly separated sections and simple icons. "
           "Use subtle vendor-market imagery.\n"
           "Heading: 6 THINGS TO TRACK\n01 — NEW CUSTOMERS\n"
           "Caption: A market's value isn't always captured by sales.\n"
           "Company post: A realistic, premium editorial photograph of a market vendor at a desk with a laptop, "
           "warm evening light, shallow depth of field.\n"
           "Caption: Not every market teaches you the same lesson. Take note.\n"
           "Company post:\n"
           "Create a clean SaaS-style layout showing a laptop with the event directory open and a notebook beside it.\n"
           "Heading: TURN MARKET DAYS INTO USEFUL DATA.\n"
           "Caption: Your work doesn't end when the market closes.\n")

    def test_writer_image_prompt_is_direction_not_text(self):
        designs, _, _ = server.parse_doc(self.DOC)
        self.assertEqual([d["content"] for d in designs],
                         ["6 THINGS TO TRACK\n01 — NEW CUSTOMERS", "Not every market teaches you the same lesson.",
                          "TURN MARKET DAYS INTO USEFUL DATA."])
        self.assertTrue(all(d.get("image_prompt") for d in designs))
        self.assertNotIn("infographic", designs[0]["content"])   # prompt image par nahi likha jata
        msg, _, _ = server.build_design_message("Booth scout", designs[:1])
        self.assertIn("IMAGE DIRECTION FROM THE WRITER", msg)
        self.assertIn("simple icons", msg)

    def test_short_heading_line_is_not_a_prompt(self):
        designs, _, _ = server.parse_doc("Post 1\nHeading: Create your dream home today\nCaption: Call us.\n")
        self.assertEqual(designs[0]["content"], "Create your dream home today")
        self.assertFalse(designs[0].get("image_prompt"))


class EnvatoFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_envato_is_always_screenshot_in_azhar_profile(self):
        shot = server.Path("img_screenshot.png")
        with patch.object(server, "_download_envato_once") as dl, \
             patch.object(server, "envato_screenshot", new=AsyncMock(return_value=shot)) as ss, \
             patch.dict(server.HUB.clients, {"instagram": object()}), \
             patch.object(server, "log"), patch.object(server, "run_sleep", new=AsyncMock()):
            path = await server.download_envato("https://elements.envato.com/x", "img")
        dl.assert_not_called()  # download ki koshish hi nahi
        self.assertEqual(ss.await_args.kwargs.get("role"), "instagram")
        self.assertEqual(path, shot)

    async def test_two_failed_freepik_downloads_use_preview_screenshot(self):
        shot = server.Path("img_screenshot.png")
        with patch.object(server, "download_freepik", side_effect=RuntimeError("download nahi hua")) as dl, \
             patch.object(server, "envato_screenshot", new=AsyncMock(return_value=shot)) as ss, \
             patch.object(server, "log"), patch.object(server, "run_sleep", new=AsyncMock()):
            path = await server.download_image("https://www.freepik.com/free-photo/x_123.htm", "img")
        self.assertEqual(dl.call_count, 2)
        ss.assert_awaited_once()
        self.assertEqual(ss.await_args.kwargs.get("role"), "designer")
        self.assertEqual(path, shot)

    def test_screenshot_photo_gets_same_image_note(self):
        d = server.new_design("post", "Post 1", "Heading")
        d["image_path"] = server.Path("img_screenshot.png")
        msg, files, _ = server.build_design_message("Example", [d])
        self.assertIn("Use this SAME image", msg)
        self.assertEqual(files, [d["image_path"]])


class FinalFolderTests(unittest.TestCase):
    def test_each_file_gets_its_own_number_folder(self):
        with work_directory() as tmp, patch.dict(server.CFG, {"final_dir": str(tmp)}):
            seven = server.final_dir("Wear Wood", "Wear Wood 7")
            eight = server.final_dir("Wear Wood", "Wear Wood 8.docx")
            self.assertEqual((seven.parent.name, seven.name), ("Wear Wood", "7"))
            self.assertEqual((eight.parent.name, eight.name), ("Wear Wood", "8"))
            self.assertEqual(server.final_dir("Beyond the Budget", "Ficus Financial 6").name, "Ficus Financial 6")
            self.assertEqual(server.final_dir("Molen tax", "Molen tax").name, "Molen tax")  # number nahi


class StorySkipTests(unittest.TestCase):
    def test_only_story_missing_file_is_done(self):
        summary = {"image_fail": ["Post 23 (story): Wealth grows -> https://elements.envato.com/x",
                                  "Story 1 (9:16): ChatGPT ne image nahi banayi"]}
        self.assertTrue(server.move_story_issues(summary))
        self.assertFalse(summary["image_fail"])  # file incomplete nahi
        self.assertEqual(len(summary["story_skipped"]), 2)

    def test_missing_post_still_incomplete(self):
        summary = {"image_fail": ["Post 2 (4:5): ChatGPT ne image nahi banayi", "Story 1 (9:16): limit"],
                   "dropped": ["Post 3"]}
        server.move_story_issues(summary)
        self.assertEqual(summary["image_fail"], ["Post 2 (4:5): ChatGPT ne image nahi banayi"])
        self.assertEqual(summary["dropped"], ["Post 3"])


class SummaryTests(unittest.TestCase):
    def test_summary_names_doc_post_heading_and_action(self):
        d = server.new_design("post", "Post 35", "Liquid BBL Aftercare: The First 2 Weeks")
        entry = f"{server.who({'d': d, 'name': 'Post 2 (4:5)', 'type': 'post'})}: ChatGPT limit — nahi bani"
        report = [{"file": "Belle 9", "reels": [], "done": [], "error": "File incomplete", "designs": 3, "made": 2,
                   "image_fail": [entry]}]
        with work_directory() as tmp, patch.object(server, "log"):
            counts = server.write_report(report, tmp)
            text = next(tmp.glob("SUMMARY_*.txt")).read_text(encoding="utf-8")
        self.assertIn("Post 35 [Post 2] «Liquid BBL Aftercare The First 2 Weeks»", text)
        self.assertIn("START dobara dabao", text)
        self.assertEqual((counts["bad"], counts["posts"]), (1, 1))

    def test_video_link_line_marks_post_as_reel_not_dropped(self):
        doc = "Post 3\nVideo link: https://www.pinterest.com/pin/1/\nHeading:\nMore Partners?\nCaption:\nText here"
        designs, reels, _ = server.parse_doc(doc)
        self.assertEqual(designs, [])
        self.assertTrue(reels and reels[0].startswith("Post 3 (video)"))


class DashboardSkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_skip_current_file_raises_once_at_safe_point(self):
        hub = server.Hub()
        hub.run_task = asyncio.current_task()
        hub.current = {"file": "Belle Clinic London UK 9"}
        await hub.checkpoint()  # skip nahi -> kuch nahi
        hub.skip.add(server.norm("Belle Clinic London UK 9"))
        with self.assertRaises(server.FileSkipped):
            await hub.checkpoint()
        await hub.checkpoint()  # ek hi dafa (finally blocks ki cleanup chalti rahe)

    def test_skip_action_and_report(self):
        with patch.object(server, "log"):
            self.assertTrue(server.dashboard_skip("skip", "Wear Wood 8")["ok"])
            self.assertIn(server.norm("Wear Wood 8"), server.HUB.skip)
            server.dashboard_skip("unskip", "Wear Wood 8")
            self.assertNotIn(server.norm("Wear Wood 8"), server.HUB.skip)
        report = [{"file": "Wear Wood 8", "reels": [], "done": [], "designs": 0, "error": None, "skipped": True}]
        with work_directory() as tmp, patch.object(server, "log"):
            counts = server.write_report(report, tmp)
            text = next(tmp.glob("SUMMARY_*.txt")).read_text(encoding="utf-8")
        self.assertIn("SKIP  Wear Wood 8", text)
        self.assertEqual(counts["bad"], 0)


class FileReportTests(unittest.TestCase):
    def test_empty_error_is_not_done(self):
        with work_directory() as tmp:
            real = server.write_file_report.__wrapped__ if hasattr(server.write_file_report, "__wrapped__") else None
            import importlib, json as _j
            src = open(server.BASE / "server.py", encoding="utf-8").read()
            self.assertIn('status = "done" if err is None', src)  # "" (khaali error) done nahi ginti
            self.assertIn("0 images bani; DONE mark nahi ki", src)


class InstagramCarouselTests(unittest.TestCase):
    def test_multi_slide_instagram_becomes_its_own_carousel(self):
        post = server.new_design("post", "Post 1", "Heading 1")
        single = server.new_design("post", "Post 2", "")
        single["ig_shot"] = [Path("a.png")]
        multi = server.new_design("post", "Post 3", "")
        multi["ig_shot"] = [Path("s1.png"), Path("s2.png"), Path("s3.png")]
        out = server.split_instagram_carousels([post, single, multi])
        self.assertEqual([d.get("carousel") for d in out], [None, None, 1, 1, 1])
        self.assertEqual([d["ig_shot"] for d in out[2:]], [[Path("s1.png")], [Path("s2.png")], [Path("s3.png")]])
        msg, files, outs = server.build_design_message("Example", out)
        self.assertEqual([o["name"] for o in outs][2:], ["Carousel 1 Slide 1 (4:5)", "Carousel 1 Slide 2 (4:5)",
                                                         "Carousel 1 Slide 3 (4:5)"])


class CleanupTests(unittest.TestCase):
    def test_only_date_folders_older_than_a_week_are_deleted(self):
        from datetime import date, timedelta
        original = server.OUT_ROOT
        self.addCleanup(setattr, server, "OUT_ROOT", original)
        with work_directory() as tmp:
            server.OUT_ROOT = tmp
            today = date.today()
            old = (today - timedelta(days=8)).isoformat()
            week = (today - timedelta(days=7)).isoformat()   # theek 7 din purana -> abhi rahega
            for name in (old, week, today.isoformat(), "logs", "download-tests"):
                (tmp / name).mkdir()
            (tmp / "state.json").write_text("{}")
            (tmp / "logs" / f"{old}.log").write_text("x")
            (tmp / "logs" / f"{today.isoformat()}.log").write_text("x")
            with patch.object(server, "log"):
                removed = server.cleanup_old_outputs(7)
            self.assertEqual(removed, [old])
            self.assertTrue((tmp / week).exists() and (tmp / today.isoformat()).exists())
            self.assertTrue((tmp / "state.json").exists() and (tmp / "download-tests").exists())
            self.assertFalse((tmp / "logs" / f"{old}.log").exists())
            self.assertTrue((tmp / "logs" / f"{today.isoformat()}.log").exists())


class AutoResumeTests(unittest.IsolatedAsyncioTestCase):
    async def _auto(self, marker, open_tabs):
        original = server.RUN_MARKER
        self.addCleanup(setattr, server, "RUN_MARKER", original)
        with work_directory() as tmp:
            server.RUN_MARKER = tmp / "run_active.json"
            server.RUN_MARKER.write_text(__import__("json").dumps(marker), encoding="utf-8")
            opened = []

            async def call(role, command, **kwargs):
                if command == "list_doc_tabs":
                    return open_tabs
                if command == "open_tab":
                    opened.append(kwargs["url"])
                return 1
            with patch.dict(server.HUB.clients, {r: object() for r in server.ROLES}, clear=True),                  patch.object(server.HUB, "call", side_effect=call),                  patch.object(server, "start_run") as start, patch.object(server, "notify"),                  patch.object(server, "log"), patch.object(server.asyncio, "sleep", new=AsyncMock()):
                server.HUB.running = False
                await server.auto_resume()
            return start, opened

    async def test_unfinished_run_resumes_and_reopens_closed_file(self):
        doc = {"url": "https://docs.google.com/document/d/docA/edit", "title": "Alpha"}
        start, opened = await self._auto({"active": True, "paused": False, "docs": [doc]}, open_tabs=[])
        start.assert_called_once()
        self.assertEqual(opened, [doc["url"]])  # bijli ke baad band tab khud kholi

    async def test_paused_run_does_not_autostart(self):
        start, _ = await self._auto({"active": True, "paused": True, "docs": []}, open_tabs=[])
        start.assert_not_called()


class LiveWatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_opened_after_start_is_processed(self):
        first = {"tabId": 1, "url": "https://docs.google.com/document/d/doc1/edit", "title": "Alpha - Google Docs"}
        later = {"tabId": 2, "url": "https://docs.google.com/document/d/doc2/edit", "title": "Beta - Google Docs"}
        clock = {"t": 0.0, "lists": 0}
        events = []

        async def call(role, command, **kwargs):
            if command == "list_doc_tabs":
                clock["lists"] += 1
                return [first] if clock["lists"] < 4 else [first, later]  # Beta thodi der baad khuli
            return {}

        async def fake_sleep(sec):
            clock["t"] += sec

        async def process(tab_id, *args, **kwargs):
            events.append(tab_id)

        with patch.dict(server.HUB.clients, {role: object() for role in server.ROLES}, clear=True),              patch.object(server.HUB, "call", side_effect=call),              patch.object(server.HUB, "send", new=AsyncMock()),              patch.object(server, "prepare_file", new=AsyncMock(return_value=[])),              patch.object(server, "process_doc", side_effect=process),              patch.object(server, "load_state", return_value={}),              patch.object(server, "save_state"), patch.object(server, "write_report"),              patch.object(server, "notify"), patch.object(server, "log"),              patch.dict(server.CFG, {"watch_idle_minutes": 1, "watch_poll_seconds": 8}),              patch.object(server.time, "time", side_effect=lambda: clock["t"]),              patch.object(server.asyncio, "sleep", side_effect=fake_sleep):
            await server.run_all()
        self.assertEqual(events, [1, 2])           # baad mein kholi file bhi bani
        self.assertGreaterEqual(clock["t"], 60)   # phir 1 min intezar kar ke khatam


class RateLimitApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_429_is_retried_not_failed(self):
        responses = iter([{"status": 429, "text": "{}"}, {"status": 429, "text": "{}"},
                          {"status": 200, "text": '{"items": []}'}])
        chat = server.Chat("waseem")
        chat.tab = 1
        with patch.object(server.HUB, "exec", new=AsyncMock(side_effect=lambda *a, **k: next(responses))),              patch.object(server.asyncio, "sleep", new=AsyncMock()) as sleep, patch.object(server, "log"):
            data = await chat.api("/backend-api/conversations?offset=0&limit=100&order=updated")
        self.assertEqual(data, {"items": []})
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [10, 30])

    async def test_chat_list_fetched_once_per_run(self):
        server.CHAT_LIST_CACHE.clear()
        chat = server.Chat("waseem")
        chat.api = AsyncMock(return_value={"items": [{"id": "a", "title": "Belle Clinic"}]})
        await chat.all_chats()
        await chat.all_chats()
        self.assertEqual(chat.api.await_count, 1)
        server.CHAT_LIST_CACHE.clear()


class FindChatTests(unittest.IsolatedAsyncioTestCase):
    def _chat(self, sidebar):
        chat = server.Chat("waseem")
        chat.tab = 1
        chat.goto = AsyncMock()
        chat.verify = AsyncMock(return_value=(True, "ok"))
        chat.api = AsyncMock(side_effect=AssertionError("API nahi chalni chahiye thi"))
        chat.all_chats = AsyncMock(side_effect=RuntimeError("429"))
        exec_mock = AsyncMock(return_value=sidebar)
        return chat, exec_mock

    async def test_memory_hit_needs_no_list(self):
        chat, ex = self._chat([])
        with patch.dict(server.CHAT_MEMORY, {"waseem": {"belle clinic": {"id": "m1", "title": "Belle Clinic"}}}, clear=True),              patch.object(server.HUB, "exec", new=ex), patch.object(server, "log"),              patch.object(server, "remember_chat"):
            found = await chat.find_chat(["Belle Clinic 8", "Belle Clinic"], "Belle Clinic", "doc")
        self.assertEqual(found, ("m1", "Belle Clinic"))
        chat.all_chats.assert_not_awaited()
        ex.assert_not_awaited()

    async def test_sidebar_hit_needs_no_api(self):
        chat, ex = self._chat([{"id": "s1", "title": "E Liquid"}])
        with patch.dict(server.CHAT_MEMORY, {}, clear=True), patch.object(server.HUB, "exec", new=ex),              patch.object(server, "log"), patch.object(server, "remember_chat"),              patch.object(server.asyncio, "sleep", new=AsyncMock()):
            found = await chat.find_chat(["Eliquid 3 & 4", "Eliquid"], "Eliquid", "doc")
        self.assertEqual(found, ("s1", "E Liquid"))
        chat.api.assert_not_awaited()
        chat.all_chats.assert_not_awaited()

    async def test_blocked_list_never_means_new_chat(self):
        chat, ex = self._chat([])
        chat.api = AsyncMock(side_effect=RuntimeError("429"))
        with patch.dict(server.CHAT_MEMORY, {}, clear=True), patch.object(server.HUB, "exec", new=ex),              patch.object(server, "log"), patch.object(server.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError) as ctx:
                await chat.find_chat(["Laima academy 4", "Laima academy"], "Laima academy", "doc")
        self.assertIn("nayi chat nahi banayi", str(ctx.exception))


class PageImageTests(unittest.TestCase):
    def test_generated_vs_upload_vs_icon(self):
        gen = {"src": "https://chatgpt.com/backend-api/estuary/content?id=file_abc&sig=1", "alt": "Generated image", "w": 1024}
        upload = dict(gen, alt="Uploaded image")
        user = dict(gen, alt="", role="user")
        icon = {"src": "https://chatgpt.com/backend-api/estuary/content?id=file_x", "alt": "", "w": 40}
        avatar = {"src": "https://lh3.googleusercontent.com/a/photo", "alt": "Profile", "w": 96}
        self.assertTrue(server.is_generated_img(gen))
        for img in (upload, user, icon, avatar):
            self.assertFalse(server.is_generated_img(img))


class BeforeAfterTests(unittest.TestCase):
    DOC = ("POST 1\nHEADLINE:\nBEFORE / AFTER\n"
           "Before: https://drive.google.com/file/d/AAA/view?usp=drive_link\n"
           "After:https://drive.google.com/file/d/BBB/view?usp=drive_link\n"
           "CAPTION:\nThe before photo shows the kitchen.")

    def parse(self, mode):
        with patch.dict(server.CFG, {"before_after_mode": mode}):
            return server.parse_doc(self.DOC)[0]

    def test_carousel_uses_both_photos_with_small_labels(self):
        designs = self.parse("carousel")
        self.assertEqual([d["content"] for d in designs], ["BEFORE", "AFTER"])  # HEADLINE: label print nahi hota
        self.assertTrue(designs[0]["image_url"].endswith("AAA/view?usp=drive_link"))
        self.assertTrue(designs[1]["image_url"].endswith("BBB/view?usp=drive_link"))
        self.assertEqual({d["carousel"] for d in designs}, {1})

    def test_comparison_is_one_design_with_both_photos(self):
        [d] = self.parse("comparison")
        self.assertEqual(d["before_after"], "pair")
        self.assertIn("AAA", d["image_url"])
        self.assertIn("BBB", d["after_url"])
        d["image_path"], d["after_path"] = "before.jpg", "after.jpg"
        msg, files, _ = server.build_design_message("AIM", [d])
        self.assertEqual(files, ["before.jpg", "after.jpg"])
        self.assertNotIn("EXACT HEADING TO PRINT", msg)


if __name__ == "__main__":
    unittest.main()
