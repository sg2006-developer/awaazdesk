"""Protects a hosted demo: an access code, per-visitor rate limits and daily spend caps.

Every Gnani and LLM request costs credits, so a public URL needs limits. When
DEMO_ACCESS_CODE is empty (local development) the gate is open; the limits still apply.
"""
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import date

from fastapi import HTTPException

COOKIE = "awaazdesk_session"
SESSION_DAYS = 7


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


class Guard:
    def __init__(self):
        self.access_code = os.getenv("DEMO_ACCESS_CODE", "").strip()
        self.secret = (os.getenv("SESSION_SECRET") or secrets.token_hex(32)).encode()
        self.calls_per_hour = _int("CALLS_PER_HOUR_PER_VISITOR", 10)
        self.speech_per_minute = _int("SPEECH_REQUESTS_PER_MINUTE", 20)
        self.daily_calls = _int("DAILY_CALL_LIMIT", 150)
        self.daily_llm = _int("DAILY_LLM_LIMIT", 800)
        self.max_live_calls = _int("MAX_LIVE_CALLS", 4)
        self.max_call_seconds = _int("MAX_CALL_SECONDS", 300)
        self.windows = defaultdict(deque)
        self.day = date.today()
        self.spent = defaultdict(int)
        self.live_calls = 0

    # ------------------------------------------------------------ access code
    @property
    def required(self):
        return bool(self.access_code)

    def check_code(self, code):
        return not self.required or hmac.compare_digest(code.strip().encode(), self.access_code.encode())

    def issue(self):
        expires = str(int(time.time()) + SESSION_DAYS * 86400)
        return f"{expires}.{self._sign(expires)}"

    def valid(self, token):
        if not self.required:
            return True
        try:
            expires, signature = (token or "").split(".", 1)
            return hmac.compare_digest(signature, self._sign(expires)) and int(expires) > time.time()
        except ValueError:
            return False

    def _sign(self, value):
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    # ------------------------------------------------------------ limits
    def _rate(self, key, limit, seconds, message):
        now = time.monotonic()
        window = self.windows[key]
        while window and window[0] <= now - seconds:
            window.popleft()
        if len(window) >= limit:
            retry = int(window[0] + seconds - now) + 1
            raise HTTPException(429, f"{message} Try again in {retry} s.", headers={"Retry-After": str(retry)})
        window.append(now)

    def _spend(self, kind, limit, message):
        if date.today() != self.day:
            self.day, self.spent = date.today(), defaultdict(int)
        if self.spent[kind] >= limit:
            raise HTTPException(429, message)
        self.spent[kind] += 1

    def start_call(self, visitor):
        self._rate(("call", visitor), self.calls_per_hour, 3600, "You've started a lot of demo calls this hour.")
        self._spend("calls", self.daily_calls, "The demo has reached today's call limit. Please come back tomorrow.")

    def reasoning(self):
        self._spend("llm", self.daily_llm, "The demo has reached today's AI reasoning limit. Please come back tomorrow.")

    def speech(self, visitor):
        self._rate(("speech", visitor), self.speech_per_minute, 60, "Too many speech requests.")

    def enter_live(self):
        if self.live_calls >= self.max_live_calls:
            raise HTTPException(429, "All live demo lines are busy. Try again in a minute, or use a text call.")
        self.live_calls += 1

    def leave_live(self):
        self.live_calls = max(0, self.live_calls - 1)

