"""Debug: python debug_cmd.py <role> <cmd> '<json args>'   e.g.  python debug_cmd.py waseem chat_tab '{}'"""
import asyncio, json, sys
from websockets.asyncio.client import connect


async def main():
    role, cmd = sys.argv[1], sys.argv[2]
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    async with connect("ws://127.0.0.1:8765", max_size=None) as ws:
        await ws.send(json.dumps({"type": "debug", "role": role, "cmd": cmd, "args": args}))
        print(await ws.recv())

asyncio.run(main())
