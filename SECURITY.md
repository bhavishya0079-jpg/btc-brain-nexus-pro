# Security notes
- Read-only by construction: provider allow-lists + forbidden-path regex in `providers/http.py`; no order/fund endpoints exist in the API. Tests assert that no allow-listed endpoint matches the forbidden pattern.
- Secrets: env/.env only; `.env` git-ignored; strategy specs containing order/key/fund-like fields are rejected (`FORBIDDEN_KEYS`, `assert_no_live_keys`).
- Auth: PBKDF2 password hashes, HMAC-signed expiring bearer tokens, login + global rate limits, 1 MB body limit, strict input validators, parameterised SQL only.
- Headers: nosniff, X-Frame-Options DENY, no-referrer, no-store on every response (`SECURITY_HEADERS`), CORS configurable.
- Audit log for strategy create/update/restore/delete, learning proposals/approvals/rejections/rollbacks, backups and restores.
- Backups: SQLite online backup, integrity check, safety snapshot before restore.
- Set a strong `NEXUS_ADMIN_PASSWORD`, a fixed `NEXUS_SECRET_KEY`, restrict CORS, use HTTPS.
- Single-user model (admin). Do not expose publicly without HTTPS.
