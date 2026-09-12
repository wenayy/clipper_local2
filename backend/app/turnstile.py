"""Cloudflare Turnstile verification.

Stops automated abuse of job submission without punishing real users.
Turnstile is invisible for most visitors — only suspicious traffic sees
a challenge. This matters here because every submitted job costs real
money (Deepgram transcription, LLM analysis), and yt-dlp requests from
a single IP that look automated get the whole server's access throttled.

Verification is OPTIONAL: when TURNSTILE_SECRET_KEY is not set, every
request passes. Local development never needs it, and a fresh deploy
works immediately without configuring Cloudflare first.
"""

import os
import httpx

TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET_KEY", "")
VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def is_enabled() -> bool:
    return bool(TURNSTILE_SECRET)


def verify(token: str, remote_ip: str = None) -> bool:
    """Returns True if the token is valid or Turnstile is not configured."""
    if not TURNSTILE_SECRET:
        return True
    if not token:
        return False

    payload = {"secret": TURNSTILE_SECRET, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        resp = httpx.post(VERIFY_URL, data=payload, timeout=10)
        return resp.json().get("success", False)
    except Exception:
        # Cloudflare is down or unreachable. Let the request through rather
        # than blocking every real user because of someone else's outage.
        return True
