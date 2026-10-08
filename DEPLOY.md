# Deploying ImageToConfig as an always-on web app

Goal: the app runs on a **server that's always on**, so teammates use a fixed URL
and it never depends on anyone's personal PC.

You handle scanned check images + MICR (PII), so **host it on internal company
infrastructure and involve your IT / DevOps + security team** — not a personal
machine or an unapproved public cloud.

---

## The idea (how "a normal web app" works)

    Your laptop  ─┐
    Teammate A   ─┼──►  http://imagetoconfig.internal:5000/  ──►  [ always-on server ]
    Teammate B   ─┘                                                (runs the app 24/7)

The server runs the app continuously and restarts it automatically after reboots.
Everyone just opens the URL and logs in.

---

## Recommended path (fintech / internal tool): a company VM or container host

1. **Ask IT for a small VM or container host** on the corporate network
   (Linux or Windows; ~2 vCPU / 4 GB RAM is plenty), and a DNS name
   (e.g. `imagetoconfig.internal`).
2. **Deploy** with Docker (files included here):
   ```bash
   docker compose up -d --build
   ```
   `restart: unless-stopped` keeps it running across crashes and reboots.
3. **Persist data**: the `./data` folder (SQLite DB + images) is mounted as a
   volume — back it up on a schedule.
4. **Put HTTPS in front**: terminate TLS at nginx / IIS / a company load balancer
   and proxy to port 5000. Don't serve auth over plain HTTP beyond a trusted LAN.
5. **First run**: open the URL → create the admin account → add teammates in
   **⚙ Users**.

### No Docker? Run it as a service directly
- **Linux (systemd):** a unit that runs `python serve.py` with `Restart=always`.
- **Windows Server:** run `serve.py` as a service with **NSSM**, or a Task
  Scheduler task set to "run whether user is logged on or not" + "restart on
  failure".

Either way set these environment variables on the server:
- `SECRET_KEY` — a long random string (stable logins across restarts)
- `PORT` / `HOST` — default `5000` / `0.0.0.0`
- `ANTHROPIC_API_KEY` — only if you want the optional AI features

---

## Cloud option (needs security approval for PII)

Azure is the usual fit for a Microsoft shop: an **Azure VM** (same Docker steps)
or **Azure App Service** (deploy the container). Because real check data is PII,
get sign-off from security first, keep it on a private network / VPN, and enable
HTTPS + access logging.

---

## Checklist before sharing widely
- [ ] Hosted on an approved internal server (not a laptop)
- [ ] `SECRET_KEY` set to a strong value
- [ ] HTTPS in front of the app
- [ ] `data/` folder backed up regularly
- [ ] Only test / masked data used (no live customer PII unless approved)
- [ ] Admin account created; teammates added via ⚙ Users
