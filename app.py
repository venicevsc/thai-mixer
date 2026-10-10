"""
app.py — the mixer web app. Guests scan the QR, type a name, and play:
bingo (find someone who…), team-up challenges and performances, with
photo/video proof. Admins approve media before it hits the projector.

Pages:
  /         join (or straight to /play if already joined)
  /play     the player's phone view
  /screen   projector: live photo/video feed + leaderboard
  /admin    review queue (password: ADMIN_PASSWORD)
  /qr       printable QR code that points at this site
"""

import json
import re
import os
import secrets
import tempfile
import urllib.error
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

from flask import (Flask, abort, g, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

import game
import db
import storage

MAX_UPLOAD_MB = 50  # Supabase's free tier caps each file at 50 MB
MAX_VIDEO_SECONDS = 30


def _secret_key():
    """FLASK_SECRET_KEY if set, else a random key saved next to the app so
    logins survive restarts. Never a fixed default: anyone who knew it could
    forge an admin login."""
    if os.environ.get("FLASK_SECRET_KEY"):
        return os.environ["FLASK_SECRET_KEY"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        with open(path, "w") as f:
            f.write(key)
        return key


app = Flask(__name__)
# Hosts put a proxy in front of the app; this makes request.host_url (used
# for the QR code) the real public https address.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = _secret_key()
app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    SESSION_COOKIE_SAMESITE="Lax",
    # https-only cookies once hosted; plain http is fine on your own computer.
    SESSION_COOKIE_SECURE=(os.environ.get("COOKIE_SECURE") or ("1" if db.USE_PG else "0")) == "1",
)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

ROLES = {"phi": "พี่", "nong": "น้อง"}
ROLE_HINTS = {"phi": "upperclassman", "nong": "new member"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}
VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm"}
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
              "image/heic": "heic", "image/heif": "heif", "video/mp4": "mp4",
              "video/quicktime": "mov", "video/webm": "webm"}

db.init_db()
storage.init_storage()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_player():
    pid = session.get("player_id")
    if not pid:
        return None
    return db.query("SELECT * FROM mixer_players WHERE id = ?", (pid,), one=True)


def is_admin():
    return session.get("admin") is True


def cfg():
    """The current activities (admin edits or content.py defaults), loaded
    once per request."""
    if "game" not in g:
        g.game = game.current()
    return g.game


# ---------- scoring ----------

BINGO_LINES = ([[r * 5 + c for c in range(5)] for r in range(5)] +
               [[r * 5 + c for r in range(5)] for c in range(5)] +
               [[i * 6 for i in range(5)], [(i + 1) * 4 for i in range(5)]])


def score(done_ids):
    """Points for a set of completed (non-rejected) activity ids."""
    points = sum(cfg().ACTIVITIES[a]["points"] for a in done_ids if a in cfg().ACTIVITIES)
    done_squares = {i for i, sq in enumerate(cfg().BINGO)
                    if sq["id"] == "free" or sq["id"] in done_ids}
    lines = sum(1 for line in BINGO_LINES if all(i in done_squares for i in line))
    return points + lines * cfg().BINGO_LINE_BONUS, lines


def leaderboard(limit=None):
    players = db.query("SELECT id, name, role FROM mixer_players")
    rows = db.query("SELECT player_id, activity_id FROM mixer_submissions WHERE status <> 'rejected'")
    done = {}
    for r in rows:
        done.setdefault(r["player_id"], set()).add(r["activity_id"])
    board = []
    for p in players:
        pts, lines = score(done.get(p["id"], set()))
        board.append({"name": p["name"], "role": ROLES.get(p["role"], ""),
                      "points": pts, "lines": lines, "id": p["id"]})
    board.sort(key=lambda b: (-b["points"], b["name"].lower()))
    return board[:limit] if limit else board


# ---------- player pages ----------

@app.route("/")
def join_page():
    if current_player():
        return redirect(url_for("play"))
    return render_template("join.html", event=cfg().EVENT_NAME, roles=ROLES, hints=ROLE_HINTS, error=None)


@app.post("/join")
def join():
    name = " ".join(request.form.get("name", "").split())[:40]
    role = request.form.get("role", "")
    if not name:
        error = "Type your name so people can find you."
    elif role not in ROLES:
        error = "Pick one: พี่ or น้อง."
    elif db.query("SELECT 1 FROM mixer_players WHERE name_key = ?", (name.lower(),), one=True):
        error = f"Someone already joined as “{name}”. Add your last initial or nickname."
    else:
        error = None
    if error:
        return render_template("join.html", event=cfg().EVENT_NAME, roles=ROLES, hints=ROLE_HINTS,
                               error=error, name=name, role=role), 400

    pid = str(uuid.uuid4())
    db.query("INSERT INTO mixer_players (id, name, name_key, role, created_at) VALUES (?, ?, ?, ?, ?)",
             (pid, name, name.lower(), role, now()))
    session.permanent = True
    session["player_id"] = pid
    return redirect(url_for("play"))


@app.post("/leave")
def leave():
    session.pop("player_id", None)
    return redirect(url_for("join_page"))


@app.route("/play")
def play():
    player = current_player()
    if not player:
        return redirect(url_for("join_page"))
    return render_template(
        "play.html", event=cfg().EVENT_NAME, player=player,
        role_label=ROLES.get(player["role"], ""),
        game=game_payload())


def game_payload():
    """Activities as the phone page needs them (re-sent with every /api/me
    poll so admin edits show up without a reload)."""
    return {"event": cfg().EVENT_NAME, "bingo": cfg().BINGO, "challenges": cfg().CHALLENGES,
            "performances": cfg().PERFORMANCES, "lineBonus": cfg().BINGO_LINE_BONUS,
            "maxUploadMb": MAX_UPLOAD_MB, "maxVideoSeconds": MAX_VIDEO_SECONDS}


@app.route("/api/me")
def api_me():
    player = current_player()
    if not player:
        return jsonify(error="not_joined"), 401
    subs = db.query("SELECT * FROM mixer_submissions WHERE player_id = ?", (player["id"],))
    pts, lines = score({s["activity_id"] for s in subs if s["status"] != "rejected"})
    board = leaderboard()
    rank = next((i + 1 for i, b in enumerate(board) if b["id"] == player["id"]), None)
    return jsonify(
        game=game_payload(), points=pts, lines=lines, rank=rank, players=len(board),
        submissions={s["activity_id"]: {"status": s["status"], "note": s["note"],
                                        "mediaUrl": storage.url(s["media_key"]),
                                        "mediaType": s["media_type"]} for s in subs})


@app.post("/api/submit")
def api_submit():
    player = current_player()
    if not player:
        return jsonify(error="Your session expired — rejoin from the QR code."), 401
    activity = cfg().ACTIVITIES.get(request.form.get("activity_id", ""))
    if not activity:
        return jsonify(error="That activity doesn't exist anymore."), 400

    existing = db.query("SELECT * FROM mixer_submissions WHERE player_id = ? AND activity_id = ?",
                        (player["id"], activity["id"]), one=True)
    if existing and existing["status"] == "approved":
        return jsonify(error="Already approved — nice!"), 400

    note = " ".join(request.form.get("note", "").split())[:80]
    upload = request.files.get("media")
    proof = activity["proof"]
    if proof == "name" and not note:
        return jsonify(error="Type the name of the person you found."), 400
    if proof in ("photo", "video", "either") and not upload:
        return jsonify(error="Add a photo or video as proof."), 400

    media_key = media_type = None
    if upload:
        ctype = (upload.mimetype or "").lower()
        kind = "image" if ctype in IMAGE_TYPES else "video" if ctype in VIDEO_TYPES else None
        if kind is None:
            return jsonify(error="That file type isn't supported — use a normal photo or video."), 400
        if proof == "photo" and kind != "image":
            return jsonify(error="This one needs a photo."), 400
        if proof == "video" and kind != "video":
            return jsonify(error="This one needs a video."), 400
        data = upload.read()
        if not data:
            return jsonify(error="The file was empty — try again."), 400
        media_key = f"{uuid.uuid4()}.{EXTENSIONS[ctype]}"
        media_type = kind
        storage.save(media_key, data, ctype)

    status = "pending" if media_key else "done"
    if existing:
        if existing["media_key"]:
            storage.delete(existing["media_key"])
        db.query("""UPDATE mixer_submissions SET note = ?, media_key = ?, media_type = ?,
                    status = ?, created_at = ?, reviewed_at = NULL WHERE id = ?""",
                 (note, media_key, media_type, status, now(), existing["id"]))
    else:
        db.query("""INSERT INTO mixer_submissions
                    (id, player_id, activity_id, note, media_key, media_type, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (str(uuid.uuid4()), player["id"], activity["id"], note,
                  media_key, media_type, status, now()))
    return jsonify(ok=True, status=status)


@app.post("/api/undo")
def api_undo():
    player = current_player()
    if not player:
        return jsonify(error="not_joined"), 401
    activity_id = request.form.get("activity_id", "")
    sub = db.query("SELECT * FROM mixer_submissions WHERE player_id = ? AND activity_id = ?",
                   (player["id"], activity_id), one=True)
    if sub:
        if sub["media_key"]:
            storage.delete(sub["media_key"])
        db.query("DELETE FROM mixer_submissions WHERE id = ?", (sub["id"],))
    return jsonify(ok=True)


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f"That file is over {MAX_UPLOAD_MB} MB — record a shorter clip."), 413


# ---------- projector ----------

@app.route("/screen")
def screen():
    return render_template("screen.html", event=cfg().EVENT_NAME)


@app.route("/api/screen")
def api_screen():
    feed = db.query("""
        SELECT s.id, s.activity_id, s.note, s.media_key, s.media_type, s.reviewed_at, p.name
        FROM mixer_submissions s JOIN mixer_players p ON p.id = s.player_id
        WHERE s.status = 'approved' AND s.media_key IS NOT NULL
        ORDER BY s.reviewed_at DESC LIMIT 24""")
    return jsonify(
        feed=[{"id": f["id"], "name": f["name"], "note": f["note"],
               "title": cfg().ACTIVITIES.get(f["activity_id"], {}).get("title", ""),
               "url": storage.url(f["media_key"]), "type": f["media_type"]} for f in feed],
        leaderboard=[{k: b[k] for k in ("name", "role", "points", "lines")}
                     for b in leaderboard(10)])


@app.route("/qr")
def qr():
    return render_template("qr.html", event=cfg().EVENT_NAME, url=request.host_url)


@app.route("/media/<path:key>")
def media(key):
    if storage.USE_SUPABASE:
        abort(404)
    return send_from_directory(storage.LOCAL_DIR, key)


# ---------- admin ----------

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if ADMIN_PASSWORD and secrets.compare_digest(
                request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            session["admin"] = True
            return redirect(url_for("admin"))
        return render_template("admin_login.html", error="Wrong password.",
                               configured=bool(ADMIN_PASSWORD)), 401
    if not is_admin():
        return render_template("admin_login.html", error=None, configured=bool(ADMIN_PASSWORD))

    def rows(status, limit):
        subs = db.query(f"""
            SELECT s.*, p.name FROM mixer_submissions s
            JOIN mixer_players p ON p.id = s.player_id
            WHERE s.status = ? AND s.media_key IS NOT NULL
            ORDER BY s.created_at {'ASC' if status == 'pending' else 'DESC'} LIMIT {int(limit)}""",
                        (status,))
        for s in subs:
            act = cfg().ACTIVITIES.get(s["activity_id"], {})
            s["title"], s["points"] = act.get("title", s["activity_id"]), act.get("points", 0)
            s["url"] = storage.url(s["media_key"])
        return subs

    return render_template("admin.html", pending=rows("pending", 50),
                           approved=rows("approved", 60), hidden=rows("hidden", 60),
                           board=leaderboard())


@app.post("/admin/review")
def admin_review():
    if not is_admin():
        abort(403)
    sub = db.query("SELECT * FROM mixer_submissions WHERE id = ?", (request.form.get("id", ""),), one=True)
    action = request.form.get("action")
    if sub and action == "approve":
        db.query("UPDATE mixer_submissions SET status = 'approved', reviewed_at = ? WHERE id = ?",
                 (now(), sub["id"]))
    elif sub and action == "reject":
        # Rejecting removes the points and the file; the player can resubmit.
        if sub["media_key"]:
            storage.delete(sub["media_key"])
        db.query("""UPDATE mixer_submissions SET status = 'rejected', media_key = NULL,
                    reviewed_at = ? WHERE id = ?""", (now(), sub["id"]))
    elif sub and action == "hide":
        # Off the projector, but the player keeps the points.
        db.query("UPDATE mixer_submissions SET status = 'hidden' WHERE id = ?", (sub["id"],))
    elif sub and action == "caption":
        note = " ".join(request.form.get("note", "").split())[:80]
        db.query("UPDATE mixer_submissions SET note = ? WHERE id = ?", (note, sub["id"]))
    if sub and request.form.get("back") == "player":
        return redirect(url_for("admin_player", player_id=sub["player_id"]))
    return redirect(url_for("admin"))


@app.route("/admin/player/<player_id>")
def admin_player(player_id):
    """Everything one player has submitted, so invalid entries (including
    bingo squares that never go through the approval queue) can be removed."""
    if not is_admin():
        return redirect(url_for("admin"))
    player = db.query("SELECT * FROM mixer_players WHERE id = ?", (player_id,), one=True)
    if not player:
        abort(404)
    subs = db.query("SELECT * FROM mixer_submissions WHERE player_id = ? ORDER BY created_at DESC",
                    (player_id,))
    acts = cfg().ACTIVITIES
    kinds = {"bingo": "Bingo", "challenge": "Team up", "perform": "Perform"}
    for s in subs:
        act = acts.get(s["activity_id"])
        s["title"] = act["title"] if act else "(deleted activity)"
        s["kind"] = kinds.get(act["kind"], "") if act else ""
        s["points"] = act["points"] if act else 0
        s["url"] = storage.url(s["media_key"])
    pts, lines = score({s["activity_id"] for s in subs if s["status"] != "rejected"})
    return render_template("admin_player.html", player=player, subs=subs, points=pts, lines=lines,
                           role=ROLES.get(player["role"], ""))


@app.post("/admin/player/<player_id>/delete")
def admin_player_delete(player_id):
    """Remove a player entirely (e.g. a joke name), with their files."""
    if not is_admin():
        abort(403)
    for s in db.query("SELECT media_key FROM mixer_submissions WHERE player_id = ? AND media_key IS NOT NULL",
                      (player_id,)):
        storage.delete(s["media_key"])
    db.query("DELETE FROM mixer_submissions WHERE player_id = ?", (player_id,))
    db.query("DELETE FROM mixer_players WHERE id = ?", (player_id,))
    return redirect(url_for("admin"))


@app.route("/admin/photos.zip")
def admin_photos_zip():
    """Every photo/video that wasn't rejected, in one zip with readable
    names: '012 Ploy - Team up with a พี่ for a wai selfie.jpg'."""
    if not is_admin():
        return redirect(url_for("admin"))
    subs = db.query("""
        SELECT s.activity_id, s.media_key, p.name FROM mixer_submissions s
        JOIN mixer_players p ON p.id = s.player_id
        WHERE s.media_key IS NOT NULL AND s.status <> 'rejected'
        ORDER BY s.created_at""")
    acts = cfg().ACTIVITIES
    # Spools to disk past 50 MB so a pile of videos doesn't fill memory.
    buf = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # photos/videos are already compressed
        for i, s in enumerate(subs, 1):
            title = acts.get(s["activity_id"], {}).get("title", s["activity_id"])
            stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", f"{s['name']} - {title}")[:120].strip(" .")
            ext = s["media_key"].rsplit(".", 1)[-1]
            try:
                zf.writestr(f"{i:03d} {stem}.{ext}", storage.read(s["media_key"]))
            except (OSError, urllib.error.URLError):
                continue  # file already gone; skip it rather than fail the whole download
    buf.seek(0)
    stamp = datetime.now().strftime("%Y-%m-%d")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"mixer-photos-{stamp}.zip")


@app.post("/admin/reset")
def admin_reset():
    """Start over: 'scores' clears every submission and file but keeps the
    players; 'everything' removes the players too. Activities are untouched."""
    if not is_admin():
        abort(403)
    what = request.form.get("what")
    if request.form.get("confirm", "").strip().upper() != "RESET" or what not in ("scores", "everything"):
        return redirect(url_for("admin", reset_error=1) + "#reset")
    for s in db.query("SELECT media_key FROM mixer_submissions WHERE media_key IS NOT NULL"):
        storage.delete(s["media_key"])
    db.query("DELETE FROM mixer_submissions")
    if what == "everything":
        db.query("DELETE FROM mixer_players")
    return redirect(url_for("admin", reset_done=what))


@app.route("/admin/content", methods=["GET", "POST"])
def admin_content():
    if not is_admin():
        return redirect(url_for("admin"))
    data, error, saved = game.raw(), None, False
    if request.method == "POST":
        try:
            submitted = json.loads(request.form.get("data", ""))
        except ValueError:
            submitted = None
        if not isinstance(submitted, dict):
            error = "Couldn't read the form — try again."
        else:
            try:
                game.save(submitted)
                data, saved = game.raw(), True
            except ValueError as e:
                error, data = str(e), {**data, **submitted}  # keep their edits on screen
    return render_template("admin_content.html", data=data, proofs=game.PROOFS,
                           error=error, saved=saved), (400 if error else 200)


@app.post("/admin/content/reset")
def admin_content_reset():
    if not is_admin():
        abort(403)
    game.reset()
    return redirect(url_for("admin_content"))


@app.post("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)  # set FLASK_DEBUG=1 for auto-reload while editing
