import unittest
from unittest.mock import AsyncMock, patch

import server
import test_workflow
from test_workflow import work_directory


class ProductIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_attachment_refreshes_and_retries_same_product_once(self):
        with work_directory() as root:
            final = root / "final"
            final.mkdir()
            product = root / "product.jpg"
            product.write_bytes(b"fixture")
            design = server.new_design("post", "Post 1", "Exact product heading")
            design["product_path"] = product
            chat, ready, download = test_workflow.ResilienceTests()._harness([["image"]])
            chat.send.side_effect = [server.AttachFailed("upload failed"), 1000]
            with patch.object(server, "style_refs", return_value=[]), \
                 patch.object(server, "page_image_refs", new=AsyncMock(return_value=[])), \
                 patch.object(server, "wait_for_images", side_effect=ready), \
                 patch.object(server, "download_generated", side_effect=download), \
                 patch.object(server, "qa_batch", new=AsyncMock(return_value=([], []))), \
                 patch.object(server, "final_dir", return_value=final), patch.object(server, "log"):
                await server.generate_and_download(chat, "cid", "Opulent Wellness", [design], root, {})
            chat.goto.assert_awaited_once_with(f"{server.CHATGPT}/c/cid")
            self.assertEqual(chat.send.await_args_list[0], chat.send.await_args_list[1])
            self.assertEqual(chat.send.await_args_list[1].args[1], [product])

    async def test_group_mode_never_batches_products_and_repeated_product_is_reattached(self):
        with work_directory() as root:
            final = root / "final"
            final.mkdir()
            products = [root / f"product{n}.jpg" for n in (1, 2)]
            for p in products:
                p.write_bytes(b"fixture")
            designs = []
            for n, product in enumerate([products[0], products[1], products[0]], 1):
                design = server.new_design("post", f"Post {n}", f"Heading {n}")
                design["product_path"] = product
                designs.append(design)
            chat, ready, download = test_workflow.ResilienceTests()._harness([["a"], ["b"], ["c"]])
            with patch.dict(server.CFG, {"posts_in_one_message": True}), \
                 patch.object(server, "style_refs", return_value=[]), \
                 patch.object(server, "page_image_refs", new=AsyncMock(return_value=[])), \
                 patch.object(server, "wait_for_images", side_effect=ready), \
                 patch.object(server, "download_generated", side_effect=download), \
                 patch.object(server, "qa_batch", new=AsyncMock(return_value=([], []))), \
                 patch.object(server, "final_dir", return_value=final), patch.object(server, "log"):
                await server.generate_and_download(chat, "cid", "Opulent Wellness", designs, root, {})
            self.assertEqual([c.args[1] for c in chat.send.await_args_list],
                             [[products[0]], [products[1]], [products[0]]])
            for n, call in enumerate(chat.send.await_args_list, 1):
                self.assertIn(f"Heading {n}", call.args[0])
                self.assertNotIn("Mockup", call.args[0])

    async def test_unconfirmed_or_duplicate_attachments_never_submit(self):
        with work_directory() as root:
            product = root / "product.jpg"
            product.write_bytes(b"fixture")
            for result in ({"typed": 10}, {"typed": 10, "attached": 0},
                           {"typed": 10, "attached": 2}, RuntimeError("stale attachment")):
                with self.subTest(result=result):
                    ex = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
                    with patch.object(server.HUB, "exec", new=ex):
                        with self.assertRaises(server.AttachFailed):
                            await server.Chat("waseem").send("Generate", [product])
                    self.assertEqual([c.args[2] for c in ex.await_args_list], ["compose"])


def conversation(*entries):
    return {"mapping": {str(n): {"message": {"author": {"role": role}, "create_time": at,
            "content": {"parts": [{"content_type": "image_asset_pointer", "asset_pointer": "sediment://" + fid}]}}}
            for n, (role, at, fid) in enumerate(entries)}}


class ImageDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def run_wait(self, dom, conv):
        clock = [1000.0]
        async def sleep(seconds):
            clock[0] += seconds
        chat = server.Chat("waseem")
        chat.generating = AsyncMock(return_value=False)
        chat.conversation = AsyncMock(side_effect=conv) if isinstance(conv, Exception) else AsyncMock(return_value=conv)
        chat.send = AsyncMock()
        with patch.object(server.time, "time", side_effect=lambda: clock[0]), \
             patch.object(server.asyncio, "sleep", side_effect=sleep), \
             patch.object(server.HUB, "exec", new=AsyncMock(side_effect=lambda role, tab, fn, *args: dom if fn == "turn_images" else "")), \
             patch.object(server, "log"):
            found = await server.wait_for_images(chat, "cid",
                {"https://chatgpt.com/backend-api/estuary/content?id=file_old&sig=old"}, 1, sent_at=1000)
        chat.send.assert_not_awaited()
        return found

    async def test_signed_url_change_cannot_count_old_image_as_new(self):
        found = await self.run_wait({"imgs": ["https://chatgpt.com/backend-api/estuary/content?id=file_old&sig=new"]},
            conversation(("tool", 500, "file_old"), ("user", 1001, "file_upload"), ("tool", 1010, "file_new")))
        self.assertEqual(found, ["file_new"])

    async def test_failed_detection_does_not_send_duplicate_generation(self):
        self.assertEqual(await self.run_wait({"imgs": []}, RuntimeError("network unavailable")), [])

    def test_api_ignores_inactive_branch_and_images_older_than_send(self):
        conv = conversation(("tool", 999, "file_old"), ("tool", 1001, "file_wrongbranch"),
                            ("user", 1001, "file_upload"), ("tool", 1010, "file_new"))
        conv["current_node"] = "3"
        for key, parent in (("3", "2"), ("2", "0")):
            conv["mapping"][key]["parent"] = parent
        self.assertEqual(server.image_ids_after(conv, 1000), ["file_new"])


class ParserRegressionTests(unittest.TestCase):
    def test_label_link_hash_emoji_headings_and_magnific_video(self):
        doc = """Post 1 https://magnific.com/premium-photo/example.jpg
Headings: Exact heading
Caption: Caption only
📌 Post#2
Heading: Video heading
https://magnific.com/premium-video/example
Post#3
Story
https://example.com/story.jpg"""
        designs, reels, _ = server.parse_doc(doc)
        self.assertEqual(len(designs), 2)
        self.assertEqual(len(reels), 1)
        self.assertEqual(designs[0]["content"], "Exact heading")
        self.assertIn("magnific.com", designs[0]["image_url"])
        self.assertEqual(designs[1]["kind"], "story")
        self.assertTrue(designs[1]["text_from_image"])
        designs[1]["image_path"] = "story.jpg"
        msg, files, _ = server.build_design_message("Example", [designs[1]])
        self.assertIn("use the exact text written in that image", msg)
        self.assertEqual(files, ["story.jpg"])

    def test_carousel_preserves_each_slide_text_image_and_parent_section(self):
        designs, _, _ = server.parse_doc("""Post 3 (Carousel)
Slide 1: First heading
https://example.com/one.jpg
Slide 2: Second heading
https://example.com/two.jpg
Caption: Not slide text""")
        self.assertEqual([d["content"] for d in designs], ["First heading", "Second heading"])
        self.assertEqual([d["image_url"] for d in designs], ["https://example.com/one.jpg", "https://example.com/two.jpg"])
        self.assertEqual([d["slide"] for d in designs], [1, 2])
        parent = server.split_sections([("Post 3 (Carousel)", False)])[0]["label"]
        self.assertEqual([d["section"] for d in designs], [parent] * 2)


def setUpModule():  # ChatGPT ke aaram (minute) tests mein fauran
    global _rest_patch
    _rest_patch = patch.object(server, "chatgpt_rest", new=AsyncMock())
    _rest_patch.start()
    global _log_patch  # asal Output\company_log.json kabhi na parho
    _log_patch = patch.object(server, "COMPANY_LOG", server.BASE / "tests" / "_no_company_log.json")
    _log_patch.start()


def tearDownModule():
    _rest_patch.stop()
    _log_patch.stop()
