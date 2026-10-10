"""
storage.py — where uploaded photos/videos go. With SUPABASE_URL and
SUPABASE_SERVICE_KEY set they go to a public Supabase Storage bucket;
otherwise to a local uploads/ folder (Render's disk is wiped on every deploy,
so the local folder is only for trying things on your computer).

File names are random UUIDs, so a file can't be found without its link.
"""

import json
import os
import urllib.error
import urllib.request

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
BUCKET = os.environ.get("SUPABASE_BUCKET", "mixer-media")
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")


def _request(method, path, body=None, content_type="application/json"):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/storage/v1/{path}", data=body, method=method,
        headers={"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                 "apikey": SUPABASE_SERVICE_KEY,
                 "Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def init_storage():
    if not USE_SUPABASE:
        os.makedirs(LOCAL_DIR, exist_ok=True)
        return
    body = json.dumps({"id": BUCKET, "name": BUCKET, "public": True}).encode()
    try:
        _request("POST", "bucket", body)
    except urllib.error.HTTPError as e:
        # Already exists → Supabase answers 400/409; anything else is real.
        if e.code not in (400, 409):
            raise


def save(key, data, content_type):
    if USE_SUPABASE:
        _request("POST", f"object/{BUCKET}/{key}", data, content_type)
    else:
        with open(os.path.join(LOCAL_DIR, key), "wb") as f:
            f.write(data)


def read(key):
    """The file's bytes (used for the admin's download-all zip)."""
    if USE_SUPABASE:
        return _request("GET", f"object/{BUCKET}/{key}")
    with open(os.path.join(LOCAL_DIR, key), "rb") as f:
        return f.read()


def delete(key):
    try:
        if USE_SUPABASE:
            _request("DELETE", f"object/{BUCKET}",
                     json.dumps({"prefixes": [key]}).encode())
        else:
            os.remove(os.path.join(LOCAL_DIR, key))
    except (OSError, urllib.error.URLError):
        pass  # a leftover file isn't worth failing a review over


def url(key):
    if not key:
        return None
    if USE_SUPABASE:
        return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{key}"
    return f"/media/{key}"
