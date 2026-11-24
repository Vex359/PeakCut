"""Tests for manual clipping mode and adding more clips."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, ".")

from studio import jobs, brain, server, db, auth, config
from fastapi.testclient import TestClient

db.init()
fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    ok or fails.append(name)


# 1. clean_options manual_clip valid
opts = jobs.clean_options({"manual_clip": {"start": "10.5", "end": "45.0"}})
check("clean_options manual clip valid", "manual_clip" in opts and opts["manual_clip"]["start"] == 10.5 and opts["manual_clip"]["end"] == 45.0 and opts["max_clips"] == 1)

# 2. clean_options manual clip > 60s raises ValueError
try:
    jobs.clean_options({"manual_clip": {"start": "0", "end": "61.0"}})
    check("manual clip >60s rejected", False, "did not raise")
except ValueError as e:
    check("manual clip >60s rejected", "cannot exceed 60 seconds" in str(e))

# 3. clean_options manual clip invalid order
try:
    jobs.clean_options({"manual_clip": {"start": "50", "end": "40"}})
    check("manual clip start >= end rejected", False, "did not raise")
except ValueError as e:
    check("manual clip start >= end rejected", "end after start" in str(e))

# 4. add_clips_to_job manual mode valid
fake_job = {
    "id": "test_j1",
    "dir": "tmp_test_dir",
    "title": "Test Video",
    "uploader": "Tester",
    "duration": 300,
    "options": {"style": "bounce", "layout": "blur", "captions": True},
    "clips": [{"id": "clip01", "start": 10.0, "end": 40.0, "status": "done"}],
    "status": "done"
}
orig_save = jobs.save
orig_put = jobs._q.put
queued = []
jobs.save = lambda j: None
jobs._q.put = lambda item: queued.append(item)
try:
    added = jobs.add_clips_to_job(fake_job, {"mode": "manual", "start": 60.0, "end": 90.0})
    check("add_clips manual adds one clip", len(added) == 1 and added[0]["id"] == "clip02" and added[0]["start"] == 60.0 and added[0]["end"] == 90.0)
    check("add_clips queues new clip", len(queued) == 1 and queued[0] == ("clip", "test_j1", "clip02"))
finally:
    jobs.save = orig_save
    jobs._q.put = orig_put

# 5. add_clips_to_job manual mode > 60s rejected
try:
    jobs.add_clips_to_job({"id": "test_j2", "dir": "tmp_test_dir", "options": {}, "clips": [], "status": "done"}, {"mode": "manual", "start": 0.0, "end": 65.0})
    check("add_clips >60s rejected", False, "did not raise")
except ValueError as e:
    check("add_clips >60s rejected", "cannot exceed 60 seconds" in str(e))

# 6. brain overlap check
existing = [{"start": 20.0, "end": 50.0}]
check("brain overlap detected", brain._overlaps_existing(25.0, 55.0, existing))
check("brain non-overlap allowed", not brain._overlaps_existing(60.0, 90.0, existing))

# 7. Setup client and user
BASE = config.PUBLIC_URL or "http://127.0.0.1:8765"
ORIGIN = {"Origin": BASE}

if config.ACCOUNTS:
    try:
        u, _ = auth._create("test_manual@example.com", "test_manual", password_hash="x")
    except Exception:
        u = auth.get_user_by_email("test_manual@example.com")
    auth.mark_verified(u.id)
    client = TestClient(server.app, base_url=BASE, follow_redirects=False)
    client.cookies.set(auth.COOKIE, auth.start_session(u.id))
    user_id = u.id
else:
    u = auth.local_user()
    client = TestClient(server.app, base_url=BASE, follow_redirects=False)
    user_id = u.id

# 8. API add_clips endpoint
jid = "test_api_job_001"
api_job = {
    "id": jid,
    "user": user_id,
    "dir": "tmp_test_dir",
    "title": "API Test Video",
    "source": {"type": "file", "path": "test.mp4"},
    "uploader": "Tester",
    "duration": 500,
    "options": {"style": "bounce", "layout": "blur", "captions": True},
    "clips": [{"id": "clip01", "start": 10.0, "end": 35.0, "status": "done"}],
    "status": "done",
    "created": 1000
}
jobs._jobs[jid] = api_job
jobs._by_user.setdefault(user_id, {})[jid] = api_job
orig_save = jobs.save
orig_put = jobs._q.put
jobs.save = lambda j: None
jobs._q.put = lambda item: None
try:
    res = client.post(f"/api/jobs/{jid}/add_clips", json={"mode": "manual", "start": 50.0, "end": 120.0}, headers=ORIGIN)
    check("POST /add_clips >60s returns 400", res.status_code == 400 and "cannot exceed 60 seconds" in res.json().get("detail", ""))

    res2 = client.post(f"/api/jobs/{jid}/add_clips", json={"mode": "manual", "start": 50.0, "end": 95.0}, headers=ORIGIN)
    check("POST /add_clips valid returns 200", res2.status_code == 200 and res2.json().get("ok") and "clip02" in res2.json().get("added", []))

    res3 = client.post(f"/api/jobs/{jid}/add_clips", json={"mode": "manual", "start": 11.0, "end": 34.0}, headers=ORIGIN)
    check("POST /add_clips overlap returns 400", res3.status_code == 400 and "heavily overlaps" in res3.json().get("detail", ""))
finally:
    jobs.save = orig_save
    jobs._q.put = orig_put
    jobs._jobs.pop(jid, None)
    jobs._by_user.get(user_id, {}).pop(jid, None)

# 9. Frame extraction endpoint
test_dir = tempfile.mkdtemp(prefix="pc-test-frame-")
test_src = os.path.join(test_dir, "test.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2", test_src], capture_output=True)

frame_jid = "test_frame_job_001"
frame_job = {
    "id": frame_jid,
    "user": user_id,
    "dir": test_dir,
    "source_path": test_src,
    "duration": 2,
    "options": {"style": "bounce", "layout": "blur"},
    "clips": [{"id": "clip01", "start": 0.0, "end": 1.0, "status": "done"}],
    "status": "done",
    "created": 1000
}
jobs._jobs[frame_jid] = frame_job
jobs._by_user.setdefault(user_id, {})[frame_jid] = frame_job
try:
    res = client.get(f"/api/jobs/{frame_jid}/clips/clip01/frame?t=0.0")
    check("GET /frame extracts JPEG frame", res.status_code == 200 and res.headers.get("content-type") == "image/jpeg" and len(res.content) > 100)
finally:
    jobs._jobs.pop(frame_jid, None)
    jobs._by_user.get(user_id, {}).pop(frame_jid, None)
    shutil.rmtree(test_dir, ignore_errors=True)

if fails:
    sys.exit(1)
