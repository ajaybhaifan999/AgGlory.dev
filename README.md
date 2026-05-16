# FFGlory backend

## Folder layout to deploy
```
ffglory/
├─ app.py
├─ ff_runner.py        ← plug your Free Fire automation here
├─ requirements.txt
├─ Procfile
├─ render.yaml
└─ public/             ← put login.html, client.html, shared.css, shared.js, favicon.ico
```

## Local run
```bash
pip install -r requirements.txt
export FLASK_SECRET="$(python -c 'import secrets;print(secrets.token_hex(32))')"
export FERNET_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
python app.py
# open http://localhost:5000
```

## Deploy on Render
1. Push this folder to GitHub.
2. Render → **New → Blueprint** → select the repo. `render.yaml` does the rest.
3. After first deploy, paste a generated `FERNET_KEY` in the dashboard.
4. Custom domain → add `ffglory.pro` and update DNS (CNAME to the Render URL).

## Where to plug Free Fire code
Open `ff_runner.py` and implement the four functions marked `# >>> FF_PLUGIN`:
- `ff_login_guest(uid, password)`
- `ff_create_lobby(session)`
- `ff_invite(session, lobby_id, invitee_uid)`
- `ff_start_match(session, lobby_id)`

Everything else (queue, retries, DB updates, activity logging, status flips, glory tracking) is already wired.

## Frontend ↔ backend
The included `app.py` serves `public/login.html` for unauthenticated users and
`public/client.html` after login (JWT in HTTP-only cookie `ff_token`). Your
existing `shared.js` `API` object should point to the paths below (all
implemented):

| Frontend `API.*`        | Endpoint                                |
|-------------------------|-----------------------------------------|
| `me`                    | `GET  /api/auth/me`                     |
| `clientGroups`          | `GET  /api/client/groups`               |
| `clientAction`          | `POST /api/client/groups/<id>/action`   |
| `activityLog`           | `GET  /api/client/activity-log`         |
| `clientActiveAlert`     | `GET  /api/client/active-alert`         |
| `inboxMessages`         | `GET  /api/client/inbox`                |
| `notifications`         | `GET  /api/client/notifications`        |
| `myCoupons`             | `GET  /api/client/coupons`              |
| `redeemedCoupons`       | `GET  /api/client/redeemed-coupons`     |
| `transactions`          | `GET  /api/client/transactions`         |
| `pricing`               | `GET  /api/client/pricing`              |
| `vapidPublicKey`        | `GET  /api/client/vapid-public-key`     |

Extra (not in API object but expected by client.html):
- `POST /api/auth/login`, `/register`, `/logout`, `/change-password`
- `GET/POST/DELETE /api/client/guests` (manage guest UIDs)
- `POST /api/client/groups` (create), `/<id>/invite`
- `GET /api/client/glory-progression?group_id=…`
- `GET /api/client/refetch-clan-data?group_id=…`
- `POST /api/admin/group-action`
