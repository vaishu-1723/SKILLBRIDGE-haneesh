# SkillBridge — upgraded real-functionality build

SkillBridge keeps the original Flask + Jinja + SQLite architecture and extends it instead of replacing it.

## Current architecture
- Flask server with server-side sessions
- SQLite with parameterized SQL
- Jinja templates
- Vanilla JavaScript + CSS
- Existing deterministic skill matching engine
- Optional OpenAI Responses API on the Flask server for the AI assistant
- WebRTC browser calling with SQLite signaling/polling

## Features added
- Relevance-ranked Find People search with related-skill matching
- Search suggestions, filters, sorting and match explanations
- Connection requests: pending, accept, decline, connected
- Persistent chat with polling, typing indicator and online/offline status
- WebRTC calling with call signaling, accept/reject/end and mute/camera-capable media
- Learning-session scheduling and status management
- Notifications and unread count
- Floating AI assistant that can search real users and take server-side actions
- Loading animation with rotating useful messages
- Light/dark theme persisted in localStorage and system-theme fallback
- Experience and availability fields
- Backward-compatible SQLite schema migration

## Install

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
```

## Environment

Set a strong production secret:

```text
SECRET_KEY=replace-with-a-long-random-secret
```

For AI:

```text
OPENAI_API_KEY=your-server-side-key
OPENAI_MODEL=gpt-5.6-luna
```

The OpenAI key is read only by Flask. It is never placed in browser JavaScript.

AI action calls are executed against the real SQLite database; the model is not allowed to invent profile IDs.

## Run

```bash
python app.py
```

Open `http://127.0.0.1:5000`.

## Database

No Supabase SQL is required because the supplied project uses SQLite, not Supabase. The application automatically creates/migrates:
- users experience/availability/last_seen fields
- connections
- messages
- typing
- sessions
- notifications
- calls

Keep `skillbridge.db` backed up before deploying a migration to important data.

## WebRTC limitation

The call feature uses browser WebRTC plus a lightweight SQLite signaling layer. It is a real implementation, not a fake button, but production deployment should add TURN infrastructure for reliable calls across restrictive NAT/firewall networks and HTTPS for browser media permissions.

## Test checklist

1. Register/login
2. Create a profile with experience and availability
3. Search Python/React/AWS and verify only relevant database users appear
4. Try related-skill matching
5. Send, accept and decline connections
6. Verify duplicate requests are blocked
7. Chat with an accepted connection; refresh and verify persistence
8. Verify typing and online/offline indicators
9. Start a WebRTC call; test accept/reject/end and microphone/camera controls
10. Schedule a session; verify it appears for both users
11. Complete/cancel a session
12. Verify notifications and unread count
13. Open AI and ask "Who can teach me Python?"
14. Ask AI to connect/message/schedule with a real returned user
15. Verify AI navigation actions
16. Toggle Light/Dark mode and refresh
17. Verify loader appears only briefly during page load
18. Test mobile widths and navigation

## Firebase Cloud setup

The project now includes Firebase Admin SDK + Firestore cloud sync. SQLite remains the local application database so the existing SkillBridge features continue to work, while cloud data is mirrored to Firestore. Password hashes are never copied to Firebase.

1. Create a Firebase project in the Firebase Console.
2. Open **Firestore Database** and create the database.
3. Go to **Project settings → Service accounts → Firebase Admin SDK → Generate new private key**.
4. Save the downloaded JSON as `firebase-service-account.json` in the Flask project folder for local development. Do not commit it to GitHub.
5. Install dependencies with `pip install -r requirements.txt`.
6. Set `FIREBASE_CREDENTIALS=firebase-service-account.json` (Windows PowerShell: `$env:FIREBASE_CREDENTIALS="firebase-service-account.json"`).
7. Optional: set `FIREBASE_PROJECT_ID` to your Firebase project ID.
8. Start Flask with `python app.py`.
9. Visit `/api/firebase/status` after logging in to confirm the connection.
10. The app performs an initial sync at startup. You can also POST to `/api/firebase/sync` while logged in to manually sync the current SQLite data.

Firestore collections created by the sync are: `users`, `connections`, `messages`, `sessions`, `notifications`, and `calls`.

### Important security note
The Firebase service-account JSON is a server credential. Never place it in `static/`, frontend JavaScript, HTML, a public repository, or a browser download. For production deployment, use your hosting provider's secret/environment-variable facility or `FIREBASE_CREDENTIALS_JSON` rather than storing a credential file in the source tree.


## Excel account database (added)

The app now uses `users.xlsx` as the basic account/profile database.
- Registration writes the new account to the `users` sheet.
- Login reads the email and password hash from Excel.
- Profile lookups read user information from Excel.
- `last_seen` is updated in Excel when the account is opened/used.
- Passwords are stored as Werkzeug hashes, not plain text.
- The existing SQLite database remains in place for relational features such as connections, chat, sessions, notifications and calls.
- On startup, any existing SQLite users missing from Excel are copied into `users.xlsx`.

### Excel limitation

This is suitable for a basic/local project or small demo. Excel is not a true concurrent server database; for many simultaneous users or production deployment, PostgreSQL/Firestore/MySQL is safer. The existing Firebase integration can still be used separately.
