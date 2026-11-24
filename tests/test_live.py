"""Live recording: a live answer with no stream URL, a stop during the stream lookup, and wait timeout fallback.
Runs against the code directly with its own empty data folder, and never starts a real ffmpeg or contacts a site."""
import os, subprocess, sys, tempfile, threading, time
from pathlib import Path
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-live-"); os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")
import yt_dlp
from studio import auth, creators, ctx, db, jobs, live
db.init()
threading.Thread(target=jobs._live_worker, daemon=True).start()   # the live cleanup queue only runs when this is up
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n + (f"  {x}" if x and not ok else "")); ok or fails.append(n)

u, _ = auth._create("live@example.com", "Live", password_hash="x")
TWITCH = "https://www.twitch.tv/somebody"

# Test 1: yt-dlp answers live but gives no stream address
yt_dlp.YoutubeDL.extract_info = lambda self, url, download=False: {"is_live": True, "requested_formats": [{}]}
try:
    live.resolve(TWITCH)
    ok = False
except live.NotLive:
    ok = True
except Exception as e:  # noqa: BLE001
    ok = False
    print("  got", type(e).__name__, e)
check("a live answer with no stream address raises NotLive, not KeyError", ok)

def wait_for(cond, secs=4.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()

with ctx.as_user(u.id):
    cr = creators.create({"name": "Streamer", "channel": TWITCH})

    # Test 2: stop() comes while the stream address is still being looked up
    entered, stopped = threading.Event(), threading.Event()
    popen_calls = []
    real_popen = live.subprocess.Popen

    def slow_resolve(url):
        entered.set()
        stopped.wait(4)
        return {"urls": ["http://127.0.0.1:9/x.m3u8"], "title": "t", "uploader": "u"}

    def fake_popen(*a, **k):
        popen_calls.append(a)
        raise FileNotFoundError("ffmpeg missing")

    live.resolve = slow_resolve
    live.subprocess.Popen = fake_popen
    try:
        j = jobs.create({"type": "live", "url": TWITCH, "name": "y live"}, {}, enqueue=False, creator=cr["id"], kind="live")
        j.update(status="live", stage="Connecting to the stream"); jobs.save(j)
        s = live.Session(j, cr)
        s.start()
        entered.wait(4)
        s.stop()
        stopped.set()
        done = wait_for(lambda: s.ended and s.finished)
    finally:
        live.subprocess.Popen = real_popen
    check("ffmpeg is never started when stop came during the lookup", not popen_calls, f"Popen calls={len(popen_calls)}")
    check("a session stopped during the lookup ends", done, f"ended={s.ended} finished={s.finished}")

    # Test 3: an unresponsive ffmpeg process on stop() falls back to kill() after wait timeout
    killed = []
    class StuckProc:
        class _Out:
            def read(self, n): return b""
            def close(self): pass
        stdout = _Out()
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None):
            if timeout is not None and not killed:
                raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
            return 0
        def kill(self):
            killed.append(True)

    live.resolve = lambda url: {"urls": ["http://127.0.0.1:9/x.m3u8"], "title": "t", "uploader": "u"}
    def one_shot_popen(*a, **k):
        s3.stop_flag.set()
        return StuckProc()
    live.subprocess.Popen = one_shot_popen
    try:
        j3 = jobs.create({"type": "live", "url": TWITCH, "name": "z live"}, {}, enqueue=False, creator=cr["id"], kind="live")
        j3.update(status="live", stage="Connecting to the stream"); jobs.save(j3)
        s3 = live.Session(j3, cr)
        s3.start()
        done3 = wait_for(lambda: s3.ended)
    finally:
        live.subprocess.Popen = real_popen
    check("a hung ffmpeg process is force-killed after wait timeout", bool(killed) and done3, f"killed={killed} ended={s3.ended}")

    # Test 4: live clip rendering concats buffer segments properly with resolved paths
    j4 = jobs.create({"type": "live", "url": TWITCH, "name": "clip live"},
                     {"layout": "blur", "captions": False}, enqueue=False, creator=cr["id"], kind="live")
    s4 = live.Session(j4, cr)
    s4.ended = True
    seg1 = s4.buf / "p01_00001.ts"
    seg2 = s4.buf / "p01_00002.ts"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=d=3:s=320x240:r=25", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-c:v", "libx264", "-c:a", "aac", "-t", "3", str(seg1)], capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=d=3:s=320x240:r=25", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-c:v", "libx264", "-c:a", "aac", "-t", "3", str(seg2)], capture_output=True)
    s4.segments = [(seg1, 0.0, 3.0), (seg2, 3.0, 6.0)]
    s4.seg_clock = 6.0
    c4 = {"id": "clip001", "start": 0.5, "end": 4.5, "status": "queued", "layout": "blur", "captions": False, "style": "hype",
          "title": "Test Live Clip", "description": "desc", "hashtags": [], "score": 80, "hook": "HOOK"}
    j4["clips"].append(c4)
    s4._render(c4)
    check("live clip rendered from buffer segments successfully", c4.get("status") == "done", f"status={c4.get('status')} err={c4.get('error')}")
    check("live clip source .src.ts is created", Path(c4.get("src", "")).exists(), f"src={c4.get('src')}")

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil; shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
