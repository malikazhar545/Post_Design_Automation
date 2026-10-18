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
        self.edit_result, self.calls = edit_result, []

    async def call(self, role, cmd, timeout=600, **args):
        self.calls.append(cmd)
        if cmd == "gmail_tab":
            return 7
        if cmd == "list_doc_tabs":
            return list(self.docs)
        return True

    async def exec(self, role, tab, fn, *args, timeout=600):
        self.calls.append(fn)
        if fn == "gmail_state":
            return {"ready": True, "rows": [{"i": 0, "id": "t1", "text": "Client Wear Wood posts Wear Wood 9.docx"},
                                            {"i": 1, "id": "t2", "text": "Invoice from someone else"}]}
        if fn == "gmail_open_row":
            return True
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

    async def test_off_or_empty_list_does_nothing(self):
        await gmail_watch.scan()
        self.assertEqual(self.hub.calls, [])

    async def test_failed_open_retries_three_times_then_tells_user(self):
        self.hub.edit_result = "Edit with Google Docs button nahi mila"
        gmail_watch.api_action("gmail_add", {"name": "Wear Wood"})
        gmail_watch.api_action("gmail_toggle", {"on": "1"})
        for _ in range(4):
            await gmail_watch.scan()
        self.assertEqual(self.hub.calls.count("gmail_edit"), 3)
        self.assertIn("Gmail: file nahi khuli", self.notes)
        self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main()
