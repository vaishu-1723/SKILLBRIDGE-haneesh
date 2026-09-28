import os
import firebase_admin
from firebase_admin import credentials, firestore

_firestore_client = None
_initialized = False

def init_firebase():
    """Initializes Firebase Admin SDK using serviceAccountKey.json."""
    global _firestore_client, _initialized
    if _initialized and _firestore_client:
        return _firestore_client

    key_path = os.path.join(os.path.dirname(__file__), "serviceAccountKey.json")
    if not os.path.exists(key_path):
        print(f"[Firebase] Warning: serviceAccountKey.json not found at {key_path}")
        return None

    try:
        cred = credentials.Certificate(key_path)
        firebase_admin.initialize_app(cred)
        _firestore_client = firestore.client()
        _initialized = True
        print("[Firebase] Connected successfully to Cloud Firestore!")
        return _firestore_client
    except Exception as e:
        print(f"[Firebase] Error during initialization: {e}")
        return None


def get_firestore_db():
    global _firestore_client
    if not _firestore_client:
        return init_firebase()
    return _firestore_client


def firebase_status():
    db = get_firestore_db()
    return {
        "configured": db is not None,
        "database": "Cloud Firestore" if db else "None"
    }


def sync_sqlite_to_firestore(sqlite_db):
    """Syncs SQLite users, sessions, and messages directly to Firestore."""
    fdb = get_firestore_db()
    if not fdb:
        return {"configured": False, "message": "Firebase not connected"}

    try:
        # Sync Users
        users = sqlite_db.execute("SELECT * FROM users").fetchall()
        for u in users:
            u_dict = dict(u)
            email = u_dict.get("email")
            if email:
                fdb.collection("users").document(email).set(u_dict, merge=True)

        # Sync Sessions
        sessions = sqlite_db.execute("SELECT * FROM sessions").fetchall()
        for s in sessions:
            s_dict = dict(s)
            fdb.collection("sessions").document(str(s_dict["id"])).set(s_dict, merge=True)

        # Sync Messages
        messages = sqlite_db.execute("SELECT * FROM messages").fetchall()
        for m in messages:
            m_dict = dict(m)
            fdb.collection("messages").document(str(m_dict["id"])).set(m_dict, merge=True)

        return {"configured": True, "synced_users": len(users), "status": "success"}
    except Exception as e:
        print(f"[Firebase Sync Error] {e}")
        return {"configured": True, "error": str(e)}
