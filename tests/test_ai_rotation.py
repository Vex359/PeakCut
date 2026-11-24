"""Testing API key rotation and fallback chain for PeakCut."""
import json
import os
import sys
import tempfile
import time

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-rot-")
os.environ["DATABASE_URL"] = ""
os.environ.update(PEAKCUT_AI_PROVIDER="groq", PEAKCUT_AI_MODELS="llama-3.3-70b-versatile", PEAKCUT_AI_KEY="", PEAKCUT_AI_BASE_URL="")
sys.path.insert(0, ".")
from studio import ai, brain, config, db  # noqa: E402

db.init()
fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    ok or fails.append(name)


# 1. Test key parsing
k_list = ai._parse_keys("gsk_key1\ngsk_key2\r\ngsk_key3")
check("parse multiline keys", k_list == ["gsk_key1", "gsk_key2", "gsk_key3"], str(k_list))

k_commas = ai._parse_keys("  gsk_aaa,  gsk_bbb , gsk_ccc  ")
check("parse comma separated keys", k_commas == ["gsk_aaa", "gsk_bbb", "gsk_ccc"], str(k_commas))

k_dups = ai._parse_keys(["gsk_one", "gsk_two", "gsk_one", " ", "gsk_three"])
check("deduplicate and filter empty keys", k_dups == ["gsk_one", "gsk_two", "gsk_three"], str(k_dups))


# 2. Test saving multiple keys
pub = ai.save("groq", "llama-3.3-70b-versatile", "", "gsk_alpha1111\ngsk_beta2222\ngsk_gamma3333")
check("public reports has_key", pub["has_key"] is True)
check("public reports key_count=3", pub["key_count"] == 3)
check("public reports key_hints", pub["key_hints"] == ["1111", "2222", "3333"], str(pub["key_hints"]))
check("ai.key() returns primary key", ai.key() == "gsk_alpha1111")
check("ai.keys() returns all keys", ai.keys() == ["gsk_alpha1111", "gsk_beta2222", "gsk_gamma3333"])

# Check file contents
raw = json.loads(ai._file().read_text(encoding="utf-8"))
check("file stores primary key for backwards compatibility", raw.get("key") == "gsk_alpha1111")
check("file stores keys array", raw.get("keys") == ["gsk_alpha1111", "gsk_beta2222", "gsk_gamma3333"])


# 3. Test round-robin rotation and fallback chain under mocked calls
calls_made = []


class MockReply:
    def __init__(self, status_code, data, text="", headers=None):
        self.status_code = status_code
        self._data = data
        self.text = text or json.dumps(data)
        self.headers = headers or {}

    def json(self):
        return self._data


CLIP = {"start": 1, "end": 20, "score": 80, "hook": "WOW", "title": "t", "description": "d", "hashtags": ["a"], "reason": "r"}
CLIP_RESPONSE = {"choices": [{"message": {"content": json.dumps({"items": [CLIP]})}}],
                 "usage": {"prompt_tokens": 50, "completion_tokens": 10}}

# Mock state
key_status = {
    "gsk_alpha1111": 200,
    "gsk_beta2222": 200,
    "gsk_gamma3333": 200,
}


def mock_post(url, json=None, headers=None, timeout=None, **_k):
    auth_h = (headers or {}).get("Authorization", "")
    used_key = auth_h.removeprefix("Bearer ").strip()
    calls_made.append(used_key)

    st = key_status.get(used_key, 200)
    if st == 200:
        return MockReply(200, CLIP_RESPONSE)
    elif st == 429:
        return MockReply(429, {"error": {"message": "Rate limit reached for requests per minute (RPM)", "type": "tokens", "code": "rate_limit_exceeded"}},
                         headers={"retry-after": "30"})
    elif st == 401:
        return MockReply(401, {"error": {"message": "Invalid API key"}})
    return MockReply(500, {"error": {"message": "Server error"}})


ai.httpx.post = mock_post

# Test round-robin rotation across healthy keys
calls_made.clear()
ai.call("llama-3.3-70b-versatile", "prompt 1", brain.SCHEMA)
check("call 1 used key 1", calls_made[-1] == "gsk_alpha1111", str(calls_made))

ai.call("llama-3.3-70b-versatile", "prompt 2", brain.SCHEMA)
check("call 2 rotated to key 2", calls_made[-1] == "gsk_beta2222", str(calls_made))

ai.call("llama-3.3-70b-versatile", "prompt 3", brain.SCHEMA)
check("call 3 rotated to key 3", calls_made[-1] == "gsk_gamma3333", str(calls_made))

ai.call("llama-3.3-70b-versatile", "prompt 4", brain.SCHEMA)
check("call 4 wrapped around to key 1", calls_made[-1] == "gsk_alpha1111", str(calls_made))


# 4. Test rate-limit fallback chain
# Let's say key 2 hits 429 rate limit
key_status["gsk_beta2222"] = 429

calls_made.clear()
# Next in rotation would be key 2, which will hit 429, then fall back immediately to key 3!
res, sent, wrote = ai.call("llama-3.3-70b-versatile", "prompt with 429", brain.SCHEMA)
check("call succeeded despite rate limit", res == [CLIP])
check("call attempted key 2 then immediately fell back to key 3", calls_made == ["gsk_beta2222", "gsk_gamma3333"], str(calls_made))

# Key 2 is now on cooldown. The next call should skip key 2 while it cools down!
calls_made.clear()
ai.call("llama-3.3-70b-versatile", "prompt after rate limit", brain.SCHEMA)
check("key 2 was skipped due to cooldown", "gsk_beta2222" not in calls_made, str(calls_made))


# 5. Test chain exhaust: all keys hit 429
key_status["gsk_alpha1111"] = 429
key_status["gsk_beta2222"] = 429
key_status["gsk_gamma3333"] = 429

calls_made.clear()
try:
    ai.call("llama-3.3-70b-versatile", "all rate limited", brain.SCHEMA)
    exhaust_ok = False
except ai.AIError as e:
    exhaust_ok = True
    exp = ai.explain(e)
    check("explain reports all keys hit rate limits", "All 3 Groq keys have hit their rate limits" in exp, exp)

check("all keys rate limited raises AIError", exhaust_ok)
check("all keys were attempted before giving up", len(set(calls_made)) == 3, str(calls_made))


# 6. Test error message masks keys
try:
    fake_r = MockReply(429, {}, text="Error with key gsk_alpha1111 and gsk_beta2222")
    ai._fail(fake_r, "test-model")
except ai.AIError as e:
    check("keys are masked with <key> in error text", "gsk_" not in str(e) and "<key>" in str(e), str(e))

if fails:
    print(f"\n{len(fails)} checks failed.")
    sys.exit(1)
else:
    print("\nALL ROTATION TESTS PASSED")
