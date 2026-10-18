"""Download-only smoke test using the running server's extension bridge.

Uses the unchanged production download functions; never starts a design run.
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from PIL import Image
from websockets.asyncio.client import connect


class DebugBridge:
    def __init__(self):
        self.clients = {}
        self.events = []

    async def call(self, role, cmd, timeout=120, **args):
        async with connect("ws://127.0.0.1:8765", max_size=None) as ws:
            await ws.send(json.dumps({"type": "debug", "role": role, "cmd": cmd, "args": args}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout + 10))
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "Extension request failed"))
        result = reply.get("result")
        event = {"role": role, "command": cmd}
        if cmd in ("open_tab", "wait_download", "close_tab"):
            event["result"] = result
        if cmd == "exec":
            event["function"] = args["fn"]
            if args["fn"] in ("freepik_click", "envato_click"):
                event["result"] = result
        self.events.append(event)
        return result

    async def exec(self, role, tab, fn, *args, timeout=120):
        result = await self.call(role, "exec", tabId=tab, fn=fn, args=list(args), timeout=timeout)
        if fn in ("freepik_click", "envato_click"):
            print(f"{role}: {fn} -> {result}", flush=True)
            self.events.append({"diagnostic": await self.call(role, "exec", tabId=tab, fn="diag", args=[])})
        return result


async def main():
    bridge = DebugBridge()
    server.HUB = bridge
    folder = server.BASE / "Output" / "download-tests" / datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True)
    print(f"Test folder: {folder}", flush=True)
    cases = [
        ("freepik", "designer", "https://www.magnific.com/free-photo/full-shot-parent-taking-phone-from-kid_27639629.htm"),
        ("envato", "envato", "https://elements.envato.com/view-from-below-of-a-group-of-sporty-people-in-a-h-PX737JP"),
    ]
    results = []
    for name, role, url in cases:
        result = {"site": name, "url": url}
        print(f"Testing {name} download...", flush=True)
        start = len(bridge.events)
        try:
            await bridge.call(role, "now")
            bridge.clients[role] = True
            path = await server.download_image(url, folder / name)
            with Image.open(path) as img:
                result.update(format=img.format, width=img.width, height=img.height)
                img.verify()
            result.update(status="PASS", path=str(path), bytes=path.stat().st_size)
        except Exception as e:
            result.update(status="FAIL", error=str(e))
        result["events"] = bridge.events[start:]
        results.append(result)
        (folder / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "events"}), flush=True)
    print(f"Evidence: {folder / 'results.json'}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
