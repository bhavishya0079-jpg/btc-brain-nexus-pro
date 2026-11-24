# Deploy

**Backend and frontend are deployed separately.** GitHub Pages can only host static files, so it serves the frontend only.

## 1. Backend (any VPS/container host: Render, Railway, Fly, a VM…)
- Python 3.11+, `pip install -r backend/requirements.txt`, start: `cd backend && python -m app.main`
- Environment: `NEXUS_ADMIN_PASSWORD`, `NEXUS_SECRET_KEY` (fixed, 64 hex chars, otherwise sessions reset on restart), `NEXUS_HOST=0.0.0.0`, `NEXUS_PORT`, provider keys.
- `NEXUS_CORS_ORIGINS=https://<user>.github.io` (not `*`) in production.
- `NEXUS_SERVE_FRONTEND=0` if the frontend is hosted on Pages.
- Put it behind HTTPS (reverse proxy such as Caddy/nginx or the host's TLS). Persist `backend/data/` on a volume.
- Run a **single** worker/process (the scheduler and rate limiter are in-process).

## 2. Frontend on GitHub Pages
1. Push the repo to GitHub; Settings → Pages → Source: GitHub Actions. The workflow `.github/workflows/pages.yml` publishes `frontend/`.
2. Open the Pages URL; in the login form set **Backend URL** to `https://your-backend.example.com/api` (stored in the browser), or edit `frontend/js/config.js` (`window.NEXUS_API_BASE`).
3. Never commit `.env`, DB files or keys.

## 3. Single-host alternative
Run only the backend; it serves `frontend/` itself at `/`.
