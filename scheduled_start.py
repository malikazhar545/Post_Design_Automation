"""Mukarrar waqt par: server chalao (agar band ho) -> profiles judne ka intezar -> START.
python scheduled_start.py 17:40"""
import asyncio, datetime, json, socket, subprocess, sys, time
from pathlib import Path
from websockets.asyncio.client import connect

BASE = Path(__file__).resolve().parent
ROLES = {"designer", "waseem", "wajid", "instagram"}  # Envato extension band — screenshot Azhar profile se


def say(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def server_up():
    try:
        socket.create_connection(("127.0.0.1", 8765), 2).close()
        return True
    except OSError:
        return False


async def status():
    async with connect("ws://127.0.0.1:8765", max_size=None) as ws:
        await ws.send(json.dumps({"type": "status"}))
        return json.loads(await ws.recv())


async def main(at):
    h, m = map(int, at.split(":"))
    target = datetime.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    say(f"{target:%H:%M} ka intezar...")
    while datetime.datetime.now() < target:
        time.sleep(30)
    if not server_up():
        say("Server band hai — START_SERVER.bat chala raha hoon")
        subprocess.Popen(["cmd", "/c", "start", "", str(BASE / "START_SERVER.bat")], cwd=BASE)
        for _ in range(60):
            if server_up():
                break
            time.sleep(2)
    say("Server chal raha hai — Chrome profiles judne ka intezar (extensions khud update/reload hoti hain)")
    st = {}
    for _ in range(90):  # 15 minute tak
        try:
            st = await status()
            got = set(st.get("connected") or [])
            if st.get("busy"):
                say("Kaam pehle se chal raha hai — START dobara nahi dabaya")
                return
            if ROLES <= {r.lower() for r in got}:
                break
        except Exception:
            pass
        time.sleep(10)
    say(f"Status: {st}")
    time.sleep(15)  # extensions update ke baad settle
    async with connect("ws://127.0.0.1:8765") as ws:
        await ws.send(json.dumps({"type": "start", "only": None, "only_post": None}))
    say("START bhej diya")


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "17:40"))
