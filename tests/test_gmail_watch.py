import asyncio
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import gmail_watch
import server


class FakeHub:
    """Designer profile ki jagah: ek search result, ek email jis mein Wear Wood 9.docx."""

    def __init__(self, edit_result="ok"):
        self.clients = {"designer": object()}
        self.pause_requested = self.paused = self.running = False
        self.current, self.watching = {}, False
        self.docs = [{"url": "https://docs.google.com/document/d/old/edit", "title": "Old - Google Docs"}]
        self.edit_result, self.calls, self.url, self.links, self.urls = edit_result, [], "", [], []

    async def call(self, role, cmd, timeout=600, **args):
        self.calls.append(cmd)
        if cmd == "gmail_tab":
            return 7
        if cmd == "list_doc_tabs":
            return list(self.docs)
        if cmd == "goto_quiet":
            self.url = args["url"]
            self.urls.append(args["url"])
        if cmd == "open_tab":  # Drive link -> Docs tab
            self.docs.append({"url": args["url"], "title": "Wear Wood 10 - Google Docs"})
        return True

    async def exec(self, role, tab, fn, *args, timeout=600):
        self.calls.append(fn)
        if fn == "gmail_state":  # Gmail search: sirf woh email jis mein company ka naam ho
            from urllib.parse import unquote
            rows = [{"i": 0, "id": "t1", "text": "Client Wear Wood posts Wear Wood 9.docx", "unread": True},
                    {"i": 1, "id": "t2", "text": "Invoice from someone else", "unread": True}]
            name = (unquote(self.url).split('"') + ["", ""])[1].lower()
            found = [r for r in rows if name and name in r["text"].lower()]
            return {"ready": True, "hash": "#" + self.url.split("#", 1)[-1], "rows": found, "empty": not found}
        if fn == "gmail_open_row":
            return True
        if fn == "gmail_doc_links":
            return list(self.links)
        if fn == "gmail_attachments":
            return [{"i": 0, "name": "Wear Wood 9.docx", "edit": True}]
        if fn == "gmail_edit":
            if self.edit_result == "ok":
                self.docs.append({"url": "https://docs.google.com/document/d/new/edit", "title": "Wear Wood 9.docx - Google Docs"})
            return self.edit_result


class GmailWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = server.BASE / "tests" / "_runs" / uuid4().hex
        self.root.mkdir(parents=True)
        self.started, self.notes = [], []
        self.hub = FakeHub()
        gmail_watch.setup(SimpleNamespace(
            OUT_ROOT=self.root, HUB=self.hub, CFG={"gmail_autostart": True}, norm=server.norm, folder_of=server.folder_of,
            load_state=lambda: {}, log=lambda m: None, notify=lambda t, m, sticky=True: self.notes.append(t),
            start_run=lambda a, b: self.started.append(True)))
        gmail_watch.STATUS.update(busy=False, last_scan=None)
        self.sleep, gmail_watch.asyncio.sleep = gmail_watch.asyncio.sleep, self._no_sleep

    def tearDown(self):
        gmail_watch.asyncio.sleep = self.sleep
        shutil.rmtree(self.root)

    async def _no_sleep(self, s):
        await self.sleep(0)

    async def test_listed_company_file_opened_once_and_run_started(self):
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood, Belle Clinic"})
        gmail_watch.api_action("gmail_toggle", {"on": "1"})
        await gmail_watch.scan()
        data = gmail_watch.load()
        self.assertIn("t1|Wear Wood 9.docx", data["opened"])
        self.assertEqual(self.started, [True])           # server idle tha -> automation khud START
        self.assertEqual(self.hub.calls.count("gmail_open_row"), 1)  # sirf list wali company ki email
        self.assertTrue(data["items"][0]["found"])
        await gmail_watch.scan()                          # dobara scan: wahi file dobara nahi
        self.assertEqual(self.hub.calls.count("gmail_edit"), 1)

    async def test_never_touches_gmail_while_a_file_is_being_made(self):
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        gmail_watch.api_action("gmail_toggle", {"on": "1"})
        self.hub.running, self.hub.current = True, {"file": "Belle Clinic 9"}
        await gmail_watch.scan()
        self.assertEqual(self.hub.calls, [])
        self.hub.current, self.hub.paused = {}, True      # run pause par -> bhi nahi
        await gmail_watch.scan()
        self.assertEqual(self.hub.calls, [])

    async def test_already_open_file_marked_found_not_reopened(self):
        self.hub.docs.append({"url": "https://docs.google.com/document/d/x/edit", "title": "Wear Wood 9.docx - Google Docs"})
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        await gmail_watch.scan(force=True)
        self.assertEqual(self.hub.calls.count("gmail_edit"), 0)
        self.assertTrue(gmail_watch.found_today(gmail_watch.load()["items"][0]))
        self.assertEqual(self.started, [])

    async def test_drive_link_in_email_opened_in_docs(self):
        self.hub.links = [{"id": "1WQm21Kj9r2J_qv_OgBEHwo7s8LsLuGgsVYEbcCZs86U", "name": "Wear Wood 10", "url": "https://drive.google.com/open?id=x"}]
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        await gmail_watch.scan(force=True)
        self.assertIn("open_tab", self.hub.calls)
        self.assertTrue(any("docs.google.com/document/d/1WQm21Kj9r2J_qv_OgBEHwo7s8LsLuGgsVYEbcCZs86U/edit" in d["url"] for d in self.hub.docs))
        self.assertEqual(self.started, [True])

    async def test_same_name_done_last_month_is_a_new_file(self):
        gmail_watch.setup(SimpleNamespace(**{**vars(gmail_watch.S), "load_state": lambda: {
            "d": {"status": "done", "title": "Wear Wood 9.docx - Google Docs", "at": "2020-01-05T10:00:00"}}}))
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        await gmail_watch.scan(force=True)
        self.assertEqual(self.hub.calls.count("gmail_edit"), 1)   # pichle mahine ka "Wear Wood 9" -> yeh nayi file

    async def test_old_done_file_not_reopened_and_company_keeps_waiting(self):
        from datetime import datetime
        gmail_watch.setup(SimpleNamespace(**{**vars(gmail_watch.S), "load_state": lambda: {
            "d": {"status": "done", "title": "Wear Wood 9.docx - Google Docs", "at": datetime.now().isoformat()}}}))
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        await gmail_watch.scan(force=True)
        self.assertEqual(self.hub.calls.count("gmail_edit"), 0)
        self.assertFalse(gmail_watch.found_today(gmail_watch.load()["items"][0]))  # aaj ki nayi ka intezar

    def test_name_split_into_parts_without_too_common_words(self):
        gmail_watch.setup(SimpleNamespace(**{**vars(gmail_watch.S), "STOPWORDS": server.STOPWORDS, "NOISE_WORDS": server.NOISE_WORDS}))
        self.assertEqual(gmail_watch.name_parts("Belle Clinic London UK"),
                         ["Belle Clinic London UK", "BelleClinicLondonUK", "Belle Clinic London", "Belle Clinic", "Belle"])
        self.assertNotIn("Wear", gmail_watch.name_parts("Wear Wood"))     # 4 harf akela lafz bohat aam
        self.assertIn("Molen tax", gmail_watch.name_parts("The Molen tax"))
        self.assertNotIn("The", gmail_watch.name_parts("The Molen tax"))

    async def test_partial_name_match_opens_only_file_with_that_part(self):
        self.hub.links = [{"id": "1" * 30, "name": "Random newsletter", "url": "u"}]
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood Pakistan"})  # poora naam Gmail mein nahi
        await gmail_watch.scan(force=True)
        self.assertNotIn("open_tab", self.hub.calls)                     # 'Wear Wood' hissa -> naam wali file hi
        self.assertIn("t1|Wear Wood 9.docx", gmail_watch.load()["opened"])

    async def test_two_available_files_older_one_opens_first_and_search_is_5_days(self):
        rows = [{"i": 0, "id": "new", "text": "Wear Wood 11 18 Oct", "unread": True},
                {"i": 1, "id": "old", "text": "Wear Wood 10 16 Oct", "unread": True}]
        order = []
        orig = self.hub.exec

        async def exec_(role, tab, fn, *args, timeout=600):
            if fn == "gmail_state":
                return {"ready": True, "rows": rows, "hash": "#" + self.hub.url.split("#", 1)[-1]}
            if fn == "gmail_open_row":
                order.append(args[0])
            return await orig(role, tab, fn, *args, timeout=timeout)
        self.hub.exec = exec_
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        await gmail_watch.scan(force=True)
        self.assertEqual(order, ["old", "new"])                  # neeche wali (purani) pehle
        self.assertTrue(any("newer_than%3A5d" in u for u in self.hub.urls))           # 5 din se purani email search hi nahi

    async def test_edit_button_that_appears_late_still_opens_file(self):
        calls = {"n": 0}
        orig = self.hub.exec

        async def exec_(role, tab, fn, *args, timeout=600):
            if fn == "gmail_edit":
                calls["n"] += 1
                if calls["n"] < 5:  # pehli 4 dafa button abhi nahi aaya (Gmail ka 5-10 second delay)
                    self.hub.calls.append(fn)
                    return "Edit with Google Docs button nahi mila"
            return await orig(role, tab, fn, *args, timeout=timeout)
        self.hub.exec = exec_
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        await gmail_watch.scan(force=True)
        self.assertIn("t1|Wear Wood 9.docx", gmail_watch.load()["opened"])
        self.assertNotIn("note", gmail_watch.load()["opened"]["t1|Wear Wood 9.docx"])

    async def test_three_available_files_only_two_oldest_open_today(self):
        rows = [{"i": 0, "id": "c", "text": "Wear Wood 12 18 Oct", "unread": True},
                {"i": 1, "id": "b", "text": "Wear Wood 11 17 Oct", "unread": True},
                {"i": 2, "id": "a", "text": "Wear Wood 10 16 Oct", "unread": True}]
        order, n = [], {"k": 0}
        orig = self.hub.exec

        async def exec_(role, tab, fn, *args, timeout=600):
            if fn == "gmail_state":
                return {"ready": True, "rows": rows, "hash": "#" + self.hub.url.split("#", 1)[-1]}
            if fn == "gmail_open_row":
                order.append(args[0])
            if fn == "gmail_attachments":
                return [{"i": 0, "name": f"Wear Wood {order[-1]}.docx", "edit": True}]
            if fn == "gmail_edit":
                n["k"] += 1
                self.hub.docs.append({"url": f"https://docs.google.com/document/d/n{n['k']}/edit", "title": f"Doc {n['k']} - Google Docs"})
                return "ok"
            return await orig(role, tab, fn, *args, timeout=timeout)
        self.hub.exec = exec_
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        await gmail_watch.scan(force=True)
        self.assertEqual(order, ["a", "b"])                      # sab se purani 2 — teesri (c) kal
        self.assertEqual(len(gmail_watch.found_today(gmail_watch.load()["items"][0])), 2)
        await gmail_watch.scan(force=True)                       # aaj dobara scan: had puri -> kuch nahi
        self.assertEqual(order, ["a", "b"])

    async def test_read_emails_are_never_opened(self):
        rows = [{"i": 0, "id": "r1", "text": "Wear Wood 9 16 Oct", "unread": False}]
        orig = self.hub.exec

        async def exec_(role, tab, fn, *args, timeout=600):
            if fn == "gmail_state":
                return {"ready": True, "rows": rows, "hash": "#" + self.hub.url.split("#", 1)[-1]}
            return await orig(role, tab, fn, *args, timeout=timeout)
        self.hub.exec = exec_
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        await gmail_watch.scan(force=True)
        self.assertNotIn("gmail_open_row", self.hub.calls)   # read = aap dekh chuke -> haath nahi lagana

    async def test_continue_editing_dialog_clicks_edit_latest(self):
        orig = self.hub.exec
        state = {"dialog": False}

        async def exec_(role, tab, fn, *args, timeout=600):
            if fn == "gmail_edit":  # Gmail: "Continue editing?" -> tab abhi nahi khulti
                state["dialog"] = True
                return "ok"
            if fn == "gmail_continue_dialog":
                if state["dialog"]:
                    state["dialog"] = False
                    self.hub.docs.append({"url": "https://docs.google.com/document/d/latest/edit", "title": "Wear Wood 9.docx - Google Docs"})
                    return True
                return False
            return await orig(role, tab, fn, *args, timeout=timeout)
        self.hub.exec = exec_
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        await gmail_watch.scan(force=True)
        self.assertIn("t1|Wear Wood 9.docx", gmail_watch.load()["opened"])
        self.assertNotIn("note", gmail_watch.load()["opened"]["t1|Wear Wood 9.docx"])

    async def test_link_to_already_made_doc_by_id_not_opened(self):
        self.hub.links = [{"id": "1p-CnuOF3ahd308lq8g4_ADm_2ieKnForK-23yBKsc9o", "name": "1p-CnuOF3ahd308lq8g4_ADm_2ieKnForK-23yBKsc9o", "url": "u"}]
        gmail_watch.setup(SimpleNamespace(**{**vars(gmail_watch.S), "load_state": lambda: {
            "1p-CnuOF3ahd308lq8g4_ADm_2ieKnForK-23yBKsc9o": {"status": "done", "title": "Wear Wood 8 - Google Docs"}}}))
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        await gmail_watch.scan(force=True)
        self.assertNotIn("open_tab", self.hub.calls)        # ban chuki file (doc id se) dobara nahi khuli
        rec = gmail_watch.load()["opened"]["t1|1p-CnuOF3ahd308lq8g4_ADm_2ieKnForK-23yBKsc9o"]
        self.assertEqual(rec["note"], "pehle ban chuki")

    def test_spreadsheet_paste_adds_every_name(self):
        gmail_watch.api_action("gmail_add", {"name": "1. Wear Wood\tBelle Clinic\r\n2) Linked up\n\nWear Wood\n• Molen tax"})
        self.assertEqual([i["name"] for i in gmail_watch.load()["items"]], ["Wear Wood", "Belle Clinic", "Linked up", "Molen tax"])

    async def test_off_or_empty_list_does_nothing(self):
        await gmail_watch.scan()
        self.assertEqual(self.hub.calls, [])

    async def test_failed_open_retries_three_times_then_tells_user(self):
        self.hub.edit_result = "Edit with Google Docs button nahi mila"
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        gmail_watch.api_action("gmail_toggle", {"on": "1"})
        for _ in range(4):
            await gmail_watch.scan()
        self.assertEqual(self.hub.calls.count("gmail_edit"), 3 * 17)  # 3 koshishen, har ek mein ~25 second sabr
        self.assertIn("Gmail: file nahi khuli", self.notes)
        self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main()
