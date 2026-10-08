"""ImageToConfig — QA batch generator for lockbox / check-processing testing.

Run:  pip install -r requirements.txt  &&  python app.py
Then: http://127.0.0.1:5000
"""
import hashlib
import io
import json
import os
import time
import zipfile
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, send_file, flash, abort, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash

from core import db, ocr, imaging, batch as batchmod, xmlgen, rules, xml_import
from core import oxi_writer, llm, version
from core.oxi_reader import open_oxz
from core.config import (ITEM_TYPES, ITEM_TYPE_LABELS, BATCH_TYPES, OUTPUT_DIR,
                         LIBRARY_DIR, REFERENCE_DIR, DATA_DIR, ensure_dirs)
import cl_routes


def _load_secret_key():
    """A stable secret key so sessions survive restarts and aren't the old, public
    dev key. From $SECRET_KEY, else a random key persisted under the data dir."""
    v = os.environ.get("SECRET_KEY")
    if v:
        return v
    ensure_dirs()
    path = os.path.join(DATA_DIR, "secret_key")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    key = os.urandom(32).hex()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB uploads
app.register_blueprint(cl_routes.cl_bp)

# endpoints reachable without being logged in
_PUBLIC_ENDPOINTS = {"login", "logout", "setup", "static"}


@app.before_request
def _setup():
    ensure_dirs()
    db.init_db()
    ep = request.endpoint or ""
    if ep in _PUBLIC_ENDPOINTS:
        return
    # first-run: no accounts yet -> force creating the first admin
    if db.count_users() == 0:
        return redirect(url_for("setup"))
    uid = session.get("user_id")
    user = db.get_user(uid) if uid else None
    if not user:                       # not logged in, or account was deleted
        session.clear()
        return redirect(url_for("login", next=request.path))


def _uid():
    return session.get("user_id")


def current_user():
    uid = session.get("user_id")
    return db.get_user(uid) if uid else None


@app.context_processor
def _inject_user():
    u = current_user()
    return {
        "current_user": u,
        "app_version": version.APP_VERSION,
        "update_available": version.has_update((u or {}).get("last_seen_version") or ""),
    }


def _owns(row):
    """True if `row` (image/batch) belongs to the logged-in user. Compares as int
    because user_id may be stored as TEXT ('1') on some columns yet the session
    holds an int (1) — a strict != would wrongly reject the owner."""
    if not row:
        return False
    try:
        return int(row.get("user_id")) == int(_uid())
    except (TypeError, ValueError):
        return False


def _owned_image(image_id):
    """Fetch an image only if it belongs to the logged-in user (else None)."""
    im = db.get_image(image_id)
    return im if _owns(im) else None


def _owned_batch(batch_id):
    b = db.get_batch(batch_id)
    return b if _owns(b) else None


def admin_required(fn):
    @wraps(fn)
    def _w(*a, **k):
        u = current_user()
        if not u or not u.get("is_admin"):
            abort(403)
        return fn(*a, **k)
    return _w


# ---------------------------------------------------------------------------
# authentication: first-run setup, login / logout, admin user management
# ---------------------------------------------------------------------------
@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run: create the first admin. Disabled once any account exists."""
    if db.count_users() > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        pw = request.form.get("password") or ""
        pw2 = request.form.get("password2") or ""
        if not username or not pw:
            flash("Enter a username and password.", "error")
        elif pw != pw2:
            flash("The two passwords don't match.", "error")
        elif len(pw) < 6:
            flash("Use a password of at least 6 characters.", "error")
        else:
            uid = db.create_user(username, generate_password_hash(pw),
                                 is_admin=True, last_seen_version=version.APP_VERSION)
            if not uid:
                flash("That username is taken.", "error")
            else:
                db.assign_orphan_data(uid)      # keep existing local library/history
                session.clear()
                session["user_id"] = uid
                flash(f"Welcome, {username}. You're the admin.", "ok")
                return redirect(url_for("index"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if db.count_users() == 0:
        return redirect(url_for("setup"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        pw = request.form.get("password") or ""
        u = db.get_user_by_username(username)
        if u and check_password_hash(u["password_hash"], pw):
            session.clear()
            session["user_id"] = u["id"]
            nxt = request.args.get("next") or request.form.get("next") or ""
            if nxt.startswith("/"):
                return redirect(nxt)
            return redirect(url_for("index"))
        flash("Wrong username or password.", "error")
    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin/users")
@admin_required
def admin_users():
    return render_template("admin_users.html", users=db.list_users())


@app.route("/admin/users/add", methods=["POST"])
@admin_required
def admin_users_add():
    username = (request.form.get("username") or "").strip()
    pw = request.form.get("password") or ""
    is_admin = bool(request.form.get("is_admin"))
    if not username or not pw:
        flash("Username and password are required.", "error")
    elif len(pw) < 6:
        flash("Use a password of at least 6 characters.", "error")
    else:
        uid = db.create_user(username, generate_password_hash(pw),
                             is_admin=is_admin, last_seen_version=version.APP_VERSION)
        flash(f"Created user '{username}'." if uid else "That username is taken.",
              "ok" if uid else "error")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/reset", methods=["POST"])
@admin_required
def admin_users_reset(uid):
    pw = request.form.get("password") or ""
    u = db.get_user(uid)
    if not u:
        flash("No such user.", "error")
    elif len(pw) < 6:
        flash("Use a password of at least 6 characters.", "error")
    else:
        db.set_password(uid, generate_password_hash(pw))
        flash(f"Reset password for '{u['username']}'.", "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_users_delete(uid):
    if uid == _uid():
        flash("You can't delete your own account while logged in.", "error")
        return redirect(url_for("admin_users"))
    u = db.get_user(uid)
    if u:
        db.delete_user(uid)
        flash(f"Deleted user '{u['username']}'. Their images/history remain in the "
              f"database but are no longer accessible.", "ok")
    return redirect(url_for("admin_users"))


@app.route("/whatsnew")
def whatsnew():
    u = current_user()
    last = (u or {}).get("last_seen_version") or ""
    entries = version.entries_since(last) or version.CHANGELOG[:1]
    return render_template("whatsnew.html", entries=entries)


@app.route("/whatsnew/dismiss", methods=["POST"])
def whatsnew_dismiss():
    if _uid():
        db.set_last_seen_version(_uid(), version.APP_VERSION)
    return redirect(url_for("index"))


def _plan():
    plan = session.get("plan", [])
    # guard against an old (pre-transaction) flat plan format in a stale cookie
    if not isinstance(plan, list) or any(not isinstance(t, list) for t in plan):
        return []
    return plan


def _save_plan(plan):
    session["plan"] = plan
    session.modified = True


def _role(item_type):
    if item_type == "check":
        return "check"
    if item_type == "statement":
        return "statement"
    if item_type == "fullpage":
        return "fullpage"
    if item_type == "stub":
        return "stub"
    if item_type == "correspondence":
        return "corr"
    return None


# ---------------------------------------------------------------------------
# perceptual-dedup helpers
# ---------------------------------------------------------------------------
def _known_hashes():
    """(id, ahash, micr_key) for every current library image."""
    return [(im["id"], imaging.ahash(im["front_path"]), ocr.micr_key(im.get("raw_micr")))
            for im in db.list_images(_uid())]


def _is_dup(phash, micr_key, known):
    """Visually-similar (ahash within DUP_THRESHOLD) is only treated as a real
    duplicate when there's no content evidence against it: either neither side
    has a confidently-read MICR (fall back to visual similarity alone, as
    before), or both sides have one AND it matches. If MICR was read on only
    one side, or was read on both and disagrees, the visual match is treated as
    a false positive (same check stock/template, different document) rather
    than a duplicate."""
    for _i, h, k in known:
        if imaging.hamming(phash, h) > imaging.DUP_THRESHOLD:
            continue
        if micr_key is None and k is None:
            return True
        if micr_key is not None and k is not None and micr_key == k:
            return True
    return False


def _cleanup_files(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# main page
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    images = db.list_images(_uid())
    # per-side preview version (file mtime) so the browser refetches after a rotate
    for im in images:
        for side, key in (("front_v", "front_path"), ("back_v", "back_path")):
            try:
                im[side] = int(os.path.getmtime(im[key])) if im.get(key) else 0
            except OSError:
                im[side] = 0
    img_by_id = {im["id"]: im for im in images}

    def _amt(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    plan = _plan()
    targets = session.get("compose_targets") or []
    plan_txns = []
    plan_total = 0.0
    for ti, txn in enumerate(plan):
        docs = []
        for g in batchmod.grouped(txn):
            docs.append({"entry": g, "count": g["count"],
                         "image": img_by_id.get(g["image_id"])})
        txn_amt = sum(_amt(e.get("amount")) for e in txn)
        plan_total += txn_amt
        tgt = targets[ti] if ti < len(targets) else None
        tgt_str = _mix_str(tgt) if (tgt and sum(int(v or 0) for v in tgt.values())) else ""
        plan_txns.append({"ti": ti, "docs": docs, "total": len(txn),
                          "amount": f"{txn_amt:.2f}", "target": tgt_str})

    pool_counts = {"check": 0, "statement": 0, "fullpage": 0, "stub": 0, "corr": 0}
    for im in images:
        r = _role(im["item_type"])
        if r:
            pool_counts[r] += 1

    try:
        css_v = int(os.path.getmtime(os.path.join(app.static_folder, "style.css")))
    except OSError:
        css_v = 0
    cfg = session.get("batch_cfg", {})
    compose_cfg = session.get("compose_cfg", {})
    file_preview = session.pop("file_preview", None)
    _sync_registered_batch_types()
    return render_template(
        "index.html",
        css_v=css_v,
        images=images,
        plan_txns=plan_txns,
        plan_count=batchmod.count_items(plan),
        plan_total=f"{plan_total:.2f}",
        pool_counts=pool_counts,
        batches=db.list_batches(_uid()),
        item_types=ITEM_TYPES,
        item_type_labels=ITEM_TYPE_LABELS,
        batch_types=_all_batch_types(),
        batch_rules=_all_batch_rules(),
        tesseract_ok=ocr.tesseract_available(),
        ai_status=llm.status(),
        today=time.strftime("%Y/%m/%d"),
        cfg=cfg,
        compose_cfg=compose_cfg,
        txn_cap=_txn_cap(),
        file_preview=file_preview,
        registered_formats=_registered_formats(),
    )


@app.route("/state/save", methods=["POST"])
def state_save():
    """Autosave the Build-a-batch form fields to the session so no button press
    resets them. Called on every field change (config form + compose form)."""
    which = request.form.get("_form")
    if which == "config":
        cfg = session.get("batch_cfg", {})
        for k in ("batch_type", "biller", "site", "subsite", "batch_date",
                  "file_date", "lockbox", "operator", "format"):
            if request.form.get(k) is not None:
                cfg[k] = request.form.get(k)
        cfg["test_file"] = "Y" if request.form.get("test_file") else "N"
        cfg["control_totals"] = bool(request.form.get("control_totals"))
        session["batch_cfg"] = cfg
    # NOTE: the compose form is intentionally NOT autosaved — its counts are the
    # enforcement target and must only change via Build plan, or they'd drift
    # from the plan that was actually built.
    session.modified = True
    return ("", 204)


# ---------------------------------------------------------------------------
# ingestion (with exact + perceptual dedup)
# ---------------------------------------------------------------------------
@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("images")
    known = _known_hashes()
    added = dup = 0
    for f in files:
        if not f or not f.filename:
            continue
        data = f.read()
        if not data:
            continue
        # Read the MICR from the ORIGINAL (full-quality colour) image BEFORE we
        # binarise it — reading off the harsh bitonal copy loses the codeline.
        micr = ocr.read_micr(data)
        try:
            sha, front_path, _ = imaging.store_bytes(data)
        except Exception as e:
            flash(f"Could not read {f.filename}: {e}", "error")
            continue
        if db.find_image_by_hash(sha, _uid()):
            dup += 1
            continue
        phash = imaging.ahash(front_path)
        mkey = ocr.micr_key(micr)
        if _is_dup(phash, mkey, known):
            _cleanup_files(front_path)
            dup += 1
            continue
        # OCR guesses the document type (blank MICR if the model found none).
        read = ocr.read_image(front_path)
        rec = {"user_id": _uid(), "sha256": sha, "original_filename": f.filename,
               "front_path": front_path, "back_path": None, "source": "upload",
               "item_type": read["item_type"], "raw_micr": micr}
        new_id, created = db.insert_image(rec)
        if created:
            known.append((new_id, phash, mkey))
            added += 1
        else:
            dup += 1
    flash(f"Uploaded: {added} new, {dup} duplicate(s) skipped.", "ok")
    return redirect(url_for("index"))


@app.route("/upload_oxz", methods=["POST"])
def upload_oxz():
    """Accepts a scanner batch (.OXZ zip) OR any config/batch file.
    Detects format by content, extracts images, and registers unknown
    extensions so the user can generate that format later."""
    f = request.files.get("oxz")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("index"))
    data = f.read()
    if not data:
        flash("Uploaded file is empty.", "error")
        return redirect(url_for("index"))
    ext = os.path.splitext(f.filename)[1].lstrip(".")
    if data[:2] == b"PK":                       # zip magic -> OXZ scanner batch
        return _ingest_oxz(f.filename, data)
    if xml_import.looks_like_xml(data):          # any XML-based file
        result = _ingest_batch_xml(f.filename, data)
        try:
            kind, _ = xml_import.parse_batch_xml(data)
        except Exception:
            kind = None
        base = "lbxxml" if kind == "LBXxml" else "3xml"
        _register_extension(ext, base)
        return result
    # unknown but text-readable — store a preview for the user
    if _is_text(data):
        preview = data.decode("utf-8", errors="replace")[:5000]
        session["file_preview"] = {"filename": f.filename, "ext": ext,
                                   "content": preview, "size": len(data)}
        base = "3xml"
        # If Claude is configured, ask it to infer the schema family + batch type.
        if llm.available():
            info = llm.analyze_sample(f.filename, preview)
            if not info.get("_error"):
                base = info.get("base_schema") or "3xml"
                bt = (info.get("batch_type_name") or "").strip()
                if bt:
                    _register_batch_type(bt, info.get("allowed_types"),
                                         desc=info.get("summary"), source="AI-analyzed sample")
                flash(f"Claude analyzed '{f.filename}': schema like .{base}. "
                      f"{info.get('summary', '')}", "ok")
        _register_extension(ext, base)
        flash(f"'{f.filename}' (.{ext}) — readable text file. "
              f"Images extracted: 0. Preview available below.", "ok")
        return redirect(url_for("index"))
    flash(f"'{f.filename}' is a binary file that couldn't be parsed. "
          f"Expected XML, OXZ, or a text-based format.", "error")
    return redirect(url_for("index"))


def _is_text(data):
    """Check if data looks like a text file (not binary)."""
    try:
        data[:2000].decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    try:
        data[:2000].decode("latin-1")
        non_text = sum(1 for b in data[:2000] if b < 8 or (14 <= b < 32))
        return non_text < len(data[:2000]) * 0.05
    except Exception:
        return False


def _registered_formats():
    """Persisted learned formats as [{'ext','base'}] (survive restarts)."""
    return db.list_formats()


def _base_for_format(fmt):
    """Which built-in generator a (possibly custom) format maps to."""
    for r in _registered_formats():
        if r["ext"].lower() == fmt.lower():
            return r["base"]
    return None


def _register_extension(ext, base="3xml"):
    """Remember a new file extension (and which schema it resembles) so the
    format dropdown can offer to generate it later. Saved to the DB."""
    if not ext or ext.lower() in ("xml", "zip"):
        return
    known = {"3xml", "lbxxml", "78tml", "oxz"}
    if ext.lower() in known:
        return
    if any(r["ext"].lower() == ext.lower() for r in db.list_formats()):
        return
    db.add_format(ext, base)
    flash(f"New format detected & saved: .{ext} (schema like .{base}) — added to the "
          f"generate dropdown. You can now create .{ext} files any time.", "ok")


# ---------------------------------------------------------------------------
# learned batch TYPES (parallel to registered formats above)
# ---------------------------------------------------------------------------
# When we ingest a batch (OXZ scanner batch, or an imported XML) whose work/job
# type isn't one of the built-in BATCH_TYPES, we remember it so the user can
# compose + generate a batch of that same type. Stored in the session as a list
# of rule dicts: {name, allowed:[...], group, desc, confirmed:False}.
def _registered_batch_types():
    """Persisted learned batch types as a list of rule dicts (survive restarts)."""
    out = []
    for r in db.list_learned_batch_types():
        out.append({
            "name": r["name"],
            "allowed": set(r["allowed"] or ["check", "stub", "statement", "correspondence"]),
            "group": r["group"] or "multiples",
            "desc": r["desc"] or f"Learned batch type '{r['name']}'.",
            "confirmed": bool(r.get("confirmed")),
        })
    return out


def _all_batch_types():
    """Built-in batch types followed by any session-learned ones."""
    learned = [r["name"] for r in _registered_batch_types()
               if r["name"] not in BATCH_TYPES]
    return list(BATCH_TYPES) + learned


def _all_batch_rules():
    """Rule map for every selectable batch type (built-in + learned). A rule
    learned from a real sample OVERRIDES the built-in best-effort default."""
    out = {t: rules.rule_for(t) for t in BATCH_TYPES}
    for r in _registered_batch_types():
        out[r["name"]] = r
    return out


def _sync_registered_batch_types():
    """Inject learned batch-type rules into rules.RULES so the existing validators
    (rule_for / validate_* / grouping) use them. Rules learned from a real sample
    OVERRIDE the built-in defaults. (Single-user prototype: RULES is process-wide.)"""
    for r in _registered_batch_types():
        rules.RULES[r["name"]] = {
            "allowed": set(r["allowed"]), "group": r["group"],
            "desc": r["desc"], "confirmed": r.get("confirmed", False),
        }


def _register_batch_type(name, allowed, group="multiples", desc=None, source="batch"):
    """Remember a new batch/work type so it appears in the Batch-type dropdown."""
    name = (name or "").strip()
    if not name or name in BATCH_TYPES:
        return
    if any(r["name"] == name for r in db.list_learned_batch_types()):
        return
    allowed = sorted(allowed) if allowed else ["check", "stub", "statement", "correspondence"]
    desc = desc or (f"Learned from an ingested {source}. Allowed documents: "
                    f"{', '.join(allowed)}. Structure is best-effort.")
    db.add_learned_batch_type(name, allowed, group, desc)
    rules.RULES.setdefault(name, {"allowed": set(allowed), "group": group,
                                  "desc": desc, "confirmed": False})
    flash(f"New batch type learned & saved: '{name}' — added to the Batch type dropdown. "
          f"You can now compose and generate a batch of this type any time.", "ok")


def _ingest_oxz(filename, data):
    tmp_path = os.path.join(OUTPUT_DIR, f"_upload_{int(time.time())}.oxz")
    with open(tmp_path, "wb") as fh:
        fh.write(data)
    known = _known_hashes()
    added = dup = excluded = 0
    seen_types = set()          # doc types this batch actually contains
    try:
        meta, pages, read_member, zf = open_oxz(tmp_path)
        try:
            for p in pages:
                if p["excluded"]:
                    excluded += 1
                    continue
                fb = read_member(p["front_filename"])
                if not fb:
                    continue
                bb = read_member(p["back_filename"])
                sha, front_path, back_path = imaging.store_bytes(fb, bb)
                if db.find_image_by_hash(sha, _uid()):
                    dup += 1
                    continue
                phash = imaging.ahash(front_path)
                micr = p.get("micr")   # accurate scanner MICR, if present
                mkey = ocr.micr_key(micr)
                if _is_dup(phash, mkey, known):
                    _cleanup_files(front_path, back_path)
                    dup += 1
                    continue
                read = ocr.read_image(front_path, scanner_hint=p["page_type"])
                item_type = read["item_type"]
                if p["item_type"] not in ("unknown", "envelope"):
                    item_type = p["item_type"]   # scanner page type wins when specific
                if item_type not in ("unknown", "envelope"):
                    seen_types.add(item_type)
                rec = {"user_id": _uid(), "sha256": sha,
                       "original_filename": f"{filename}:{p['front_filename']}",
                       "front_path": front_path, "back_path": back_path,
                       "source": "oxz", "item_type": item_type,
                       "raw_micr": micr}
                new_id, created = db.insert_image(rec)
                if created:
                    known.append((new_id, phash, mkey))
                    added += 1
                else:
                    dup += 1
        finally:
            zf.close()
        flash(f"OXZ '{meta.get('job_name')}': {added} new, {dup} duplicate(s) skipped, "
              f"{excluded} envelope page(s) excluded.", "ok")
        # Learn the batch/work type so the user can create one of the same kind.
        _learn_batch_type_from_oxz(filename, meta, seen_types)
    except Exception as e:
        flash(f"Failed to read OXZ: {e}", "error")
    finally:
        _cleanup_files(tmp_path)
    return redirect(url_for("index"))


def _learn_batch_type_from_oxz(filename, meta, seen_types):
    """Derive a batch-type name from an OXZ's JobName / JobType and register it."""
    name = (meta.get("job_name") or meta.get("job_type")
            or os.path.splitext(os.path.basename(filename))[0] or "").strip()
    if not name:
        return
    allowed = sorted(seen_types) if seen_types else ["check", "stub", "statement", "correspondence"]
    jt = (meta.get("job_type") or "").strip()
    desc = (f"Learned from scanner batch '{filename}'"
            + (f" (JobType {jt})" if jt else "")
            + f". Contains: {', '.join(allowed)}. Grouping is best-effort — "
              f"compose transactions as needed.")
    _register_batch_type(name, allowed, group="multiples", desc=desc, source="OXZ scanner batch")


def _ingest_batch_xml(filename, data):
    """Pull embedded images out of a .3xml / .LBXxml file into the library."""
    try:
        kind, docs = xml_import.parse_batch_xml(data)
    except Exception as e:
        flash(f"Couldn't parse '{filename}' as .3xml / .LBXxml: {e}", "error")
        return redirect(url_for("index"))
    if not docs:
        flash(f"No document images found in '{filename}'.", "error")
        return redirect(url_for("index"))
    known = _known_hashes()
    added = dup = 0
    for d in docs:
        try:
            sha, front_path, back_path = imaging.store_bytes(d["front"], d.get("back"))
        except Exception:
            continue
        if db.find_image_by_hash(sha, _uid()):
            dup += 1
            continue
        phash = imaging.ahash(front_path)
        # keep a scanline only if it's a real codeline (has digits); drop junk
        # like the sanity file's "'''';;;sdddd''''''ddd".
        scan = d.get("scanline")
        if scan and sum(c.isdigit() for c in scan) < 4:
            scan = None
        mkey = ocr.micr_key(scan)
        if _is_dup(phash, mkey, known):
            _cleanup_files(front_path, back_path)
            dup += 1
            continue
        rec = {"user_id": _uid(), "sha256": sha, "original_filename": filename,
               "front_path": front_path, "back_path": back_path,
               "source": "import", "item_type": d["item_type"],
               "raw_micr": scan}
        new_id, created = db.insert_image(rec)
        if created:
            known.append((new_id, phash, mkey))
            added += 1
        else:
            dup += 1
    flash(f"Imported {kind} '{filename}': {added} new image(s), {dup} duplicate(s) skipped.", "ok")
    _learn_rule_from_batch(data)     # learn this transaction type's real structure
    _maybe_save_checklist(filename, data)  # archive Check-List files in their own section
    return redirect(url_for("index"))


def _learn_rule_from_batch(data):
    """If `data` is a transaction-structured batch (.3xml / .78tml), learn or
    refresh the rule for its transaction type from the real document structure —
    which document types it allows and how many per transaction. Saved + applied."""
    info = xml_import.analyze_batch_structure(data)
    if not info or not info.get("transaction_type"):
        return
    name = info["transaction_type"]
    allowed = set(info["doc_types"])
    if "statement" in allowed:
        allowed.add("fullpage")          # our full-page docs are statement-family
    if not allowed:
        return
    group = info["group"]
    counts = info["item_counts"] or [0]
    lo, hi = min(counts), max(counts)
    span = f"{lo}" if lo == hi else f"{lo}–{hi}"
    desc = (f"Learned from a real {name} sample: allows {', '.join(sorted(allowed))}; "
            f"{span} document(s) per transaction.")
    db.upsert_learned_batch_type(name, sorted(allowed), group, desc, confirmed=True)
    rules.RULES[name] = {"allowed": allowed, "group": group, "desc": desc, "confirmed": True}
    verb = "updated" if name in BATCH_TYPES else "learned"
    flash(f"Analyzed the '{name}' transaction type from this sample — {verb} its rule: "
          f"allows {', '.join(sorted(allowed))}, {span} doc(s)/transaction.", "ok")


@app.route("/library/dedup", methods=["POST"])
def library_dedup():
    """Remove near-duplicate images already in the library (keep the first)."""
    images = sorted(db.list_images(_uid()), key=lambda r: r["id"])
    kept = []  # (ahash, micr_key) already-kept images, for _is_dup's `known` shape
    removed = 0
    for im in images:
        h = imaging.ahash(im["front_path"])
        k = ocr.micr_key(im.get("raw_micr"))
        if _is_dup(h, k, [(None, kh, kk) for kh, kk in kept]):
            db.delete_image(im["id"])
            _cleanup_files(im["front_path"], im.get("back_path"))
            removed += 1
        else:
            kept.append((h, k))
    flash(f"Removed {removed} duplicate image(s); {len(kept)} unique kept.", "ok")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# library item editing
# ---------------------------------------------------------------------------
@app.route("/image/<int:image_id>/edit", methods=["POST"])
def edit_image(image_id):
    im = _owned_image(image_id)
    if not im:
        abort(404)
    fields = {
        "item_type": request.form.get("item_type", im["item_type"]),
        "amount": request.form.get("amount", "").strip() or None,
        "raw_micr": request.form.get("raw_micr", "").strip() or None,
    }
    db.update_image(image_id, fields)
    flash(f"Saved item #{image_id}.", "ok")
    return redirect(url_for("index"))


@app.route("/image/<int:image_id>/reread_micr", methods=["POST"])
def reread_micr(image_id):
    """Re-run the MICR model on demand and RETURN the reading for review — it is
    NOT saved automatically (a degraded scan can produce a wrong-but-plausible
    routing). The user verifies it in the Scanline field, then clicks Save."""
    im = _owned_image(image_id)
    if not im:
        return {"ok": False, "micr": ""}, 404
    return {"ok": True, "micr": ocr.read_micr(im["front_path"]) or ""}


@app.route("/image/<int:image_id>/rotate", methods=["POST"])
def rotate_image(image_id):
    im = _owned_image(image_id)
    if not im:
        abort(404)
    side = request.form.get("side", "front")
    direction = request.form.get("direction", "right")
    path = im["front_path"] if side == "front" else im.get("back_path")
    if not path or not os.path.exists(path):
        flash("No image to rotate.", "error")
        return redirect(url_for("index"))
    new_sha = imaging.rotate_file(path, direction)
    # the front image is the dedup key — refresh it (unless it now collides)
    if side == "front" and new_sha and not db.find_image_by_hash(new_sha, _uid()):
        db.update_sha(image_id, new_sha)
    if request.headers.get("X-Requested-With"):       # AJAX (card / viewer) — no flash, no reload
        return ("", 204)
    flash(f"Rotated image #{image_id} ({side}).", "ok")
    return redirect(url_for("index"))


@app.route("/image/<int:image_id>/attach_side", methods=["POST"])
def attach_side(image_id):
    """Club a second image file onto an existing library item as its back
    (or replace the front) — producing one item with both sides, exactly like
    an OXZ-ingested image that carries front + back together."""
    im = _owned_image(image_id)
    if not im:
        abort(404)
    side = (request.form.get("side") or "back").lower()
    f = request.files.get("side_image")
    if not f or not f.filename:
        flash("No image selected.", "error")
        return redirect(url_for("index"))
    data = f.read()
    if not data:
        flash("Selected file is empty.", "error")
        return redirect(url_for("index"))
    try:
        tiff = imaging.to_tiff_bytes(data)
    except Exception as e:
        flash(f"Could not read image: {e}", "error")
        return redirect(url_for("index"))

    if side == "back":
        back_path = os.path.join(LIBRARY_DIR, f"{im['sha256']}_back.tif")
        with open(back_path, "wb") as fh:
            fh.write(tiff)
        db.set_image_paths(image_id, back_path=back_path)
        flash(f"Attached back side to image #{image_id}.", "ok")
    else:  # replace / set the front side (front is the dedup key)
        new_sha = imaging.sha256_bytes(tiff)
        clash = db.find_image_by_hash(new_sha, _uid())
        if clash and clash["id"] != image_id:
            flash("That front image already exists in the library.", "error")
            if request.headers.get("X-Requested-With"):
                return ("exists", 409)
            return redirect(url_for("index"))
        new_front = os.path.join(LIBRARY_DIR, f"{new_sha}_front.tif")
        with open(new_front, "wb") as fh:
            fh.write(tiff)
        old_front = im.get("front_path")
        db.set_image_paths(image_id, front_path=new_front, sha256=new_sha)
        if old_front and old_front != new_front:
            _cleanup_files(old_front)
        flash(f"Replaced front image of #{image_id}.", "ok")

    if request.headers.get("X-Requested-With"):
        return ("", 200)
    return redirect(url_for("index"))


@app.route("/image/<int:image_id>/swap_sides", methods=["POST"])
def swap_sides(image_id):
    """Swap which stored image is the front vs the back — for items whose
    sides came in reversed. Serving is by explicit path, so swapping the two
    paths is all that's needed (the dedup key is left as-is)."""
    im = _owned_image(image_id)
    if not im:
        abort(404)
    front, back = im.get("front_path"), im.get("back_path")
    if not back:
        flash(f"Image #{image_id} has no back side to swap.", "error")
        if request.headers.get("X-Requested-With"):
            return ("no back", 400)
        return redirect(url_for("index"))
    db.set_image_paths(image_id, front_path=back, back_path=front)
    flash(f"Swapped front / back of image #{image_id}.", "ok")
    if request.headers.get("X-Requested-With"):
        return ("", 200)
    return redirect(url_for("index"))


@app.route("/image/<int:image_id>/delete", methods=["POST"])
def delete_image(image_id):
    im = _owned_image(image_id)
    db.delete_image(image_id)
    if im:
        _cleanup_files(im["front_path"], im.get("back_path"))
    flash(f"Deleted image #{image_id} from library.", "ok")
    return redirect(url_for("index"))


@app.route("/preview/<int:image_id>/<side>")
def preview(image_id, side):
    im = _owned_image(image_id)
    if not im:
        abort(404)
    path = im["front_path"] if side == "front" else im.get("back_path")
    if not path or not os.path.exists(path):
        abort(404)
    try:
        png = imaging.to_png_preview(path)
    except Exception:
        abort(404)
    return send_file(io.BytesIO(png), mimetype="image/png")


# ---------------------------------------------------------------------------
# batch plan (transaction model)
# ---------------------------------------------------------------------------
def _txn_cap():
    """The plan's fixed transaction count (set by Build plan), or None if the
    user is building freely without composing."""
    v = session.get("txn_cap")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _at_txn_cap(plan):
    """True if the plan already has as many transactions as Build plan fixed."""
    cap = _txn_cap()
    return cap is not None and len(plan) >= cap


_TARGET_TYPES = ("check", "statement", "fullpage", "stub", "correspondence")


def _compose_target(ti):
    """Max documents by item_type for a SPECIFIC transaction `ti`, from the last
    Build plan. Each transaction can have its own target mix. Returns {} when that
    transaction has no positive target (so it accepts any mix / is free)."""
    if not session.get("txn_cap"):
        return {}
    targets = session.get("compose_targets") or []
    if not (0 <= ti < len(targets)):
        return {}
    t = targets[ti] or {}
    target = {k: int(t.get(k, 0) or 0) for k in _TARGET_TYPES}
    return target if sum(target.values()) > 0 else {}


def _has_compose_targets():
    """True if the current plan defines ANY positive per-transaction target."""
    if not session.get("txn_cap"):
        return False
    for t in (session.get("compose_targets") or []):
        if any(int(v or 0) for v in (t or {}).values()):
            return True
    return False


def _room_for(plan, ti, item_type, exclude=None):
    """How many more of `item_type` may be added into transaction `ti` per that
    transaction's composed target. Returns a large number when no target is set
    (free build). `exclude` is an optional (image_id, type, amount) tuple
    identifying a specific grouped row to omit from the count (row editing)."""
    target = _compose_target(ti)
    if not target:
        return 10 ** 9
    limit = target.get(item_type, 0)
    have = 0
    if 0 <= ti < len(plan):
        for e in plan[ti]:
            if e.get("item_type") != item_type:
                continue
            if exclude and (e.get("image_id") == exclude[0]
                            and e.get("item_type") == exclude[1]
                            and str(e.get("amount")) == str(exclude[2])):
                continue
            have += 1
    return max(0, limit - have)


def _first_txn_with_room(plan, item_type):
    """First transaction index that can still take another `item_type` (per the
    composed target), or None if every transaction is full for that type."""
    for i in range(len(plan)):
        if _room_for(plan, i, item_type) > 0:
            return i
    return None


def _auto_dest(plan, item_type):
    """Which transaction an auto-distributed image goes into:
      - with a per-type target: the first transaction with room for that type;
      - without a target (free mix): the transaction holding the fewest documents
        (round-robin balance) so images spread evenly instead of piling in #1.
    Returns None if no transaction can take it."""
    if not plan:
        return None
    if _has_compose_targets():
        return _first_txn_with_room(plan, item_type)
    return min(range(len(plan)), key=lambda i: len(plan[i]))


def _target_ti(plan):
    """The 'target_ti' form field: which transaction a library add goes into.
    Returns 'auto' (distribute across transactions), a valid transaction index,
    or None for 'new transaction' (empty / missing / out-of-range)."""
    raw = request.form.get("target_ti", "")
    if raw == "auto":
        return "auto"
    if raw in (None, "", "new"):
        return None
    try:
        ti = int(raw)
    except (TypeError, ValueError):
        return None
    return ti if 0 <= ti < len(plan) else None


@app.route("/plan/add", methods=["POST"])
def plan_add():
    image_id = int(request.form["image_id"])
    im = _owned_image(image_id)
    it = im["item_type"] if im else "unknown"
    amt = (im.get("amount") if im else "0.00") or "0.00"
    plan = _plan()
    ti = _target_ti(plan)
    if ti == "auto":
        dest = _auto_dest(plan, it)
        if dest is None:
            flash(f"No transaction has room for another {it} (per your compose target). "
                  f"Change the compose counts and rebuild to allow more.", "error")
            return redirect(url_for("index"))
        _save_plan(batchmod.add_to_transaction(plan, dest, image_id, it, amt, qty=1))
        flash(f"Added #{image_id} into Transaction {dest + 1}.", "ok")
    elif ti is None:
        if _at_txn_cap(plan):
            flash(f"This plan is fixed to {_txn_cap()} transaction(s). Add this image "
                  f"INTO an existing transaction (use the “into” selector), or rebuild "
                  f"the plan with a higher count.", "error")
            return redirect(url_for("index"))
        _save_plan(batchmod.add_single(plan, image_id, it, amt))
    else:
        if _room_for(plan, ti, it) <= 0:
            tgt = _compose_target(ti).get(it, 0)
            flash(f"Transaction {ti + 1} is at its target of {tgt} {it}(s). "
                  f"Change that transaction's compose counts and rebuild to allow more.", "error")
            return redirect(url_for("index"))
        _save_plan(batchmod.add_to_transaction(plan, ti, image_id, it, amt, qty=1))
        flash(f"Added #{image_id} into Transaction {ti + 1}.", "ok")
    return redirect(url_for("index"))


@app.route("/plan/add_many", methods=["POST"])
def plan_add_many():
    ids = request.form.getlist("image_id")
    plan = _plan()
    ti = _target_ti(plan)
    cap = _txn_cap()
    n = skipped = 0
    for sid in ids:
        try:
            image_id = int(sid)
        except (TypeError, ValueError):
            continue
        im = _owned_image(image_id)
        if not im:
            continue
        it = im["item_type"]
        amt = im.get("amount") or "0.00"
        if ti == "auto":
            # spread each image into the first transaction with room for its type
            dest = _auto_dest(plan, it)
            if dest is None:
                skipped += 1
                continue
            plan = batchmod.add_to_transaction(plan, dest, image_id, it, amt, qty=1)
        elif ti is None:
            # each selected image would start a NEW transaction — respect the cap
            if cap is not None and len(plan) >= cap:
                skipped += 1
                continue
            plan = batchmod.add_single(plan, image_id, it, amt)
        else:
            # respect the per-transaction target for this type
            if _room_for(plan, ti, it) <= 0:
                skipped += 1
                continue
            plan = batchmod.add_to_transaction(plan, ti, image_id, it, amt, qty=1)
        n += 1
    _save_plan(plan)
    if not n and skipped and ti == "auto":
        flash("No transaction has room for the selected image type(s) per your compose "
              "target. Change the compose counts and rebuild to allow more.", "error")
    elif not n and skipped and ti is None:
        flash(f"This plan is fixed to {cap} transaction(s). Add images INTO existing "
              f"transactions (use the “into” selector), or rebuild with a higher count.", "error")
    elif not n and skipped:
        flash(f"Transaction {ti + 1} is already at its target document mix. "
              f"Change the compose counts and rebuild to allow more.", "error")
    elif not n:
        flash("No images selected.", "error")
    elif ti == "auto":
        msg = f"Distributed {n} image(s) across the transactions."
        if skipped:
            msg += f" {skipped} skipped — no transaction had room for their type."
        flash(msg, "ok" if not skipped else "warn")
    elif ti is None:
        msg = f"Added {n} image(s), each as its own transaction."
        if skipped:
            msg += f" {skipped} skipped — plan is capped at {cap} transaction(s)."
        flash(msg, "ok" if not skipped else "warn")
    else:
        msg = f"Added {n} image(s) into Transaction {ti + 1}."
        if skipped:
            msg += f" {skipped} skipped — Transaction {ti + 1} is at its target mix."
        flash(msg, "ok" if not skipped else "warn")
    return redirect(url_for("index"))


_COMPOSE_FIELD = {"check": "check", "statement": "statement", "fullpage": "fullpage",
                  "stub": "stub", "correspondence": "corr"}


def _mix_str(row):
    """'1 check + 1 statement' from a target row dict; 'any' if all zero."""
    parts = [(row.get("check", 0), "check"), (row.get("statement", 0), "statement"),
             (row.get("fullpage", 0), "full-page"), (row.get("stub", 0), "stub"),
             (row.get("correspondence", 0), "corr")]
    s = " + ".join(f"{v} {lbl}" for v, lbl in parts if v)
    return s or "any"


@app.route("/plan/compose", methods=["POST"])
def plan_compose():
    """Build a plan of N transactions. In UNIFORM mode every transaction gets the
    same target mix; in PER-TRANSACTION mode each transaction has its own mix, so
    the user can build e.g. T1 = 1 check + 1 statement, T2 = 3 checks + 1 statement,
    T3 = 2 correspondence — full liberty over the batch shape."""
    def _int(v, default=0):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return default

    n_txn = _int(request.form.get("n_transactions"), 0)
    if n_txn < 1:
        flash("Set how many transactions (at least 1).", "error")
        return redirect(url_for("index"))
    batch_type = request.form.get("batch_type") or "Singles"
    _sync_registered_batch_types()
    allowed = rules.rule_for(batch_type).get("allowed") or set()
    uniform = request.form.get("uniform", "1") != "0"

    # Build the per-transaction target matrix (a list of {type: count} dicts).
    rows = []
    if uniform:
        one = {t: _int(request.form.get("n_" + f)) for t, f in _COMPOSE_FIELD.items()}
        rows = [dict(one) for _ in range(n_txn)]
    else:
        lists = {t: request.form.getlist("row_" + f) for t, f in _COMPOSE_FIELD.items()}
        for i in range(n_txn):
            rows.append({t: (_int(lists[t][i]) if i < len(lists[t]) else 0)
                         for t in _COMPOSE_FIELD})
    # a batch type only allows certain document types — force the rest to 0
    for r in rows:
        for t in _COMPOSE_FIELD:
            if t not in allowed:
                r[t] = 0

    session["txn_cap"] = n_txn                 # plan is fixed to n_txn transactions
    session["compose_targets"] = rows          # per-transaction target mix
    session["compose_cfg"] = {"n_transactions": n_txn, "uniform": uniform, "rows": rows}
    _cfg = session.get("batch_cfg", {})
    _cfg["batch_type"] = batch_type
    session["batch_cfg"] = _cfg
    session.modified = True

    # batch-type mismatch is ADVISORY — warn but still build. Validate against the
    # heaviest row so a mismatch anywhere is surfaced once.
    maxrow = {f: max((r[t] for r in rows), default=0) for t, f in _COMPOSE_FIELD.items()}
    for e in rules.validate_composition(batch_type, maxrow):
        flash("Heads up: " + e, "warn")

    # Create the transaction STRUCTURE only — do NOT auto-pick images.
    _save_plan([[] for _ in range(n_txn)])
    if uniform:
        detail = f"target each: {_mix_str(rows[0])}"
    else:
        detail = "; ".join(f"T{i + 1} = {_mix_str(r)}" for i, r in enumerate(rows))
    flash(f"Created {n_txn} empty transaction(s) — {detail}. Now pick images from the "
          f"library and add them into each transaction (use the “into: Transaction N” "
          f"selector, or the “＋ add to this transaction” row).", "ok")
    return redirect(url_for("index"))


@app.route("/plan/new_txn", methods=["POST"])
def plan_new_txn():
    plan = _plan()
    if _at_txn_cap(plan):
        flash(f"This plan is fixed to {_txn_cap()} transaction(s). Remove one (✕), or "
              f"rebuild the plan with a higher count to add more.", "error")
        return redirect(url_for("index"))
    _save_plan(batchmod.add_empty_transaction(plan))
    return redirect(url_for("index"))


@app.route("/plan/add_to_txn", methods=["POST"])
def plan_add_to_txn():
    """Add an image (×qty) into a specific existing transaction — e.g. attach
    2 statements to the check already in transaction 1."""
    ti = int(request.form["ti"])
    image_id = int(request.form["image_id"])
    try:
        qty = max(1, int(request.form.get("qty", 1)))
    except (TypeError, ValueError):
        qty = 1
    im = _owned_image(image_id)
    if im:
        plan = _plan()
        it = im["item_type"]
        room = _room_for(plan, ti, it)
        if room <= 0:
            tgt = _compose_target(ti).get(it, 0)
            flash(f"Transaction {ti + 1} is at its target of {tgt} {it}(s). Change that "
                  f"transaction's compose counts and rebuild the plan to allow more.", "error")
            return redirect(url_for("index"))
        qty = min(qty, room)
        _save_plan(batchmod.add_to_transaction(
            plan, ti, image_id, it, im.get("amount") or "0.00", qty=qty))
    return redirect(url_for("index"))


@app.route("/plan/move_item", methods=["POST"])
def plan_move_item():
    """Reorder a document up/down within its transaction."""
    _save_plan(batchmod.move_group(
        _plan(),
        int(request.form["ti"]),
        int(request.form["image_id"]),
        request.form["o_type"],
        request.form["o_amount"],
        int(request.form["delta"]),
    ))
    return redirect(url_for("index"))


@app.route("/plan/multiply_txn", methods=["POST"])
def plan_multiply_txn():
    plan = batchmod.multiply_transaction(_plan(), int(request.form["ti"]),
                                         int(request.form["total"]))
    _save_plan(plan)
    # ×N deliberately changes the transaction count — keep the cap in sync so it
    # doesn't leave the plan stuck over a now-inconsistent limit.
    if session.get("txn_cap") is not None:
        session["txn_cap"] = len(plan)
        session.modified = True
    flash("Applied transaction multiply.", "ok")
    return redirect(url_for("index"))


@app.route("/plan/remove_txn", methods=["POST"])
def plan_remove_txn():
    _save_plan(batchmod.remove_transaction(_plan(), int(request.form["ti"])))
    return redirect(url_for("index"))


@app.route("/plan/group_set", methods=["POST"])
def plan_group_set():
    """Change a grouped row's type / amount / quantity (×N) in one transaction."""
    ti = int(request.form["ti"])
    image_id = int(request.form["image_id"])
    qty = request.form.get("qty")
    try:
        qty = int(qty) if qty not in (None, "") else None
    except ValueError:
        qty = None
    o_type = request.form["o_type"]
    o_amount = request.form["o_amount"]
    new_type = request.form.get("item_type")
    plan = _plan()
    # clamp the row so neither a ×N bump NOR a reclassification can push the
    # effective type over the composed target. Count everything of that type in
    # the transaction except this exact row (image + type + amount).
    eff_type = new_type or o_type
    cur = 0
    if 0 <= ti < len(plan):
        cur = sum(1 for e in plan[ti]
                  if e.get("image_id") == image_id and e.get("item_type") == o_type
                  and str(e.get("amount")) == str(o_amount))
    effective_qty = qty if qty is not None else cur   # None keeps the row's current count
    if effective_qty > 0:
        room = _room_for(plan, ti, eff_type, exclude=(image_id, o_type, o_amount))
        if effective_qty > room:
            tgt = _compose_target(ti).get(eff_type, 0)
            flash(f"Transaction {ti + 1} allows only {tgt} {eff_type}(s) per its compose "
                  f"target — kept at {max(room, 0)}. Rebuild with higher counts to allow more.", "warn")
            qty = max(room, 0)
    _save_plan(batchmod.group_set(
        plan, ti, image_id, o_type, o_amount,
        new_type=new_type,
        new_amount=request.form.get("amount"),
        qty=qty,
    ))
    return redirect(url_for("index"))


@app.route("/plan/move_txn", methods=["POST"])
def plan_move_txn():
    _save_plan(batchmod.move_transaction(_plan(), int(request.form["ti"]),
                                         int(request.form["delta"])))
    return redirect(url_for("index"))


@app.route("/plan/clear", methods=["POST"])
def plan_clear():
    _save_plan([])
    session.pop("txn_cap", None)          # clearing the plan drops the transaction cap
    session.pop("compose_targets", None)  # and its per-transaction targets
    # reset the compose form so transactions + all item counts show 0
    session["compose_cfg"] = {"n_transactions": 0, "uniform": True, "rows": []}
    session.modified = True
    flash("Cleared batch plan.", "ok")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------
def _resolve_plan():
    """Common pre-generate logic: resolve plan entries to (image, entry) pairs."""
    plan = _plan()
    if not plan:
        return None, None, None, "Batch plan is empty — compose or add items first."
    transactions = []
    for txn in plan:
        resolved_txn = []
        for e in txn:
            im = _owned_image(e["image_id"])
            if im:
                im = dict(im, item_type=e.get("item_type") or im["item_type"])
                resolved_txn.append((im, e))
        if resolved_txn:
            transactions.append(resolved_txn)
    if not transactions:
        return None, None, None, "None of the planned images still exist in the library."
    resolved = [pair for txn in transactions for pair in txn]
    return plan, transactions, resolved, None


def _build_meta():
    """Read batch config from the form and persist to session."""
    meta = {
        "batch_type": request.form.get("batch_type") or "Singles",
        "biller": request.form.get("biller") or "SANITY_LBX_05 LBX",
        "site": request.form.get("site") or "Worldwide",
        "subsite": request.form.get("subsite") or "",
        "batch_date": request.form.get("batch_date") or time.strftime("%Y/%m/%d"),
        "file_date": request.form.get("file_date") or "",
        "lockbox": request.form.get("lockbox") or "",
        "test_file": "Y" if request.form.get("test_file") else "N",
        "operator": request.form.get("operator") or "QA Operator",
    }
    include_ct = bool(request.form.get("control_totals"))
    fmt = request.form.get("format", "3xml")
    session["batch_cfg"] = {**meta, "control_totals": include_ct, "format": fmt}
    session.modified = True
    return meta, include_ct


def _validate_and_warn(meta, transactions, resolved):
    """Validate batch rules. Batch-type / item-type / structure mismatches are
    ADVISORY (warn, still generate) — an operator may knowingly build off-spec.
    The ONLY hard block is a check/correspondence with no MICR line, since that
    produces a structurally invalid file. Returns (errors, warnings)."""
    terrors, twarnings = rules.validate_transactions(meta["batch_type"], transactions)
    # demote type/structure errors to warnings — the user can proceed
    warnings = list(terrors) + list(twarnings)
    errors = []
    missing = []
    for txn in transactions:
        for im, _e in txn:
            # only CHECKS require a MICR line; correspondence / stubs / statements
            # / full-page docs are non-payment documents and need no MICR.
            if im["item_type"] == "check" and not (im.get("raw_micr") or "").strip():
                if im["id"] not in missing:
                    missing.append(im["id"])
    if missing:
        ids = ", #".join(str(i) for i in missing)
        aihint = ("use '✨ AI read MICR' on the image, or "
                  if llm.available() else "")
        errors.append(
            f"{len(missing)} check(s) have no MICR line: #{ids}. "
            f"A batch can't be generated without a check's MICR — {aihint}open Edit and "
            f"fill the Scanline field, then generate again.")
        return errors, warnings
    return [], warnings


@app.route("/generate", methods=["POST"])
def generate():
    fmt = request.form.get("format", "3xml")
    builtin_formats = {"3xml", "lbxxml", "78tml", "bundle", "oxz"}
    custom_base = None if fmt in builtin_formats else _base_for_format(fmt)
    if fmt not in builtin_formats and not custom_base:
        flash(f"Unknown format '{fmt}'. Choose .3xml, .LBXxml, .78tml, All + bundle, "
              f"or a registered format.", "error")
        return redirect(url_for("index"))
    _, transactions, resolved, err = _resolve_plan()
    if err:
        flash(err, "error")
        return redirect(url_for("index"))
    meta, include_ct = _build_meta()

    _sync_registered_batch_types()
    errors, warnings = _validate_and_warn(meta, transactions, resolved)
    if errors:
        for e in errors:
            flash("Cannot generate: " + e, "error")
        return redirect(url_for("index"))
    for w in warnings:
        flash(w, "warn")

    ts = time.strftime("%Y%m%d_%H%M%S")
    base = f"batch_{ts}"
    xml3_path = lbx_path = tml78_path = bundle_path = oxz_path = None
    custom_ext = custom_path = None

    if custom_base:
        # a registered custom extension — generate with the schema it resembles
        if custom_base == "lbxxml":
            content = xmlgen.generate_lbxxml(meta, resolved)
        else:
            content = xmlgen.generate_3xml(meta, transactions, include_control_totals=include_ct)
        custom_ext = fmt
        custom_path = os.path.join(OUTPUT_DIR, f"{base}.{fmt}")
        with open(custom_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    elif fmt == "3xml":
        content = xmlgen.generate_3xml(meta, transactions, include_control_totals=include_ct)
        xml3_path = os.path.join(OUTPUT_DIR, base + ".3xml")
        with open(xml3_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    elif fmt == "lbxxml":
        content = xmlgen.generate_lbxxml(meta, resolved)
        lbx_path = os.path.join(OUTPUT_DIR, base + ".LBXxml")
        with open(lbx_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    elif fmt == "78tml":
        content = xmlgen.generate_78tml(meta, transactions, include_control_totals=include_ct)
        tml78_path = os.path.join(OUTPUT_DIR, base + ".78tml")
        with open(tml78_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    elif fmt == "oxz":
        # synthetic scanner batch: zip of TIFFs + a generated .oxi index
        oxz_bytes = oxi_writer.build_oxz(meta, transactions)
        oxz_path = os.path.join(OUTPUT_DIR, base + ".oxz")
        with open(oxz_path, "wb") as fh:
            fh.write(oxz_bytes)
    elif fmt == "bundle":
        xml3 = xmlgen.generate_3xml(meta, transactions, include_control_totals=include_ct)
        lbx = xmlgen.generate_lbxxml(meta, resolved)
        tml78 = xmlgen.generate_78tml(meta, transactions, include_control_totals=include_ct)
        xml3_path = os.path.join(OUTPUT_DIR, base + ".3xml")
        lbx_path = os.path.join(OUTPUT_DIR, base + ".LBXxml")
        tml78_path = os.path.join(OUTPUT_DIR, base + ".78tml")
        bundle_path = os.path.join(OUTPUT_DIR, base + "_bundle.zip")
        for path, data in ((xml3_path, xml3), (lbx_path, lbx), (tml78_path, tml78)):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(data)
        with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(base + ".3xml", xml3)
            z.writestr(base + ".LBXxml", lbx)
            z.writestr(base + ".78tml", tml78)
            seen = set()
            for im, _e in resolved:
                for key in ("front_path", "back_path"):
                    p = im.get(key)
                    if p and os.path.exists(p) and p not in seen:
                        seen.add(p)
                        z.write(p, arcname="images/" + os.path.basename(p))

    ct = xmlgen.compute_control_totals(resolved)
    control_total = f"CR {ct['cc']}/{ct['ca']:.2f} · DR {ct['dc']}/{ct['da']:.2f}"
    db.insert_batch(
        {**meta, "format": fmt, "control_total": control_total,
         "xml3_path": xml3_path, "lbxxml_path": lbx_path,
         "tml78_path": tml78_path, "bundle_path": bundle_path,
         "oxz_path": oxz_path,
         "custom_ext": custom_ext, "custom_path": custom_path},
        [{"image_id": im["id"], "item_type": im["item_type"], "amount": e.get("amount")}
         for im, e in resolved],
        user_id=_uid(),
    )
    label = {"3xml": ".3xml", "lbxxml": ".LBXxml", "78tml": ".78tml",
             "bundle": "all formats", "oxz": ".oxz scanner batch"}.get(fmt, f".{fmt}")
    flash(f"Generated {label}: {len(transactions)} txn / {len(resolved)} doc(s).", "ok")
    return redirect(url_for("index"))


@app.route("/batch/<int:batch_id>/discard", methods=["POST"])
def discard_batch(batch_id):
    b = _owned_batch(batch_id)
    is_ajax = bool(request.headers.get("X-Requested-With"))
    if b:
        _cleanup_files(b.get("xml3_path"), b.get("lbxxml_path"),
                       b.get("tml78_path"), b.get("bundle_path"),
                       b.get("oxz_path"), b.get("custom_path"))
        db.delete_batch(batch_id)
    # AJAX discard updates the card in place + shows a transient toast, so we do
    # NOT flash — a session flash would otherwise linger and re-appear on the next
    # page refresh. Only the no-JS fallback flashes.
    if is_ajax:
        return ("", 200) if b else ("Batch not found", 404)
    flash(f"Discarded batch #{batch_id}." if b else "Batch not found.",
          "ok" if b else "error")
    return redirect(url_for("index"))


def _batch_file_path(b, kind):
    """Resolve a batch's output path for a given kind (built-in or custom ext)."""
    path = {"3xml": b["xml3_path"], "lbxxml": b["lbxxml_path"],
            "78tml": b.get("tml78_path"), "bundle": b["bundle_path"],
            "oxz": b.get("oxz_path")}.get(kind)
    if path is None and b.get("custom_ext") and kind == b["custom_ext"]:
        path = b.get("custom_path")
    return path


@app.route("/download/<int:batch_id>/<kind>")
def download(batch_id, kind):
    b = _owned_batch(batch_id)
    if not b:
        abort(404)
    path = _batch_file_path(b, kind)
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.route("/view/<int:batch_id>/<kind>")
def view_file(batch_id, kind):
    """Render generated file content in-browser (like Notepad++)."""
    b = _owned_batch(batch_id)
    if not b:
        abort(404)
    path = _batch_file_path(b, kind)
    if kind in ("bundle", "oxz"):
        path = None  # binary zips — not viewable in-browser
    if not path or not os.path.exists(path):
        abort(404)
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    return render_template("view_file.html", filename=os.path.basename(path),
                           kind=kind, batch_id=batch_id, content=content)


# ---------------------------------------------------------------------------
# Claude (LLM) endpoints — each degrades gracefully if AI isn't configured
# ---------------------------------------------------------------------------
@app.route("/image/<int:image_id>/ai_classify", methods=["POST"])
def ai_classify(image_id):
    """Ask Claude to classify a document from its OCR text (smart classify)."""
    if not llm.available():
        return jsonify({"ok": False, "error": "Claude is not configured. "
                        "Add ANTHROPIC_API_KEY to a .env file."}), 200
    im = _owned_image(image_id)
    if not im:
        return jsonify({"ok": False, "error": "Image not found."}), 404
    read = ocr.read_image(im["front_path"])
    result = llm.classify_document(read.get("ocr_text"), im.get("raw_micr"))
    if result.get("_error"):
        return jsonify({"ok": False, "error": result["_error"]}), 200
    return jsonify({"ok": True, "item_type": result.get("item_type"),
                    "confidence": result.get("confidence"),
                    "reasoning": result.get("reasoning")}), 200


@app.route("/image/<int:image_id>/ai_micr", methods=["POST"])
def ai_micr(image_id):
    """Ask Claude (vision) to read the MICR from a check image and save it."""
    if not llm.available():
        return jsonify({"ok": False, "error": "Claude is not configured. "
                        "Add ANTHROPIC_API_KEY to a .env file."}), 200
    im = _owned_image(image_id)
    if not im:
        return jsonify({"ok": False, "error": "Image not found."}), 404
    result = llm.read_micr_from_image(im["front_path"])
    if result.get("_error"):
        return jsonify({"ok": False, "error": result["_error"]}), 200
    micr = (result.get("micr") or "").strip()
    if micr:
        db.update_image(image_id, {"raw_micr": micr})   # save so the batch gate clears
    return jsonify({"ok": True, "micr": micr,
                    "readable": bool(result.get("readable")) and bool(micr)}), 200


@app.route("/library/ai_fill_micr", methods=["POST"])
def ai_fill_micr():
    """Bulk: read + save MICR for every check/correspondence image that's missing one."""
    if not llm.available():
        return jsonify({"ok": False, "error": "Claude is not configured. "
                        "Add ANTHROPIC_API_KEY to a .env file."}), 200
    filled = failed = 0
    updated = []
    for im in db.list_images(_uid()):
        if im["item_type"] != "check":          # only checks need a MICR line
            continue
        if (im.get("raw_micr") or "").strip():
            continue
        result = llm.read_micr_from_image(im["front_path"])
        micr = (result.get("micr") or "").strip() if not result.get("_error") else ""
        if micr:
            db.update_image(im["id"], {"raw_micr": micr})
            filled += 1
            updated.append({"id": im["id"], "micr": micr})
        else:
            failed += 1
    return jsonify({"ok": True, "filled": filled, "failed": failed, "updated": updated}), 200


def _normalize_amount(raw):
    """'$1,234.5' -> '1234.50'. Returns '' if nothing numeric is present."""
    s = "".join(ch for ch in (raw or "") if ch.isdigit() or ch == ".")
    # keep only the first dot if the model returned several
    if s.count(".") > 1:
        head, _, tail = s.partition(".")
        s = head + "." + tail.replace(".", "")
    if not s or s == ".":
        return ""
    try:
        return f"{float(s):.2f}"
    except ValueError:
        return ""


@app.route("/image/<int:image_id>/ai_amount", methods=["POST"])
def ai_amount(image_id):
    """Ask Claude (vision) to read the payment amount from a document image and save it."""
    if not llm.available():
        return jsonify({"ok": False, "error": "Claude is not configured. "
                        "Add ANTHROPIC_API_KEY to a .env file."}), 200
    im = _owned_image(image_id)
    if not im:
        return jsonify({"ok": False, "error": "Image not found."}), 404
    result = llm.read_amount_from_image(im["front_path"])
    if result.get("_error"):
        return jsonify({"ok": False, "error": result["_error"]}), 200
    amt = _normalize_amount(result.get("amount"))
    if amt:
        db.update_image(image_id, {"amount": amt})
    return jsonify({"ok": True, "amount": amt,
                    "readable": bool(result.get("readable")) and bool(amt)}), 200


@app.route("/library/ai_fill_amounts", methods=["POST"])
def ai_fill_amounts():
    """Bulk: read + save the amount for every check image that doesn't have one yet."""
    if not llm.available():
        return jsonify({"ok": False, "error": "Claude is not configured. "
                        "Add ANTHROPIC_API_KEY to a .env file."}), 200
    filled = failed = 0
    total = 0.0
    updated = []
    for im in db.list_images(_uid()):
        if im["item_type"] != "check":              # only checks carry a dollar amount
            continue
        cur = (im.get("amount") or "").strip()
        if cur and cur not in ("0", "0.0", "0.00"):  # skip ones already priced
            continue
        result = llm.read_amount_from_image(im["front_path"])
        amt = _normalize_amount(result.get("amount")) if not result.get("_error") else ""
        if amt and amt != "0.00":
            db.update_image(im["id"], {"amount": amt})
            filled += 1
            total += float(amt)
            updated.append({"id": im["id"], "amount": amt})
        else:
            failed += 1
    return jsonify({"ok": True, "filled": filled, "failed": failed,
                    "total": f"{total:.2f}", "updated": updated}), 200


@app.route("/batch/<int:batch_id>/review", methods=["POST"])
def ai_review(batch_id):
    """Ask Claude to QA-review a generated batch file before download."""
    if not llm.available():
        return jsonify({"ok": False, "error": "Claude is not configured. "
                        "Add ANTHROPIC_API_KEY to a .env file."}), 200
    b = _owned_batch(batch_id)
    if not b:
        return jsonify({"ok": False, "error": "Batch not found."}), 404
    # review the first viewable (text) output the batch has
    for kind in ("3xml", "lbxxml", "78tml"):
        path = _batch_file_path(b, kind)
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            review = llm.review_batch(kind, content)
            return jsonify({"ok": True, "kind": kind, "review": review}), 200
    if b.get("custom_path") and os.path.exists(b["custom_path"]):
        with open(b["custom_path"], "r", encoding="utf-8") as fh:
            content = fh.read()
        review = llm.review_batch(b.get("custom_ext") or "file", content)
        return jsonify({"ok": True, "kind": b.get("custom_ext"), "review": review}), 200
    return jsonify({"ok": False, "error": "No text-based file to review "
                    "(bundles / .oxz are binary)."}), 200


# ---------------------------------------------------------------------------
# Format templates page — a reference window showing what each output file the
# app can create looks like (.3xml / .LBXxml / .78tml / .oxz + learned formats)
# ---------------------------------------------------------------------------
def _sample_meta():
    return {"batch_type": "Singles", "biller": "SAMPLE_BILLER LBX", "site": "Worldwide",
            "subsite": "New York", "batch_date": "2026/07/19", "lockbox": "678245",
            "operator": "QA Operator", "test_file": "Y"}


def _sample_transactions():
    """One transaction: a check + a statement, with no image bytes (so the
    template shows structure without a huge base64 blob)."""
    check = {"id": "IMG_CHECK", "item_type": "check",
             "raw_micr": "T121000248T O0155130O 4121176507",
             "front_path": None, "back_path": None, "amount": "200.00", "extra_json": "{}"}
    stmt = {"id": "IMG_STMT", "item_type": "statement", "raw_micr": "",
            "front_path": None, "back_path": None, "amount": "0.00", "extra_json": "{}"}
    return [[(check, {"item_type": "check", "amount": "200.00"}),
             (stmt, {"item_type": "statement", "amount": "0.00"})]]


def _format_templates():
    meta, txns = _sample_meta(), _sample_transactions()
    flat = [pair for txn in txns for pair in txn]
    out = []

    def _safe(fn):
        try:
            return fn()
        except Exception as e:
            return f"(could not render template: {type(e).__name__}: {e})"

    out.append({"name": ".3xml", "kind": "XML", "builtin": True,
                "desc": "Nested file > cashLetter > batch > transaction. Checks become "
                        "<payment> (with rawMicr); statements/stubs become <statement>/<stub> "
                        "(with ocrLine). Control totals at batch/cashLetter/file level.",
                "content": _safe(lambda: xmlgen.generate_3xml(meta, txns, include_control_totals=True))})
    out.append({"name": ".LBXxml", "kind": "XML", "builtin": True,
                "desc": "Flat PascalCase File > Batch > Item structure. Numeric ItemType "
                        "codes (check=1, stub=2, statement=3, correspondence=4), Dataline holds "
                        "the scanline, Debit/Credit counts at the end.",
                "content": _safe(lambda: xmlgen.generate_lbxxml(meta, flat))})
    out.append({"name": ".78tml", "kind": "XML", "builtin": True,
                "desc": "Like .3xml, but fileDate / siteName / subsiteName are injected inside "
                        "each payment's Front <image> element instead of at file/batch level.",
                "content": _safe(lambda: xmlgen.generate_78tml(meta, txns, include_control_totals=True))})

    def _oxi():
        import io as _io
        import zipfile as _zf
        blob = oxi_writer.build_oxz(meta, txns)
        z = _zf.ZipFile(_io.BytesIO(blob))
        name = next((n for n in z.namelist() if n.lower().endswith(".oxi")), None)
        return z.read(name).decode("utf-8", errors="replace") if name else "(no .oxi)"
    out.append({"name": ".oxz", "kind": "ZIP + XML", "builtin": True,
                "desc": "A ZIP archive containing one .oxi XML index plus the page TIFFs "
                        "(Batch > Transaction > Group > Page > Image). Shown below: the .oxi index.",
                "content": _safe(_oxi)})

    # persisted learned formats
    for r in _registered_formats():
        base = r["base"]
        content = _safe(lambda b=base: xmlgen.generate_lbxxml(meta, flat) if b == "lbxxml"
                        else xmlgen.generate_3xml(meta, txns, include_control_totals=True))
        out.append({"name": f".{r['ext']}", "kind": f"learned → .{base}", "builtin": False,
                    "desc": f"Learned format — generated using the .{base} schema and written "
                            f"with the .{r['ext']} extension.",
                    "content": content})
    return out


@app.route("/templates")
def format_templates():
    try:
        css_v = int(os.path.getmtime(os.path.join(app.static_folder, "style.css")))
    except OSError:
        css_v = 0
    _sync_registered_batch_types()
    # every transaction type with its (possibly learned) rule, for the reference
    group_label = {"check_plus_stub": "1 check + 1 remittance", "one_each": "1 doc",
                   "multiples": "multiple docs"}
    txn_types = []
    for name in _all_batch_types():
        r = _all_batch_rules().get(name) or rules.rule_for(name)
        txn_types.append({
            "name": name,
            "allowed": ", ".join(sorted(r.get("allowed") or [])),
            "group": group_label.get(r.get("group"), r.get("group") or ""),
            "confirmed": bool(r.get("confirmed")),
        })
    return render_template("templates.html", css_v=css_v, templates=_format_templates(),
                           txn_types=txn_types)


# ---------------------------------------------------------------------------
# Check-List files — a dedicated section that archives whole Check-List .3xml
# files (auto-saved on import + manual upload), so they can be reused and the
# user doesn't recreate them wrong. Validated so only real Check-List files land.
# ---------------------------------------------------------------------------
def _safe_name(name):
    keep = "-_. "
    base = os.path.basename(name or "file.3xml")
    cleaned = "".join(c for c in base if c.isalnum() or c in keep).strip()
    return cleaned or "file.3xml"


def _checklist_info(data):
    """Return (is_checklist, info) for a batch file's bytes."""
    if not xml_import.looks_like_xml(data):
        return False, None
    info = xml_import.analyze_batch_structure(data)
    ttype = ((info or {}).get("transaction_type") or "").strip().lower()
    return ttype == "check-list", info


def _save_reference_file(txn_type, filename, data, note=""):
    """Write the raw bytes to REFERENCE_DIR and catalogue them. Deduped by
    content hash — returns (row_id, created_bool)."""
    sha = hashlib.sha256(data).hexdigest()
    existing = db.find_reference_by_hash(sha)
    if existing:
        return existing["id"], False
    info = xml_import.analyze_batch_structure(data) or {}
    counts = info.get("item_counts") or []
    safe = _safe_name(filename)
    disk_name = f"{sha[:12]}_{safe}"
    path = os.path.join(REFERENCE_DIR, disk_name)
    ensure_dirs()
    with open(path, "wb") as fh:
        fh.write(data)
    rid = db.add_reference_file(txn_type, safe, path, sha, len(data),
                                len(counts), sum(counts), note)
    return rid, True


def _maybe_save_checklist(filename, data):
    """Auto-archive a file in the Check-List section if it IS a Check-List file."""
    is_cl, _ = _checklist_info(data)
    if not is_cl:
        return
    rid, created = _save_reference_file("Check-List", filename, data)
    if created:
        flash(f"Saved '{_safe_name(filename)}' to the Check-List files section for reuse.", "ok")


@app.route("/checklist")
def checklist():
    files = db.list_reference_files("Check-List")
    try:
        css_v = int(os.path.getmtime(os.path.join(app.static_folder, "style.css")))
    except OSError:
        css_v = 0
    return render_template("checklist.html", css_v=css_v, files=files)


@app.route("/checklist/upload", methods=["POST"])
def checklist_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("checklist"))
    data = f.read()
    if not data:
        flash("Uploaded file is empty.", "error")
        return redirect(url_for("checklist"))
    is_cl, info = _checklist_info(data)
    if not is_cl:
        ttype = ((info or {}).get("transaction_type") or "unknown")
        flash(f"'{f.filename}' has transactionType '{ttype}', not 'Check-List'. "
              f"Only Check-List .3xml files belong in this section.", "error")
        return redirect(url_for("checklist"))
    rid, created = _save_reference_file("Check-List", f.filename,
                                        data, request.form.get("note", "").strip())
    if created:
        _learn_rule_from_batch(data)     # keep the Check-List rule fresh too
        flash(f"Saved Check-List file '{_safe_name(f.filename)}'.", "ok")
    else:
        flash("That exact file is already saved here.", "ok")
    return redirect(url_for("checklist"))


@app.route("/checklist/<int:rid>/download")
def checklist_download(rid):
    rf = db.get_reference_file(rid)
    if not rf or not os.path.exists(rf["path"]):
        abort(404)
    return send_file(rf["path"], as_attachment=True,
                     download_name=rf["filename"] or "checklist.3xml")


@app.route("/checklist/<int:rid>/update", methods=["POST"])
def checklist_update(rid):
    rf = db.get_reference_file(rid)
    if not rf:
        abort(404)
    fields = {}
    note = request.form.get("note")
    if note is not None:
        fields["note"] = note.strip()
    f = request.files.get("file")
    if f and f.filename:                             # optional replacement file
        data = f.read()
        is_cl, info = _checklist_info(data)
        if not data:
            flash("Replacement file is empty — kept the existing one.", "error")
        elif not is_cl:
            ttype = ((info or {}).get("transaction_type") or "unknown")
            flash(f"Replacement transactionType is '{ttype}', not 'Check-List' — "
                  f"kept the existing one.", "error")
        else:
            sha = hashlib.sha256(data).hexdigest()
            safe = _safe_name(f.filename)
            path = os.path.join(REFERENCE_DIR, f"{sha[:12]}_{safe}")
            with open(path, "wb") as fh:
                fh.write(data)
            if rf["path"] != path and os.path.exists(rf["path"]):
                try:
                    os.remove(rf["path"])
                except OSError:
                    pass
            counts = (info or {}).get("item_counts") or []
            fields.update({"filename": safe, "path": path, "sha256": sha,
                           "size": len(data), "txn_count": len(counts),
                           "doc_count": sum(counts)})
            _learn_rule_from_batch(data)
    if fields:
        db.update_reference_file(rid, fields)
        flash("Check-List file updated.", "ok")
    return redirect(url_for("checklist"))


@app.route("/checklist/<int:rid>/delete", methods=["POST"])
def checklist_delete(rid):
    rf = db.get_reference_file(rid)
    if rf:
        if rf.get("path") and os.path.exists(rf["path"]):
            try:
                os.remove(rf["path"])
            except OSError:
                pass
        db.delete_reference_file(rid)
        flash("Check-List file deleted.", "ok")
    return redirect(url_for("checklist"))


@app.route("/checklist/<int:rid>/load", methods=["POST"])
def checklist_load(rid):
    """Pull the images out of a saved Check-List file into the Check-List
    workspace's own separate pool (not the main library) — the images are
    only usable to compose a batch on the /cl page from here on."""
    rf = db.get_reference_file(rid)
    if not rf or not os.path.exists(rf["path"]):
        abort(404)
    with open(rf["path"], "rb") as fh:
        data = fh.read()
    result, err = cl_routes.ingest_checklist_file(rf["filename"], data, _uid())
    if err:
        flash(err, "error")
    else:
        flash(f"Loaded '{rf['filename']}' into the Check-List workspace: "
              f"{result['added']} new image(s), {result['dup']} duplicate(s) skipped.", "ok")
    return redirect(url_for("cl.index"))


def _open_browser_when_ready(port):
    """Open the browser only once the server is actually accepting connections,
    so the user never lands on a 'can't reach the site' error and has to refresh."""
    import socket
    import threading
    import webbrowser

    def _wait_then_open():
        url = f"http://127.0.0.1:{port}/"
        for _ in range(200):                       # poll up to ~20s
            with socket.socket() as s:
                s.settimeout(0.25)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    try:
                        webbrowser.open(url)
                    except Exception:
                        pass
                    return
            time.sleep(0.1)

    threading.Thread(target=_wait_then_open, daemon=True).start()


if __name__ == "__main__":
    ensure_dirs()
    db.init_db()
    host = os.environ.get("HOST", "127.0.0.1")   # set HOST=0.0.0.0 to share on a LAN
    port = int(os.environ.get("PORT", "5000"))
    # Debug/reloader OFF by default: the reloader starts the app twice (slower to
    # bind) and exposes the Werkzeug debugger. Turn it on with FLASK_DEBUG=1.
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    # Browser auto-open is OPT-IN (OPEN_BROWSER=1). It's off by default because it
    # opens a NEW tab every launch — if you already keep a tab on the app, that's
    # a duplicate. The fast startup below is what actually fixes the old "error on
    # first open, refresh to fix" problem, so just reload your existing tab.
    if os.environ.get("OPEN_BROWSER", "0") == "1" and (
            not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
        _open_browser_when_ready(port)

    shown = "127.0.0.1" if host in ("127.0.0.1", "localhost") else host
    print(f" * ImageToConfig {version.APP_VERSION} ready at http://{shown}:{port}/  "
          f"(set OPEN_BROWSER=1 to auto-open a tab)")
    app.run(host=host, port=port, debug=debug, use_reloader=debug)
