"""Shared-server launcher for ImageToConfig, using waitress (a real WSGI server).

Why not `python app.py` for shared use?
  app.py runs Flask's built-in dev server — fine for one person on localhost, but
  not meant to serve a team. waitress is a small, production-grade WSGI server
  that runs well on Windows.

Usage
  pip install waitress
  # (optional) choose host/port; defaults bind to all interfaces on :5000
  set HOST=0.0.0.0
  set PORT=5000
  python serve.py

Then teammates open  http://<this-machine-or-server-IP>:<port>/  and sign in with
accounts an admin created. On the very first run the app shows a one-time page to
create the first admin; any existing local library/history becomes that admin's.

Notes
  - Set a stable SECRET_KEY env var on the server (else a random one is generated
    and persisted under data/secret_key).
  - Put ANTHROPIC_API_KEY in the server's environment (or a gitignored .env) only
    if you want the optional AI features; the app works fully without it.
  - Data lives in ./data (SQLite + images). Back that folder up.
"""
import os

from app import app
from core import config, db, version

if __name__ == "__main__":
    try:
        from waitress import serve
    except ImportError:
        raise SystemExit("waitress is not installed. Run:  pip install waitress")

    config.ensure_dirs()
    db.init_db()
    host = os.environ.get("HOST", "0.0.0.0")     # all interfaces, so the LAN can reach it
    port = int(os.environ.get("PORT", "5000"))
    print(f" * ImageToConfig {version.APP_VERSION} (waitress) serving on "
          f"http://{host}:{port}/  — users sign in; first run creates the admin.")
    serve(app, host=host, port=port, threads=8)
