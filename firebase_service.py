"""Firebase Cloud Firestore integration for SkillBridge.

The Flask app keeps SQLite as its local application database and mirrors
application data (excluding password hashes) to Firestore when Firebase is
configured. This makes the existing app easy to run locally while giving it a
cloud copy of profiles, connections, messages, sessions, notifications and
call state.
"""

import json
import os
from datetime import datetime

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:  # Firebase is optional until configured.
    firebase_admin = None
    credentials = None
    firestore = None


FIREBASE_COLLECTIONS = {
    "users": "users",
    "connections": "connections",
    "messages": "messages",
    "sessions": "sessions",
    "notifications": "notifications",
    "calls": "calls",
}


def _credential_from_environment():
    """Load Firebase credentials from a JSON file or JSON environment value."""
    if not credentials:
        return None

    raw_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
    if raw_json:
        return credentials.Certificate(json.loads(raw_json))

    path = os.environ.get("FIREBASE_CREDENTIALS", "").strip()
    if path:
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(__file__), path)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Firebase credentials file not found: {path}")
        return credentials.Certificate(path)

    return None


def init_firebase():
    """Initialize Firebase once. Returns a Firestore client or None."""
    if firebase_admin is None:
        return None
    if firebase_admin._apps:
        return firestore.client()

    cred = _credential_from_environment()
    if cred is None:
        return None

    options = {}
    bucket = os.environ.get("FIREBASE_STORAGE_BUCKET", "").strip()
    if bucket:
        options["storageBucket"] = bucket

    firebase_admin.initialize_app(cred, options)
    return firestore.client()


def firebase_status():
    """Return a safe, user-facing Firebase configuration status."""
    if firebase_admin is None:
        return {"configured": False, "reason": "firebase-admin is not installed"}
    try:
        client = init_firebase()
        if client is None:
            return {"configured": False, "reason": "Firebase credentials are not configured"}
        return {"configured": True, "project_id": os.environ.get("FIREBASE_PROJECT_ID", "configured")}
    except Exception as exc:
        return {"configured": False, "reason": str(exc)}


def _clean_value(value):
    """Make SQLite values Firestore-friendly."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def sync_sqlite_to_firestore(db):
    """Mirror the current SQLite state to Firestore.

    Password hashes are deliberately never uploaded. IDs remain stable by
    using the SQLite primary key as the Firestore document ID.
    """
    client = init_firebase()
    if client is None:
        return {"configured": False, "synced": False}

    counts = {}
    for table, collection in FIREBASE_COLLECTIONS.items():
        rows = db.execute(f"SELECT * FROM {table}").fetchall()
        count = 0
        for row in rows:
            data = dict(row)
            data.pop("password_hash", None)
            data = {k: _clean_value(v) for k, v in data.items()}
            doc_id = str(data.pop("id"))
            client.collection(collection).document(doc_id).set(data, merge=True)
            count += 1
        counts[collection] = count

    return {"configured": True, "synced": True, "counts": counts}
