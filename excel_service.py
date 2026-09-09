"""Simple Excel-backed user database for SkillBridge.

The workbook is intentionally basic: one sheet named ``users`` with one row
per account. Passwords are stored as Werkzeug password hashes, never plaintext.

Excel is the source of truth for account lookup/authentication. SQLite is kept
for the rest of SkillBridge's relational features (connections, chat, etc.).
"""
import os
import threading
from openpyxl import Workbook, load_workbook

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
EXCEL_PATH = os.path.join(BASE_DIR, "users.xlsx")
SHEET_NAME = "users"

COLUMNS = [
    "id", "name", "email", "password_hash", "teach_skill", "learn_skill",
    "bio", "image", "location", "experience", "availability",
    "last_seen", "created_at"
]

_lock = threading.RLock()


def _ensure_workbook():
    os.makedirs(BASE_DIR, exist_ok=True)
    if not os.path.exists(EXCEL_PATH):
        wb = Workbook()
        ws = wb.active
        ws.title = SHEET_NAME
        ws.append(COLUMNS)
        wb.save(EXCEL_PATH)
        wb.close()


def _open():
    _ensure_workbook()
    return load_workbook(EXCEL_PATH)


def _normalize(row):
    if not row:
        return None
    return {col: row.get(col, "") for col in COLUMNS}


def all_users():
    with _lock:
        wb = _open()
        ws = wb[SHEET_NAME]
        headers = [c.value for c in ws[1]]
        result = []
        for values in ws.iter_rows(min_row=2, values_only=True):
            data = dict(zip(headers, values))
            if data.get("id") not in (None, ""):
                result.append(_normalize(data))
        wb.close()
        return result


def get_user_by_email(email):
    email = (email or "").strip().lower()
    for user in all_users():
        if (user.get("email") or "").strip().lower() == email:
            return user
    return None


def get_user_by_id(user_id):
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    for user in all_users():
        if int(user.get("id") or 0) == user_id:
            return user
    return None


def insert_user(user):
    """Append one user row. Returns the assigned integer id."""
    with _lock:
        wb = _open()
        ws = wb[SHEET_NAME]
        ids = [int(r[0]) for r in ws.iter_rows(min_row=2, values_only=True)
               if r[0] not in (None, "")]
        user_id = int(user.get("id") or (max(ids, default=0) + 1))
        row = dict(user)
        row["id"] = user_id
        ws.append([row.get(col, "") for col in COLUMNS])
        wb.save(EXCEL_PATH)
        wb.close()
        return user_id


def update_user(user_id, **changes):
    with _lock:
        wb = _open()
        ws = wb[SHEET_NAME]
        headers = {cell.value: cell.column for cell in ws[1]}
        found = False
        for row in ws.iter_rows(min_row=2):
            if int(row[headers["id"] - 1].value or 0) == int(user_id):
                for key, value in changes.items():
                    if key in headers:
                        row[headers[key] - 1].value = value
                found = True
                break
        if found:
            wb.save(EXCEL_PATH)
        wb.close()
        return found


def sync_sqlite_users(db):
    """Create/match Excel rows from the existing SQLite users table once."""
    with _lock:
        existing = {int(u["id"]): u for u in all_users()}
        rows = db.execute("SELECT * FROM users ORDER BY id").fetchall()
        missing = [dict(r) for r in rows if int(r["id"]) not in existing]
        if not missing:
            return 0
        wb = _open()
        ws = wb[SHEET_NAME]
        for user in missing:
            ws.append([user.get(col, "") for col in COLUMNS])
        wb.save(EXCEL_PATH)
        wb.close()
        return len(missing)
