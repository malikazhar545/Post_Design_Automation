"""START button ki jagah command line se: python trigger_start.py [file naam ka hissa]"""
import asyncio, json, sys
from websockets.asyncio.client import connect

async def main():
    async with connect("ws://127.0.0.1:8765") as ws:
        await ws.send(json.dumps({"type": "start", "only": sys.argv[1] if len(sys.argv) > 1 else None,
                                  "only_post": sys.argv[2] if len(sys.argv) > 2 else None}))
    print("START bhej diya")

asyncio.run(main())
