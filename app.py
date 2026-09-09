import os
import json
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from matching import rank_matches, score_match
from firebase_service import init_firebase, firebase_status, sync_sqlite_to_firestore
from excel_service import get_user_by_email as excel_user_by_email, get_user_by_id as excel_user_by_id, insert_user as excel_insert_user, update_user as excel_update_user, sync_sqlite_users as sync_excel_users

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "skillbridge.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-in-production")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _columns(db, table):
    return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(db, table, name, definition):
    if name not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            teach_skill TEXT NOT NULL,
            learn_skill TEXT NOT NULL,
            bio TEXT DEFAULT '',
            image TEXT DEFAULT '',
            location TEXT DEFAULT '',
            experience TEXT DEFAULT 'Intermediate',
            availability TEXT DEFAULT 'Available',
            last_seen TEXT,
            created_at TEXT NOT NULL
        )
    """)
    for name, definition in [
        ("experience", "TEXT DEFAULT 'Intermediate'"),
        ("availability", "TEXT DEFAULT 'Available'"),
        ("last_seen", "TEXT"),
    ]:
        _add_column(db, "users", name, definition)

    db.executescript("""
        CREATE TABLE IF NOT EXISTS connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(requester_id, recipient_id),
            FOREIGN KEY(requester_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(recipient_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read_at TEXT,
            FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(recipient_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS typing (
            user_id INTEGER NOT NULL,
            peer_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, peer_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(peer_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id INTEGER NOT NULL,
            learner_id INTEGER NOT NULL,
            skill TEXT NOT NULL,
            title TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'Scheduled',
            created_at TEXT NOT NULL,
            FOREIGN KEY(teacher_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(learner_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            link TEXT,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caller_id INTEGER NOT NULL,
            callee_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'ringing',
            caller_offer TEXT,
            callee_answer TEXT,
            caller_candidates TEXT DEFAULT '[]',
            callee_candidates TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            ended_at TEXT,
            FOREIGN KEY(caller_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(callee_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_messages_pair ON messages(sender_id, recipient_id, id);
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, id);
        CREATE INDEX IF NOT EXISTS idx_sessions_users ON sessions(teacher_id, learner_id, starts_at);
    """)
    db.commit()
    db.close()


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def row_to_dict(row):
    return dict(row) if row else None


def find_user_by_email(email):
    # Excel is the account/authentication source of truth.
    return excel_user_by_email(email)


def find_user_by_id(user_id):
    # Excel is the account/profile source of truth.
    return excel_user_by_id(user_id)


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    user = find_user_by_id(uid)
    if user:
        timestamp = now_iso()
        excel_update_user(uid, last_seen=timestamp)
        # Keep SQLite's relational tables in sync for chat/connections/etc.
        get_db().execute("UPDATE users SET last_seen=? WHERE id=?", (timestamp, uid))
        get_db().commit()
        user["last_seen"] = timestamp
    return user


def login_required():
    return current_user() is not None


@app.context_processor
def inject_user():
    user = current_user()
    unread = 0
    if user:
        unread = get_db().execute(
            "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0", (user["id"],)
        ).fetchone()["c"]
    return {"current_user": user, "unread_notifications": unread}


def create_notification(user_id, kind, title, body, link=None):
    db = get_db()
    db.execute(
        "INSERT INTO notifications(user_id,kind,title,body,link,created_at) VALUES(?,?,?,?,?,?)",
        (user_id, kind, title, body, link, now_iso()),
    )
    db.commit()


def connection_between(a, b):
    row = get_db().execute("""
        SELECT * FROM connections
        WHERE (requester_id=? AND recipient_id=?)
           OR (requester_id=? AND recipient_id=?)
        ORDER BY id DESC LIMIT 1
    """, (a, b, b, a)).fetchone()
    return row_to_dict(row)


def connected(a, b):
    c = connection_between(a, b)
    return bool(c and c["status"] == "accepted")


def search_users(query, viewer_id=None, experience=None, availability=None, mode=None, sort="relevance"):
    db = get_db()
    rows = [dict(r) for r in db.execute("SELECT * FROM users").fetchall()]
    viewer = find_user_by_id(viewer_id) if viewer_id else None
    q = (query or "").strip().lower()
    terms = [x for x in q.split() if x]
    related = {
        "python": ["django", "flask", "fastapi", "pandas", "machine learning", "data science"],
        "javascript": ["react", "node", "typescript", "next", "frontend"],
        "react": ["javascript", "next", "frontend", "ui"],
        "web development": ["html", "css", "javascript", "react", "django", "flask"],
        "data science": ["python", "pandas", "machine learning", "sql"],
        "machine learning": ["python", "data science", "pytorch", "tensorflow"],
        "ui/ux": ["figma", "ux", "ui", "design"],
        "aws": ["cloud", "devops", "docker"],
    }
    expanded = set(terms)
    for key, vals in related.items():
        if key in q or any(t == key for t in terms):
            expanded.update(vals)

    results = []
    for u in rows:
        if viewer_id and u["id"] == viewer_id:
            continue
        hay = " ".join([
            u.get("name",""), u.get("teach_skill",""), u.get("learn_skill",""), u.get("bio",""), u.get("location","")
        ]).lower()
        if experience and u.get("experience") != experience:
            continue
        if availability and u.get("availability") != availability:
            continue
        teach = (u.get("teach_skill") or "").lower()
        learn = (u.get("learn_skill") or "").lower()
        score = 0
        reasons = []
        if not q:
            score = 1
        if q and q in teach:
            score += 100; reasons.append("Teaches this skill")
        if q and q in learn:
            score += 75; reasons.append("Wants to learn this skill")
        if q and q in u.get("name","").lower():
            score += 70; reasons.append("Name matches")
        if q and q in u.get("bio","").lower():
            score += 40; reasons.append("Bio mentions it")
        if q and q in hay:
            score += 25
        for item in expanded:
            if item and item in teach:
                score += 32
                if item != q: reasons.append(f"Related skill: {item.title()}")
            if item and item in learn:
                score += 18
                if item != q: reasons.append(f"Learning related skill: {item.title()}")
        if q and not any([q in teach, q in learn, q in hay]) and score == 0:
            continue
        if mode == "teaching":
            if q:
                if q not in teach and not any(x in teach for x in expanded):
                    continue
            elif viewer and viewer.get("learn_skill"):
                if viewer["learn_skill"].strip().lower() not in teach:
                    continue
        if mode == "learning":
            if q:
                if q not in learn and not any(x in learn for x in expanded):
                    continue
            elif viewer and viewer.get("teach_skill"):
                if viewer["teach_skill"].strip().lower() not in learn:
                    continue
        results.append({**u, "match_score": score, "match_reasons": list(dict.fromkeys(reasons))[:3]})
    if sort == "name":
        results.sort(key=lambda x: x["name"].lower())
    elif sort == "recent":
        results.sort(key=lambda x: x.get("created_at",""), reverse=True)
    else:
        results.sort(key=lambda x: (x["match_score"], x.get("created_at","")), reverse=True)
    return results


# ---------------- Pages ----------------

@app.route("/")
def index():
    db = get_db()
    featured = [dict(r) for r in db.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 6").fetchall()]
    user_count = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    return render_template("index.html", featured=featured, user_count=user_count)


@app.route("/explore")
def explore():
    query = request.args.get("q", "").strip()
    users = search_users(
        query,
        viewer_id=session.get("user_id"),
        experience=request.args.get("experience") or None,
        availability=request.args.get("availability") or None,
        mode=request.args.get("mode") or None,
        sort=request.args.get("sort", "relevance"),
    )
    return render_template("explore.html", users=users, query=query)


@app.route("/how-it-works")
def how_it_works():
    return render_template("how.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/profile/<int:user_id>")
def profile(user_id):
    user = find_user_by_id(user_id)
    if not user:
        return render_template("404.html"), 404
    viewer = current_user()
    match = score_match(viewer, user) if viewer and viewer["id"] != user["id"] else None
    conn = connection_between(viewer["id"], user_id) if viewer and viewer["id"] != user_id else None
    return render_template("profile.html", profile_user=user, match=match, connection=conn,
                           is_connected=bool(conn and conn["status"]=="accepted"))


@app.route("/matches")
def matches():
    viewer = current_user()
    if not viewer:
        flash("Log in to see your AI-powered skill matches.", "error")
        return redirect(url_for("login"))
    all_users = [dict(r) for r in get_db().execute("SELECT * FROM users").fetchall()]
    ranked = rank_matches(viewer, all_users, limit=12)
    return render_template("matches.html", ranked=ranked, viewer=viewer)


@app.route("/connections")
def connections():
    user = current_user()
    if not user: return redirect(url_for("login"))
    db = get_db()
    incoming = [dict(r) for r in db.execute("""
        SELECT c.*, u.name, u.image, u.teach_skill, u.learn_skill FROM connections c
        JOIN users u ON u.id=c.requester_id WHERE c.recipient_id=? AND c.status='pending'
        ORDER BY c.id DESC
    """, (user["id"],)).fetchall()]
    sent = [dict(r) for r in db.execute("""
        SELECT c.*, u.name, u.image, u.teach_skill, u.learn_skill FROM connections c
        JOIN users u ON u.id=c.recipient_id WHERE c.requester_id=? AND c.status='pending'
        ORDER BY c.id DESC
    """, (user["id"],)).fetchall()]
    accepted = [dict(r) for r in db.execute("""
        SELECT c.*, u.id user_id, u.name, u.image, u.teach_skill, u.learn_skill FROM connections c
        JOIN users u ON u.id = CASE WHEN c.requester_id=? THEN c.recipient_id ELSE c.requester_id END
        WHERE (c.requester_id=? OR c.recipient_id=?) AND c.status='accepted'
        ORDER BY c.updated_at DESC
    """, (user["id"], user["id"], user["id"])).fetchall()]
    return render_template("connections.html", incoming=incoming, sent=sent, accepted=accepted)


@app.route("/chat")
@app.route("/chat/<int:user_id>")
def chat(user_id=None):
    user = current_user()
    if not user: return redirect(url_for("login"))
    peers = [dict(r) for r in get_db().execute("""
        SELECT u.* FROM users u
        JOIN connections c ON ((c.requester_id=u.id AND c.recipient_id=?) OR (c.recipient_id=u.id AND c.requester_id=?))
        WHERE c.status='accepted' ORDER BY u.name
    """, (user["id"], user["id"])).fetchall()]
    selected = find_user_by_id(user_id) if user_id else (peers[0] if peers else None)
    if selected and not connected(user["id"], selected["id"]):
        selected = None
    return render_template("chat.html", peers=peers, selected=selected)


@app.route("/sessions")
def sessions():
    user = current_user()
    if not user: return redirect(url_for("login"))
    db = get_db()
    rows = [dict(r) for r in db.execute("""
        SELECT s.*, t.name teacher_name, l.name learner_name
        FROM sessions s JOIN users t ON t.id=s.teacher_id JOIN users l ON l.id=s.learner_id
        WHERE s.teacher_id=? OR s.learner_id=? ORDER BY s.starts_at
    """, (user["id"], user["id"])).fetchall()]
    peers = [dict(r) for r in db.execute("""
        SELECT u.id,u.name,u.teach_skill FROM users u
        JOIN connections c ON ((c.requester_id=u.id AND c.recipient_id=?) OR (c.recipient_id=u.id AND c.requester_id=?))
        WHERE c.status='accepted' ORDER BY u.name
    """, (user["id"], user["id"])).fetchall()]
    return render_template("sessions.html", sessions=rows, peers=peers)


@app.route("/notifications")
def notifications():
    user = current_user()
    if not user: return redirect(url_for("login"))
    rows = [dict(r) for r in get_db().execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 80", (user["id"],)
    ).fetchall()]
    get_db().execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
    get_db().commit()
    return render_template("notifications.html", notifications=rows)


# ---------------- Auth ----------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name","").strip()
        email = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        teach_skill = request.form.get("teach_skill","").strip()
        learn_skill = request.form.get("learn_skill","").strip()
        bio = request.form.get("bio","").strip()
        location = request.form.get("location","").strip()
        experience = request.form.get("experience","Intermediate")
        availability = request.form.get("availability","Available")
        errors = []
        if len(name)<2: errors.append("Please enter your full name.")
        if "@" not in email or "." not in email: errors.append("Please enter a valid email address.")
        if len(password)<6: errors.append("Password must be at least 6 characters.")
        if not teach_skill: errors.append("Tell us a skill you can teach.")
        if not learn_skill: errors.append("Tell us a skill you want to learn.")
        if find_user_by_email(email): errors.append("An account with that email already exists.")
        if errors:
            for e in errors: flash(e,"error")
            return render_template("register.html", form=request.form)
        image_filename = ""
        file = request.files.get("image")
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(f"{email}_{file.filename}")
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            image_filename = filename
        db=get_db()
        password_hash = generate_password_hash(password)
        created = now_iso()
        cur=db.execute("""INSERT INTO users(name,email,password_hash,teach_skill,learn_skill,bio,image,location,experience,availability,last_seen,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (name,email,password_hash,teach_skill,learn_skill,bio,image_filename,location,
                        experience,availability,created,created))
        user_id = cur.lastrowid
        # Write the account to Excel before committing SQLite. If Excel fails,
        # the SQLite transaction is rolled back so the two stores don't diverge.
        try:
            excel_insert_user({
                "id": user_id, "name": name, "email": email,
                "password_hash": password_hash, "teach_skill": teach_skill,
                "learn_skill": learn_skill, "bio": bio, "image": image_filename,
                "location": location, "experience": experience,
                "availability": availability, "last_seen": created,
                "created_at": created
            })
        except Exception:
            db.rollback()
            app.logger.exception("Could not save account to Excel")
            flash("Could not save your account to the Excel database. Please try again.", "error")
            return render_template("register.html", form=request.form)
        db.commit()
        session["user_id"]=user_id
        flash(f"Welcome to SkillBridge, {name.split(' ')[0]}!","success")
        return redirect(url_for("index"))
    return render_template("register.html", form={})


@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        email=request.form.get("email","").strip().lower()
        password=request.form.get("password","")
        user=find_user_by_email(email)
        if not user or not check_password_hash(user["password_hash"],password):
            flash("Incorrect email or password.","error"); return render_template("login.html")
        timestamp = now_iso()
        session["user_id"]=user["id"]
        excel_update_user(user["id"], last_seen=timestamp)
        get_db().execute("UPDATE users SET last_seen=? WHERE id=?", (timestamp,user["id"])); get_db().commit()
        user["last_seen"] = timestamp
        flash(f"Welcome back, {user['name'].split(' ')[0]}!","success"); return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    # Clear the entire Flask session so no authenticated/session state survives logout.
    session.clear()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for("index"))


# ---------------- Connections ----------------

@app.post("/api/connections/request")
def connection_request():
    me=current_user()
    if not me: return jsonify(error="Login required"),401
    target_id=int(request.json.get("user_id",0))
    if target_id==me["id"]: return jsonify(error="You cannot connect with yourself."),400
    target=find_user_by_id(target_id)
    if not target: return jsonify(error="Profile not found."),404
    existing=connection_between(me["id"],target_id)
    if existing:
        if existing["status"]=="accepted": return jsonify(status="accepted")
        if existing["status"]=="pending": return jsonify(status="pending")
        # declined/rejected can be re-requested
        db=get_db(); db.execute("UPDATE connections SET requester_id=?, recipient_id=?, status='pending', updated_at=? WHERE id=?",
                                (me["id"],target_id,now_iso(),existing["id"])); db.commit()
    else:
        db=get_db(); db.execute("INSERT INTO connections(requester_id,recipient_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                                (me["id"],target_id,"pending",now_iso(),now_iso())); db.commit()
    create_notification(target_id,"connection","New connection request",f"{me['name']} wants to connect with you.",url_for("connections"))
    return jsonify(status="pending")


@app.post("/api/connections/<int:connection_id>/<action>")
def connection_action(connection_id, action):
    me=current_user()
    if not me: return jsonify(error="Login required"),401
    db=get_db(); c=db.execute("SELECT * FROM connections WHERE id=?", (connection_id,)).fetchone()
    if not c: return jsonify(error="Request not found."),404
    if action=="accept" and c["recipient_id"]==me["id"]:
        db.execute("UPDATE connections SET status='accepted',updated_at=? WHERE id=?", (now_iso(),connection_id)); db.commit()
        create_notification(c["requester_id"],"connection","Connection accepted",f"{me['name']} accepted your connection request.",url_for("profile",user_id=me["id"]))
        return jsonify(status="accepted")
    if action=="decline" and c["recipient_id"]==me["id"]:
        db.execute("UPDATE connections SET status='declined',updated_at=? WHERE id=?", (now_iso(),connection_id)); db.commit()
        return jsonify(status="declined")
    return jsonify(error="Not allowed."),403


# ---------------- Chat ----------------

@app.get("/api/messages/<int:user_id>")
def api_messages(user_id):
    me=current_user()
    if not me: return jsonify(error="Login required"),401
    if not connected(me["id"],user_id): return jsonify(error="You are not connected with this person."),403
    db=get_db()
    rows=db.execute("""
        SELECT m.*, u.name sender_name FROM messages m JOIN users u ON u.id=m.sender_id
        WHERE (sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?)
        ORDER BY m.id ASC LIMIT 300
    """,(me["id"],user_id,user_id,me["id"])).fetchall()
    db.execute("UPDATE messages SET read_at=? WHERE sender_id=? AND recipient_id=? AND read_at IS NULL",
               (now_iso(),user_id,me["id"])); db.commit()
    return jsonify(messages=[dict(r) for r in rows])


@app.post("/api/messages/<int:user_id>")
def api_send_message(user_id):
    me=current_user()
    if not me: return jsonify(error="Login required"),401
    if not connected(me["id"],user_id): return jsonify(error="Connect before messaging."),403
    body=(request.json.get("body") or "").strip()
    if not body or len(body)>5000: return jsonify(error="Message must be 1–5000 characters."),400
    db=get_db(); cur=db.execute("INSERT INTO messages(sender_id,recipient_id,body,created_at) VALUES(?,?,?,?)",
                                (me["id"],user_id,body,now_iso())); db.commit()
    create_notification(user_id,"message","New message",f"{me['name']}: {body[:80]}",url_for("chat",user_id=me["id"]))
    row=db.execute("SELECT m.*,u.name sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.id=?",(cur.lastrowid,)).fetchone()
    return jsonify(message=dict(row))


@app.post("/api/typing/<int:user_id>")
def api_typing(user_id):
    me=current_user()
    if not me or not connected(me["id"],user_id): return jsonify(ok=False),403
    db=get_db(); db.execute("INSERT OR REPLACE INTO typing(user_id,peer_id,updated_at) VALUES(?,?,?)",(me["id"],user_id,now_iso())); db.commit()
    return jsonify(ok=True)


@app.get("/api/typing/<int:user_id>")
def api_is_typing(user_id):
    me=current_user()
    if not me: return jsonify(typing=False)
    row=get_db().execute("SELECT updated_at FROM typing WHERE user_id=? AND peer_id=?",(user_id,me["id"])).fetchone()
    if not row: return jsonify(typing=False)
    try: active=datetime.fromisoformat(row["updated_at"].replace("Z","")) > datetime.utcnow()-timedelta(seconds=4)
    except ValueError: active=False
    return jsonify(typing=active)


# ---------------- Sessions ----------------

@app.post("/api/sessions")
def api_create_session():
    me=current_user()
    if not me: return jsonify(error="Login required"),401
    data=request.json or {}
    other_id=int(data.get("user_id",0)); target=find_user_by_id(other_id)
    if not target: return jsonify(error="Profile not found."),404
    if not connected(me["id"],other_id): return jsonify(error="You must be connected first."),403
    skill=(data.get("skill") or target["teach_skill"]).strip()
    title=(data.get("title") or f"{skill} learning session").strip()
    try:
        starts=str(data.get("starts_at","")).replace("Z","")
        dt=datetime.fromisoformat(starts)
        duration=int(data.get("duration_minutes",60))
        if dt <= datetime.now(): return jsonify(error="Choose a future date and time."),400
        if duration<15 or duration>240: raise ValueError
    except (ValueError, TypeError):
        return jsonify(error="Invalid date/time or duration."),400
    # viewer is learner by default; the other connected user is teacher when they teach the selected skill.
    if skill.lower() in target["teach_skill"].lower():
        teacher_id, learner_id=target["id"],me["id"]
    else:
        teacher_id, learner_id=me["id"],target["id"]
    db=get_db()
    cur=db.execute("""INSERT INTO sessions(teacher_id,learner_id,skill,title,starts_at,duration_minutes,status,created_at)
                      VALUES(?,?,?,?,?,?,?,?)""",(teacher_id,learner_id,skill,title,dt.isoformat(),duration,"Scheduled",now_iso()))
    db.commit()
    sid=cur.lastrowid
    other=teacher_id if learner_id==me["id"] else learner_id
    create_notification(other,"session","Learning session scheduled",f"{title} · {dt.strftime('%b %d, %I:%M %p')}",url_for("sessions"))
    return jsonify(id=sid, status="Scheduled", url=url_for("sessions"))


@app.post("/api/sessions/<int:sid>/status")
def session_status(sid):
    me=current_user()
    if not me: return jsonify(error="Login required"),401
    status=(request.json.get("status") or "").title()
    if status not in {"Scheduled","Completed","Cancelled"}: return jsonify(error="Invalid status"),400
    db=get_db(); row=db.execute("SELECT * FROM sessions WHERE id=?",(sid,)).fetchone()
    if not row or me["id"] not in (row["teacher_id"],row["learner_id"]): return jsonify(error="Not allowed"),403
    db.execute("UPDATE sessions SET status=? WHERE id=?",(status,sid)); db.commit()
    other=row["learner_id"] if me["id"]==row["teacher_id"] else row["teacher_id"]
    create_notification(other,"session",f"Session {status.lower()}",row["title"],url_for("sessions"))
    return jsonify(status=status)


# ---------------- Notifications ----------------

@app.get("/api/notifications")
def api_notifications():
    me=current_user()
    if not me: return jsonify(error="Login required"),401
    db=get_db()
    rows=[dict(r) for r in db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 30",(me["id"],)).fetchall()]
    unread=sum(1 for r in rows if not r["is_read"])
    return jsonify(notifications=rows,unread=unread)


@app.get("/api/users/<int:user_id>/status")
def api_user_status(user_id):
    me=current_user()
    if not me: return jsonify(error="Login required"),401
    u=find_user_by_id(user_id)
    if not u: return jsonify(error="Not found"),404
    online=False
    if u.get("last_seen"):
        try:
            online=datetime.fromisoformat(u["last_seen"].replace("Z","")) > datetime.utcnow()-timedelta(seconds=90)
        except ValueError: pass
    return jsonify(online=online,last_seen=u.get("last_seen"))

@app.get("/api/calls/incoming")
def incoming_calls():
    me=current_user()
    if not me: return jsonify(calls=[])
    rows=get_db().execute("""
        SELECT c.*,u.name caller_name FROM calls c JOIN users u ON u.id=c.caller_id
        WHERE c.callee_id=? AND c.status='ringing' ORDER BY c.id DESC LIMIT 3
    """,(me["id"],)).fetchall()
    return jsonify(calls=[dict(r) for r in rows])

# ---------------- WebRTC signaling ----------------

@app.post("/api/calls")
def create_call():
    me=current_user()
    if not me: return jsonify(error="Login required"),401
    callee_id=int((request.json or {}).get("callee_id",0))
    if not connected(me["id"],callee_id): return jsonify(error="You must be connected before calling."),403
    db=get_db()
    db.execute("UPDATE calls SET status='ended',ended_at=? WHERE caller_id=? AND callee_id=? AND status IN ('ringing','active')",
               (now_iso(),me["id"],callee_id))
    cur=db.execute("INSERT INTO calls(caller_id,callee_id,status,created_at) VALUES(?,?,?,?)",
                   (me["id"],callee_id,"ringing",now_iso()))
    db.commit()
    cid=cur.lastrowid
    create_notification(callee_id,"call","Incoming call",f"{me['name']} is calling you.",url_for("chat",user_id=me["id"]))
    return jsonify(call_id=cid)


@app.get("/api/calls/<int:call_id>")
def get_call(call_id):
    me=current_user()
    if not me: return jsonify(error="Login required"),401
    row=get_db().execute("SELECT * FROM calls WHERE id=?",(call_id,)).fetchone()
    if not row or me["id"] not in (row["caller_id"],row["callee_id"]): return jsonify(error="Not allowed"),403
    return jsonify(dict(row))


@app.post("/api/calls/<int:call_id>")
def update_call(call_id):
    me=current_user(); data=request.json or {}
    if not me: return jsonify(error="Login required"),401
    db=get_db(); row=db.execute("SELECT * FROM calls WHERE id=?",(call_id,)).fetchone()
    if not row or me["id"] not in (row["caller_id"],row["callee_id"]): return jsonify(error="Not allowed"),403
    allowed={"status","caller_offer","callee_answer"}
    sets=[]; vals=[]
    for k in allowed:
        if k in data:
            sets.append(f"{k}=?"); vals.append(json.dumps(data[k]) if k in {"caller_offer","callee_answer"} and isinstance(data[k],(dict,list)) else data[k])
    if "candidate" in data:
        col="caller_candidates" if me["id"]==row["caller_id"] else "callee_candidates"
        old=json.loads(row[col] or "[]"); old.append(data["candidate"]); sets.append(f"{col}=?"); vals.append(json.dumps(old))
    if sets:
        vals.append(call_id); db.execute(f"UPDATE calls SET {', '.join(sets)} WHERE id=?",vals); db.commit()
    return jsonify(ok=True)


# ---------------- Search API ----------------

@app.get("/api/search")
def api_search():
    q=request.args.get("q","").strip()
    users=search_users(q,viewer_id=session.get("user_id"))
    return jsonify([{
        "id":u["id"],"name":u["name"],"teach_skill":u["teach_skill"],"learn_skill":u["learn_skill"],
        "experience":u.get("experience"),"availability":u.get("availability"),
        "reasons":u.get("match_reasons",[])
    } for u in users[:10]])


@app.after_request
def sync_after_write(response):
    """Automatically mirror successful write requests to Firestore."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and response.status_code < 400:
        try:
            sync_sqlite_to_firestore(get_db())
        except Exception:
            # Cloud sync must never break an otherwise successful local request.
            app.logger.exception("Automatic Firebase sync failed")
    return response


# ---------------- Firebase Cloud ----------------

@app.get("/api/firebase/status")
def api_firebase_status():
    """Show Firebase connection status without exposing credentials."""
    status = firebase_status()
    return jsonify(status)


@app.post("/api/firebase/sync")
def api_firebase_sync():
    """Manually mirror the current local database to Firebase Firestore."""
    me = current_user()
    if not me:
        return jsonify(error="Login required"), 401
    try:
        result = sync_sqlite_to_firestore(get_db())
        if not result.get("configured"):
            return jsonify(error="Firebase is not configured", **result), 503
        return jsonify(result)
    except Exception:
        app.logger.exception("Firebase sync failed")
        return jsonify(error="Firebase sync failed. Check your Firebase credentials and Firestore setup."), 502



# ---------------- AI assistant + actions ----------------

AI_TOOLS = [
    {"type":"function","name":"find_people","description":"Find real SkillBridge users by skill, name, bio or related skill. Never invent users.",
     "parameters":{"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":8}},"required":["query"]}},
    {"type":"function","name":"connect_with_person","description":"Send a real connection request to a SkillBridge user. Use find_people first if needed.",
     "parameters":{"type":"object","properties":{"user_id":{"type":"integer"}},"required":["user_id"]}},
    {"type":"function","name":"send_message","description":"Send a message to a connected SkillBridge user.",
     "parameters":{"type":"object","properties":{"user_id":{"type":"integer"},"message":{"type":"string"}},"required":["user_id","message"]}},
    {"type":"function","name":"schedule_session","description":"Schedule a real learning session with a connected user.",
     "parameters":{"type":"object","properties":{"user_id":{"type":"integer"},"skill":{"type":"string"},"title":{"type":"string"},"starts_at":{"type":"string","description":"ISO local date/time"},"duration_minutes":{"type":"integer"}},"required":["user_id","skill","starts_at","duration_minutes"]}},
    {"type":"function","name":"open_profile","description":"Open a real user's profile.",
     "parameters":{"type":"object","properties":{"user_id":{"type":"integer"}},"required":["user_id"]}},
    {"type":"function","name":"open_page","description":"Navigate the user to a SkillBridge page.",
     "parameters":{"type":"object","properties":{"page":{"type":"string","enum":["explore","connections","chat","sessions","notifications","my_profile"]}},"required":["page"]}},
]


def ai_tool_result(name,args,me):
    db=get_db()
    if name=="find_people":
        people=search_users(args.get("query",""),viewer_id=me["id"])[:int(args.get("limit",6))]
        return {"people":[{"id":p["id"],"name":p["name"],"teach_skill":p["teach_skill"],"learn_skill":p["learn_skill"],
                           "bio":p["bio"],"experience":p.get("experience"),"availability":p.get("availability"),
                           "reasons":p.get("match_reasons",[])} for p in people]}
    if name=="connect_with_person":
        target=find_user_by_id(int(args["user_id"]))
        if not target: return {"error":"User not found."}
        if target["id"]==me["id"]: return {"error":"Cannot connect to yourself."}
        existing=connection_between(me["id"],target["id"])
        if existing and existing["status"]=="accepted": return {"status":"accepted","user":target["name"]}
        if not existing:
            db.execute("INSERT INTO connections(requester_id,recipient_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                       (me["id"],target["id"],"pending",now_iso(),now_iso())); db.commit()
            create_notification(target["id"],"connection","New connection request",f"{me['name']} wants to connect with you.",url_for("connections"))
        return {"status":"pending","user":target["name"],"action":"connection_request_sent"}
    if name=="send_message":
        uid=int(args["user_id"])
        if not connected(me["id"],uid): return {"error":"You must be connected before messaging."}
        body=args.get("message","").strip()
        if not body: return {"error":"Message is empty."}
        db.execute("INSERT INTO messages(sender_id,recipient_id,body,created_at) VALUES(?,?,?,?)",(me["id"],uid,body,now_iso())); db.commit()
        create_notification(uid,"message","New message",f"{me['name']}: {body[:80]}",url_for("chat",user_id=me["id"]))
        return {"status":"sent","action":"open_chat","user_id":uid}
    if name=="schedule_session":
        uid=int(args["user_id"])
        if not connected(me["id"],uid): return {"error":"You must be connected before scheduling."}
        try:
            dt=datetime.fromisoformat(args["starts_at"].replace("Z","")); duration=int(args["duration_minutes"])
            if dt <= datetime.now() or duration<15 or duration>240: raise ValueError
        except Exception: return {"error":"Invalid future date/time or duration."}
        target=find_user_by_id(uid); skill=args["skill"]; title=args.get("title") or f"{skill} learning session"
        if skill.lower() in target["teach_skill"].lower(): teacher,learner=uid,me["id"]
        else: teacher,learner=me["id"],uid
        cur=db.execute("""INSERT INTO sessions(teacher_id,learner_id,skill,title,starts_at,duration_minutes,status,created_at)
                          VALUES(?,?,?,?,?,?,?,?)""",(teacher,learner,skill,title,dt.isoformat(),duration,"Scheduled",now_iso())); db.commit()
        create_notification(uid,"session","Learning session scheduled",f"{title} · {dt.strftime('%b %d, %I:%M %p')}",url_for("sessions"))
        return {"status":"scheduled","session_id":cur.lastrowid,"action":"open_sessions"}
    if name=="open_profile":
        uid=int(args["user_id"])
        if not find_user_by_id(uid): return {"error":"Profile not found."}
        return {"action":"navigate","url":url_for("profile",user_id=uid)}
    if name=="open_page":
        page=args["page"]
        endpoints={"explore":"explore","connections":"connections","chat":"chat","sessions":"sessions","notifications":"notifications","my_profile":"profile"}
        if page=="my_profile": return {"action":"navigate","url":url_for("profile",user_id=me["id"])}
        return {"action":"navigate","url":url_for(endpoints[page])}
    return {"error":"Unknown action."}


@app.post("/api/ai")
def api_ai():
    me=current_user()
    if not me: return jsonify(error="Login required"),401
    if OpenAI is None or not os.environ.get("OPENAI_API_KEY"):
        return jsonify(error="AI is not configured. Set OPENAI_API_KEY on the Flask server."),503
    data=request.json or {}; message=(data.get("message") or "").strip()
    history=data.get("history") or []
    if not message: return jsonify(error="Please enter a message."),400
    client=OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    profile=f"""Current user: {me['name']} (id {me['id']}).
Teaches: {me['teach_skill']}; wants to learn: {me['learn_skill']}; experience: {me.get('experience')}; availability: {me.get('availability')}.
Today: {datetime.now().strftime('%Y-%m-%d %H:%M')}."""
    instructions="""You are SkillBridge AI, an assistant inside a real skill-sharing application.
Be concise, friendly and practical. Use tools for real app actions. Never invent users, IDs, availability, messages or session confirmations.
If the user asks who can teach something, call find_people. If they ask to connect, message or schedule, use the appropriate tool after identifying a real person. You can explain learning paths without tools.
Do not claim an action succeeded unless the tool result says it did."""
    inputs=[{"role":"system","content":instructions+"\n"+profile}]
    for h in history[-12:]:
        if h.get("role") in ("user","assistant"):
            inputs.append({"role":h["role"],"content":str(h.get("content",""))})
    inputs.append({"role":"user","content":message})
    actions=[]
    try:
        response=client.responses.create(model=os.environ.get("OPENAI_MODEL","gpt-5.6-luna"),instructions=instructions,input=inputs[1:],tools=AI_TOOLS,tool_choice="auto")
        for _ in range(5):
            calls=[x for x in response.output if getattr(x,"type",None)=="function_call"]
            if not calls: break
            tool_outputs=[]
            for call in calls:
                args=json.loads(call.arguments or "{}")
                result=ai_tool_result(call.name,args,me)
                if isinstance(result,dict) and result.get("action"):
                    actions.append(result)
                tool_outputs.append({"type":"function_call_output","call_id":call.call_id,"output":json.dumps(result)})
            response=client.responses.create(model=os.environ.get("OPENAI_MODEL","gpt-5.6-luna"),instructions=instructions,
                                             input=tool_outputs,tools=AI_TOOLS,tool_choice="auto")
        return jsonify(reply=response.output_text or "I completed that action.",actions=actions,configured=True)
    except Exception:
        app.logger.exception("AI request failed")
        return jsonify(error="The AI assistant is temporarily unavailable. Please try again."),502


@app.errorhandler(413)
def too_large(e):
    return render_template("404.html", message="That upload is too large. Maximum size is 4 MB."),413


init_db()

# Excel is the account database. Create it and import any pre-existing local
# SQLite accounts so IDs remain compatible with SkillBridge's relational data.
try:
    with app.app_context():
        sync_excel_users(get_db())
except Exception:
    app.logger.exception("Excel database initialization/sync skipped")

# If Firebase credentials are present, create the client and make an initial
# cloud copy. The app still works locally when Firebase is not configured.
try:
    init_firebase()
    with app.app_context():
        sync_sqlite_to_firestore(get_db())
except Exception:
    app.logger.exception("Firebase initialization/sync skipped")

if __name__ == "__main__":
    app.run(debug=True)
