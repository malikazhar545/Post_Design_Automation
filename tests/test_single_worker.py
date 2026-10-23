import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import server


class SingleWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_starts_cannot_replace_paused_worker(self):
        hub = server.Hub()
        ready, release = asyncio.Event(), asyncio.Event()
        async def worker(*args):
            ready.set()
            await release.wait()
        with patch.object(server, 'HUB', hub), patch.object(server, 'run_all', side_effect=worker) as run:
            self.assertTrue(server.start_run())
            owner = hub.run_task
            self.assertFalse(server.start_run())
            await ready.wait()
            hub.pause_requested = hub.paused = True
            self.assertFalse(server.start_run())
            self.assertIs(hub.run_task, owner)
            self.assertTrue(hub.paused)
            # Even a stale status flag cannot replace an existing worker.
            hub.running = False
            self.assertFalse(server.start_run())
            release.set()
            await owner
            self.assertEqual(run.call_count, 1)

    async def test_direct_duplicate_run_cannot_steal_owner(self):
        hub = server.Hub()
        owner = asyncio.create_task(asyncio.Event().wait())
        hub.run_task, hub.running, hub.paused = owner, True, True
        hub.current = {'file': 'First company'}
        with patch.object(server, 'HUB', hub):
            await server.run_all()
            self.assertIs(hub.run_task, owner)
            self.assertTrue(hub.paused)
            self.assertEqual(hub.current['file'], 'First company')
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)

    async def test_manual_start_during_autoresume_browser_wait_aborts_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / 'active.json'
            marker.write_text(json.dumps({'active': True, 'docs': [
                {'url': 'https://docs.google.com/document/d/example/edit', 'title': 'Example'}]}))
            hub = server.Hub()
            hub.clients = {role: object() for role in server.ROLES}
            async def call(role, command, **args):
                hub.running = True  # Manual START while list_doc_tabs was awaiting a reply.
                return []
            with patch.object(server, 'HUB', hub), patch.object(server, 'RUN_MARKER', marker), \
                 patch.object(hub, 'call', side_effect=call) as browser, \
                 patch.object(server.asyncio, 'sleep', new=AsyncMock()), \
                 patch.object(server, 'start_run') as start, patch.object(server, 'log'), patch.object(server, 'notify'):
                await server.auto_resume()
                start.assert_not_called()
                self.assertEqual(browser.call_count, 1)


if __name__ == '__main__':
    unittest.main()
