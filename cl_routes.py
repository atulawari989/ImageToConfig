"""Check-List workspace (/cl/*) — a standalone clone of the main page's
upload -> compose -> generate -> download engine, pointed at a fully separate
image + batch pool (checklist_images / checklist_batches / checklist_batch_items,
CL_LIBRARY_DIR / CL_OUTPUT_DIR, and cl_* session keys).

Built as a Blueprint registered on the same Flask app as everything in app.py,
so it inherits the exact same login gate (app.before_request) and the exact
same user model (_uid via session), but shares NO session state, NO database
rows, and NO files on disk with the main page. Batch type is fixed to
'Check-List' throughout — there is no batch-type picker here.

This file exists so the main app.py routes never need to be touched to add
Check-List-workspace behavior. See MEMORY / conversation history for why:
stability for a stakeholder demo was prioritized over a shared/parameterized
implementation.
"""
import io
import os
import re
import time
import zipfile

from flask import Blueprint, request, redirect, url_for, session, send_file, flash, abort, jsonify, render_template

from core import db, ocr, imaging, batch as batchmod, xmlgen, rules, xml_import, oxi_writer, llm
from core.config import CL_LIBRARY_DIR, CL_OUTPUT_DIR, ITEM_TYPE_LABELS, ensure_dirs

cl_bp = Blueprint("cl", __name__, url_prefix="/cl")

BATCH_TYPE = "Check-List"
CL_ITEM_TYPES = sorted(rules.rule_for(BATCH_TYPE)["allowed"])  # ["check","fullpage","statement","stub"]


# ---------------------------------------------------------------------------
# ownership / session helpers (mirrors app.py's _uid/_owns/_owned_image, but
# reads the checklist_images/checklist_batches tables instead)
# ---------------------------------------------------------------------------
def _uid():
    return session.get("user_id")


def _owns(row):
    if not row:
        return False
    try:
        return int(row.get("user_id")) == int(_uid())
    except (TypeError, ValueError):
        return False


def _owned_image(image_id):
    im = db.get_checklist_image(image_id)
    return im if _owns(im) else None


def _owned_batch(batch_id):
    b = db.get_checklist_batch(batch_id)
    return b if _owns(b) else None


def _cleanup_files(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# perceptual-dedup helpers. Check-List-workspace-only (not shared with the
# main page's dedup): a visually-similar (ahash) pair is only a real duplicate
# when there's no content evidence against it — for checks that's the MICR
# routing+account (ocr.micr_key, same as the main library); for everything
# else (stub/statement/full-page — documents that never carry a MICR line at
# all) it's a fingerprint built from the digit content actually printed on the
# page (account numbers, reference numbers, dollar amounts), read via plain
# OCR. Two documents sharing a visual template but listing different accounts
# or amounts are provably different once their fingerprints disagree.
# ---------------------------------------------------------------------------
_ACCOUNT_RUN = re.compile(r"\d{4,}")
_AMOUNT_RUN = re.compile(r"\d[\d,]*\.\d{2}")


def _statement_key(ocr_text, min_groups=2):
    """Comparable content fingerprint for a non-check document, built from the
    account/reference numbers and dollar amounts in its OCR'd text. Returns
    None if too little digit content was read to trust a match confidently —
    callers must then fall back to visual similarity alone, same as when a
    check's MICR can't be read."""
    if not ocr_text:
        return None
    # collapse a single OCR-inserted space INSIDE one number (a real artifact
    # seen splitting one long account number in two on some scans), but leave
    # real separators (newlines, multi-space table gaps) alone so distinct
    # fields don't get merged into each other.
    collapsed = re.sub(r"(?<=\d) (?=\d)", "", ocr_text)
    accounts = _ACCOUNT_RUN.findall(collapsed)
    amounts = _AMOUNT_RUN.findall(ocr_text)   # decimals are untouched by the collapse above
    groups = accounts + amounts
    if len(groups) < min_groups:
        return None
    return tuple(groups)


def _content_key(item_type, raw_micr, ocr_text):
    """Dispatch to the right content fingerprint for this document's type.
    Tagged with a type prefix so a check's key can never coincidentally equal
    a statement's key."""
    if item_type == "check":
        k = ocr.micr_key(raw_micr)
        return ("micr", k) if k else None
    k = _statement_key(ocr_text)
    return ("text", k) if k else None


def _known_records(user_id=None):
    uid = user_id if user_id is not None else _uid()
    out = []
    for im in db.list_checklist_images(uid):
        ck = _content_key(im.get("item_type"), im.get("raw_micr"), im.get("ocr_text"))
        out.append((im["id"], imaging.ahash(im["front_path"]), ck))
    return out


def _is_dup(phash, content_key, known):
    for _i, h, k in known:
        if h is None or phash is None:
            continue
        if imaging.hamming(phash, h) > imaging.DUP_THRESHOLD:
            continue
        if content_key is None and k is None:
            return True
        if content_key is not None and k is not None and content_key == k:
            return True
    return False


def _classify_for_checklist(front_path, raw_micr, requested_type=None):
    """Best-effort item-type classification, restricted to Check-List's allowed
    set (check/stub/statement/fullpage). Returns (item_type, ocr_text) or
    (None, ocr_text) if nothing in the allowed set could be determined.

    `requested_type` lets the uploader explicitly tag the document (used when
    OCR's guess is wrong) — honored only if it's itself an allowed type.
    """
    read = ocr.read_image(front_path)
    guess = read.get("item_type")
    ocr_text = read.get("ocr_text")
    if requested_type and requested_type in CL_ITEM_TYPES:
        return requested_type, ocr_text
    if guess in CL_ITEM_TYPES:
        return guess, ocr_text
    # a confidently-read routing number is strong evidence this is a check,
    # even if the OCR keyword classifier missed (e.g. sparse/blank check face)
    if ocr.micr_key(raw_micr) is not None:
        return "check", ocr_text
    return None, ocr_text


# ---------------------------------------------------------------------------
# main workspace page
# ---------------------------------------------------------------------------
@cl_bp.route("/")
def index():
    images = db.list_checklist_images(_uid())
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
    targets = session.get("cl_compose_targets") or []
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

    pool_counts = {"check": 0, "statement": 0, "fullpage": 0, "stub": 0}
    for im in images:
        if im["item_type"] in pool_counts:
            pool_counts[im["item_type"]] += 1

    try:
        from flask import current_app
        css_v = int(os.path.getmtime(os.path.join(current_app.static_folder, "style.css")))
    except OSError:
        css_v = 0
    cfg = session.get("cl_batch_cfg", {})
    compose_cfg = session.get("cl_compose_cfg", {})
    rule = rules.rule_for(BATCH_TYPE)
    return render_template(
        "checklist_workspace.html",
        css_v=css_v,
        images=images,
        plan_txns=plan_txns,
        plan_count=batchmod.count_items(plan),
        plan_total=f"{plan_total:.2f}",
        pool_counts=pool_counts,
        batches=db.list_checklist_batches(_uid()),
        item_types=CL_ITEM_TYPES,
        item_type_labels=ITEM_TYPE_LABELS,
        batch_type=BATCH_TYPE,
        rule=rule,
        tesseract_ok=ocr.tesseract_available(),
        ai_status=llm.status(),
        today=time.strftime("%Y/%m/%d"),
        cfg=cfg,
        compose_cfg=compose_cfg,
        txn_cap=_txn_cap(),
        registered_formats=db.list_formats(),
    )


@cl_bp.route("/state/save", methods=["POST"])
def state_save():
    which = request.form.get("_form")
    if which == "config":
        cfg = session.get("cl_batch_cfg", {})
        for k in ("biller", "site", "subsite", "batch_date", "file_date",
                  "lockbox", "operator", "format"):
            if request.form.get(k) is not None:
                cfg[k] = request.form.get(k)
        cfg["test_file"] = "Y" if request.form.get("test_file") else "N"
        cfg["control_totals"] = bool(request.form.get("control_totals"))
        session["cl_batch_cfg"] = cfg
    session.modified = True
    return ("", 204)


# ---------------------------------------------------------------------------
# ingestion (raw images + whole Check-List .3xml files) — exact + perceptual
# dedup, same as the main library, strictly scoped to Check-List item types
# ---------------------------------------------------------------------------
@cl_bp.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("images")
    requested_type = request.form.get("item_type") or None
    known = _known_records()
    added = dup = rejected = 0
    for f in files:
        if not f or not f.filename:
            continue
        data = f.read()
        if not data:
            continue
        micr = ocr.read_micr(data)
        try:
            sha, front_path, _ = imaging.store_bytes(data, library_dir=CL_LIBRARY_DIR)
        except Exception as e:
            flash(f"Could not read {f.filename}: {e}", "error")
            continue
        if db.find_checklist_image_by_hash(sha, _uid()):
            dup += 1
            continue
        # classify BEFORE the dup check — the content fingerprint depends on
        # whether this is a check (MICR-based) or not (OCR-text-based)
        item_type, ocr_text = _classify_for_checklist(front_path, micr, requested_type)
        if item_type is None:
            _cleanup_files(front_path)
            rejected += 1
            continue
        phash = imaging.ahash(front_path)
        ckey = _content_key(item_type, micr, ocr_text)
        if _is_dup(phash, ckey, known):
            _cleanup_files(front_path)
            dup += 1
            continue
        rec = {"user_id": _uid(), "sha256": sha, "original_filename": f.filename,
               "front_path": front_path, "back_path": None, "source": "upload",
               "item_type": item_type, "raw_micr": micr, "ocr_text": ocr_text}
        new_id, created = db.insert_checklist_image(rec)
        if created:
            known.append((new_id, phash, ckey))
            added += 1
        else:
            dup += 1
    msg = f"Uploaded: {added} new, {dup} duplicate(s) skipped"
    if rejected:
        msg += f", {rejected} rejected (not a recognizable check/stub/statement/full-page)"
    flash(msg + ".", "ok" if not rejected else "warn")
    return redirect(url_for("cl.index"))


def ingest_checklist_file(filename, data, user_id):
    """Extract every document from a Check-List .3xml/.LBXxml file into the
    Check-List pool. Shared with app.py's checklist_load() (the archive page's
    "Load images" button) so both entry points behave identically."""
    try:
        kind, docs = xml_import.parse_batch_xml(data)
    except Exception as e:
        return None, f"Couldn't parse '{filename}': {e}"
    if not docs:
        return None, f"No document images found in '{filename}'."
    known = _known_records(user_id)
    added = dup = 0
    for d in docs:
        try:
            sha, front_path, back_path = imaging.store_bytes(
                d["front"], d.get("back"), library_dir=CL_LIBRARY_DIR)
        except Exception:
            continue
        if db.find_checklist_image_by_hash(sha, user_id):
            dup += 1
            continue
        phash = imaging.ahash(front_path)
        scan = d.get("scanline")
        if scan and sum(c.isdigit() for c in scan) < 4:
            scan = None
        item_type = d["item_type"] if d["item_type"] in CL_ITEM_TYPES else None
        if item_type is None and ocr.micr_key(scan) is not None:
            item_type = "check"
        if item_type is None:
            _cleanup_files(front_path, back_path)
            continue
        # non-checks carry no scanline at all — read the page's own printed
        # content (account numbers, amounts) so near-duplicate detection has
        # something to check against instead of visual similarity alone
        ocr_text = ocr.ocr_text(front_path) if item_type != "check" else None
        ckey = _content_key(item_type, scan, ocr_text)
        if _is_dup(phash, ckey, known):
            _cleanup_files(front_path, back_path)
            dup += 1
            continue
        rec = {"user_id": user_id, "sha256": sha, "original_filename": filename,
               "front_path": front_path, "back_path": back_path,
               "source": "import", "item_type": item_type, "raw_micr": scan,
               "ocr_text": ocr_text}
        new_id, created = db.insert_checklist_image(rec)
        if created:
            known.append((new_id, phash, ckey))
            added += 1
        else:
            dup += 1
    return {"kind": kind, "added": added, "dup": dup}, None


@cl_bp.route("/import", methods=["POST"])
def import_file():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("cl.index"))
    data = f.read()
    if not data:
        flash("Uploaded file is empty.", "error")
        return redirect(url_for("cl.index"))
    if not xml_import.looks_like_xml(data):
        flash(f"'{f.filename}' isn't a .3xml/.LBXxml file.", "error")
        return redirect(url_for("cl.index"))
    info = xml_import.analyze_batch_structure(data) or {}
    ttype = (info.get("transaction_type") or "").strip().lower()
    if ttype != "check-list":
        flash(f"'{f.filename}' has transactionType '{info.get('transaction_type') or 'unknown'}', "
              f"not 'Check-List'. Only Check-List files belong in this workspace.", "error")
        return redirect(url_for("cl.index"))
    result, err = ingest_checklist_file(f.filename, data, _uid())
    if err:
        flash(err, "error")
    else:
        flash(f"Imported '{f.filename}': {result['added']} new image(s), "
              f"{result['dup']} duplicate(s) skipped.", "ok")
    return redirect(url_for("cl.index"))


@cl_bp.route("/library/dedup", methods=["POST"])
def library_dedup():
    images = sorted(db.list_checklist_images(_uid()), key=lambda r: r["id"])
    kept = []
    removed = 0
    for im in images:
        h = imaging.ahash(im["front_path"])
        k = _content_key(im.get("item_type"), im.get("raw_micr"), im.get("ocr_text"))
        if _is_dup(h, k, [(None, kh, kk) for kh, kk in kept]):
            db.delete_checklist_image(im["id"])
            _cleanup_files(im["front_path"], im.get("back_path"))
            removed += 1
        else:
            kept.append((h, k))
    flash(f"Removed {removed} duplicate image(s); {len(kept)} unique kept.", "ok")
    return redirect(url_for("cl.index"))


# ---------------------------------------------------------------------------
# library item editing
# ---------------------------------------------------------------------------
@cl_bp.route("/image/<int:image_id>/edit", methods=["POST"])
def edit_image(image_id):
    im = _owned_image(image_id)
    if not im:
        abort(404)
    new_type = request.form.get("item_type", im["item_type"])
    if new_type not in CL_ITEM_TYPES:
        flash(f"'{new_type}' isn't a Check-List document type — kept as '{im['item_type']}'.", "error")
        new_type = im["item_type"]
    fields = {
        "item_type": new_type,
        "amount": request.form.get("amount", "").strip() or None,
        "raw_micr": request.form.get("raw_micr", "").strip() or None,
    }
    db.update_checklist_image(image_id, fields)
    flash(f"Saved item #{image_id}.", "ok")
    return redirect(url_for("cl.index"))


@cl_bp.route("/image/<int:image_id>/reread_micr", methods=["POST"])
def reread_micr(image_id):
    im = _owned_image(image_id)
    if not im:
        return {"ok": False, "micr": ""}, 404
    return {"ok": True, "micr": ocr.read_micr(im["front_path"]) or ""}


@cl_bp.route("/image/<int:image_id>/rotate", methods=["POST"])
def rotate_image(image_id):
    im = _owned_image(image_id)
    if not im:
        abort(404)
    side = request.form.get("side", "front")
    direction = request.form.get("direction", "right")
    path = im["front_path"] if side == "front" else im.get("back_path")
    if not path or not os.path.exists(path):
        flash("No image to rotate.", "error")
        return redirect(url_for("cl.index"))
    new_sha = imaging.rotate_file(path, direction)
    if side == "front" and new_sha and not db.find_checklist_image_by_hash(new_sha, _uid()):
        db.update_checklist_sha(image_id, new_sha)
    if request.headers.get("X-Requested-With"):
        return ("", 204)
    flash(f"Rotated image #{image_id} ({side}).", "ok")
    return redirect(url_for("cl.index"))


@cl_bp.route("/image/<int:image_id>/attach_side", methods=["POST"])
def attach_side(image_id):
    im = _owned_image(image_id)
    if not im:
        abort(404)
    side = (request.form.get("side") or "back").lower()
    f = request.files.get("side_image")
    if not f or not f.filename:
        flash("No image selected.", "error")
        return redirect(url_for("cl.index"))
    data = f.read()
    if not data:
        flash("Selected file is empty.", "error")
        return redirect(url_for("cl.index"))
    try:
        tiff = imaging.to_tiff_bytes(data)
    except Exception as e:
        flash(f"Could not read image: {e}", "error")
        return redirect(url_for("cl.index"))

    if side == "back":
        back_path = os.path.join(CL_LIBRARY_DIR, f"{im['sha256']}_back.tif")
        with open(back_path, "wb") as fh:
            fh.write(tiff)
        db.set_checklist_image_paths(image_id, back_path=back_path)
        flash(f"Attached back side to image #{image_id}.", "ok")
    else:
        new_sha = imaging.sha256_bytes(tiff)
        clash = db.find_checklist_image_by_hash(new_sha, _uid())
        if clash and clash["id"] != image_id:
            flash("That front image already exists in the library.", "error")
            if request.headers.get("X-Requested-With"):
                return ("exists", 409)
            return redirect(url_for("cl.index"))
        new_front = os.path.join(CL_LIBRARY_DIR, f"{new_sha}_front.tif")
        with open(new_front, "wb") as fh:
            fh.write(tiff)
        old_front = im.get("front_path")
        db.set_checklist_image_paths(image_id, front_path=new_front, sha256=new_sha)
        if old_front and old_front != new_front:
            _cleanup_files(old_front)
        flash(f"Replaced front image of #{image_id}.", "ok")

    if request.headers.get("X-Requested-With"):
        return ("", 200)
    return redirect(url_for("cl.index"))


@cl_bp.route("/image/<int:image_id>/swap_sides", methods=["POST"])
def swap_sides(image_id):
    im = _owned_image(image_id)
    if not im:
        abort(404)
    front, back = im.get("front_path"), im.get("back_path")
    if not back:
        flash(f"Image #{image_id} has no back side to swap.", "error")
        if request.headers.get("X-Requested-With"):
            return ("no back", 400)
        return redirect(url_for("cl.index"))
    db.set_checklist_image_paths(image_id, front_path=back, back_path=front)
    flash(f"Swapped front / back of image #{image_id}.", "ok")
    if request.headers.get("X-Requested-With"):
        return ("", 200)
    return redirect(url_for("cl.index"))


@cl_bp.route("/image/<int:image_id>/delete", methods=["POST"])
def delete_image(image_id):
    im = _owned_image(image_id)
    db.delete_checklist_image(image_id)
    if im:
        _cleanup_files(im["front_path"], im.get("back_path"))
    flash(f"Deleted image #{image_id} from library.", "ok")
    return redirect(url_for("cl.index"))


@cl_bp.route("/preview/<int:image_id>/<side>")
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
# batch plan (transaction model) — identical mechanics to the main page's,
# against cl_* session keys and the checklist_images pool
# ---------------------------------------------------------------------------
def _plan():
    plan = session.get("cl_plan", [])
    if not isinstance(plan, list) or any(not isinstance(t, list) for t in plan):
        return []
    return plan


def _save_plan(plan):
    session["cl_plan"] = plan
    session.modified = True


def _txn_cap():
    v = session.get("cl_txn_cap")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _at_txn_cap(plan):
    cap = _txn_cap()
    return cap is not None and len(plan) >= cap


_TARGET_TYPES = ("check", "statement", "fullpage", "stub")


def _compose_target(ti):
    if not session.get("cl_txn_cap"):
        return {}
    targets = session.get("cl_compose_targets") or []
    if not (0 <= ti < len(targets)):
        return {}
    t = targets[ti] or {}
    target = {k: int(t.get(k, 0) or 0) for k in _TARGET_TYPES}
    return target if sum(target.values()) > 0 else {}


def _has_compose_targets():
    if not session.get("cl_txn_cap"):
        return False
    for t in (session.get("cl_compose_targets") or []):
        if any(int(v or 0) for v in (t or {}).values()):
            return True
    return False


def _room_for(plan, ti, item_type, exclude=None):
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
    for i in range(len(plan)):
        if _room_for(plan, i, item_type) > 0:
            return i
    return None


def _auto_dest(plan, item_type):
    if not plan:
        return None
    if _has_compose_targets():
        return _first_txn_with_room(plan, item_type)
    return min(range(len(plan)), key=lambda i: len(plan[i]))


def _target_ti(plan):
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


_COMPOSE_FIELD = {"check": "check", "statement": "statement", "fullpage": "fullpage", "stub": "stub"}


def _mix_str(row):
    parts = [(row.get("check", 0), "check"), (row.get("statement", 0), "statement"),
             (row.get("fullpage", 0), "full-page"), (row.get("stub", 0), "stub")]
    s = " + ".join(f"{v} {lbl}" for v, lbl in parts if v)
    return s or "any"


@cl_bp.route("/plan/add", methods=["POST"])
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
            return redirect(url_for("cl.index"))
        _save_plan(batchmod.add_to_transaction(plan, dest, image_id, it, amt, qty=1))
        flash(f"Added #{image_id} into Transaction {dest + 1}.", "ok")
    elif ti is None:
        if _at_txn_cap(plan):
            flash(f"This plan is fixed to {_txn_cap()} transaction(s). Add this image "
                  f"INTO an existing transaction (use the “into” selector), or rebuild "
                  f"the plan with a higher count.", "error")
            return redirect(url_for("cl.index"))
        _save_plan(batchmod.add_single(plan, image_id, it, amt))
    else:
        if _room_for(plan, ti, it) <= 0:
            tgt = _compose_target(ti).get(it, 0)
            flash(f"Transaction {ti + 1} is at its target of {tgt} {it}(s). "
                  f"Change that transaction's compose counts and rebuild to allow more.", "error")
            return redirect(url_for("cl.index"))
        _save_plan(batchmod.add_to_transaction(plan, ti, image_id, it, amt, qty=1))
        flash(f"Added #{image_id} into Transaction {ti + 1}.", "ok")
    return redirect(url_for("cl.index"))


@cl_bp.route("/plan/add_many", methods=["POST"])
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
            dest = _auto_dest(plan, it)
            if dest is None:
                skipped += 1
                continue
            plan = batchmod.add_to_transaction(plan, dest, image_id, it, amt, qty=1)
        elif ti is None:
            if cap is not None and len(plan) >= cap:
                skipped += 1
                continue
            plan = batchmod.add_single(plan, image_id, it, amt)
        else:
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
    return redirect(url_for("cl.index"))


@cl_bp.route("/plan/compose", methods=["POST"])
def plan_compose():
    def _int(v, default=0):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return default

    n_txn = _int(request.form.get("n_transactions"), 0)
    if n_txn < 1:
        flash("Set how many transactions (at least 1).", "error")
        return redirect(url_for("cl.index"))
    allowed = rules.rule_for(BATCH_TYPE)["allowed"]
    uniform = request.form.get("uniform", "1") != "0"

    rows = []
    if uniform:
        one = {t: _int(request.form.get("n_" + f)) for t, f in _COMPOSE_FIELD.items()}
        rows = [dict(one) for _ in range(n_txn)]
    else:
        lists = {t: request.form.getlist("row_" + f) for t, f in _COMPOSE_FIELD.items()}
        for i in range(n_txn):
            rows.append({t: (_int(lists[t][i]) if i < len(lists[t]) else 0)
                         for t in _COMPOSE_FIELD})
    for r in rows:
        for t in _COMPOSE_FIELD:
            if t not in allowed:
                r[t] = 0

    session["cl_txn_cap"] = n_txn
    session["cl_compose_targets"] = rows
    session["cl_compose_cfg"] = {"n_transactions": n_txn, "uniform": uniform, "rows": rows}
    session.modified = True

    maxrow = {f: max((r[t] for r in rows), default=0) for t, f in _COMPOSE_FIELD.items()}
    for e in rules.validate_composition(BATCH_TYPE, maxrow):
        flash("Heads up: " + e, "warn")

    _save_plan([[] for _ in range(n_txn)])
    if uniform:
        detail = f"target each: {_mix_str(rows[0])}"
    else:
        detail = "; ".join(f"T{i + 1} = {_mix_str(r)}" for i, r in enumerate(rows))
    flash(f"Created {n_txn} empty transaction(s) — {detail}. Now pick images from the "
          f"library and add them into each transaction.", "ok")
    return redirect(url_for("cl.index"))


@cl_bp.route("/plan/new_txn", methods=["POST"])
def plan_new_txn():
    plan = _plan()
    if _at_txn_cap(plan):
        flash(f"This plan is fixed to {_txn_cap()} transaction(s). Remove one (✕), or "
              f"rebuild the plan with a higher count to add more.", "error")
        return redirect(url_for("cl.index"))
    _save_plan(batchmod.add_empty_transaction(plan))
    return redirect(url_for("cl.index"))


@cl_bp.route("/plan/add_to_txn", methods=["POST"])
def plan_add_to_txn():
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
            return redirect(url_for("cl.index"))
        qty = min(qty, room)
        _save_plan(batchmod.add_to_transaction(
            plan, ti, image_id, it, im.get("amount") or "0.00", qty=qty))
    return redirect(url_for("cl.index"))


@cl_bp.route("/plan/move_item", methods=["POST"])
def plan_move_item():
    _save_plan(batchmod.move_group(
        _plan(),
        int(request.form["ti"]),
        int(request.form["image_id"]),
        request.form["o_type"],
        request.form["o_amount"],
        int(request.form["delta"]),
    ))
    return redirect(url_for("cl.index"))


@cl_bp.route("/plan/multiply_txn", methods=["POST"])
def plan_multiply_txn():
    plan = batchmod.multiply_transaction(_plan(), int(request.form["ti"]),
                                         int(request.form["total"]))
    _save_plan(plan)
    if session.get("cl_txn_cap") is not None:
        session["cl_txn_cap"] = len(plan)
        session.modified = True
    flash("Applied transaction multiply.", "ok")
    return redirect(url_for("cl.index"))


@cl_bp.route("/plan/remove_txn", methods=["POST"])
def plan_remove_txn():
    _save_plan(batchmod.remove_transaction(_plan(), int(request.form["ti"])))
    return redirect(url_for("cl.index"))


@cl_bp.route("/plan/group_set", methods=["POST"])
def plan_group_set():
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
    if new_type and new_type not in CL_ITEM_TYPES:
        new_type = o_type   # ignore an invalid type rather than corrupting the plan
    plan = _plan()
    eff_type = new_type or o_type
    cur = 0
    if 0 <= ti < len(plan):
        cur = sum(1 for e in plan[ti]
                  if e.get("image_id") == image_id and e.get("item_type") == o_type
                  and str(e.get("amount")) == str(o_amount))
    effective_qty = qty if qty is not None else cur
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
    return redirect(url_for("cl.index"))


@cl_bp.route("/plan/move_txn", methods=["POST"])
def plan_move_txn():
    _save_plan(batchmod.move_transaction(_plan(), int(request.form["ti"]),
                                         int(request.form["delta"])))
    return redirect(url_for("cl.index"))


@cl_bp.route("/plan/clear", methods=["POST"])
def plan_clear():
    _save_plan([])
    session.pop("cl_txn_cap", None)
    session.pop("cl_compose_targets", None)
    session["cl_compose_cfg"] = {"n_transactions": 0, "uniform": True, "rows": []}
    session.modified = True
    flash("Cleared batch plan.", "ok")
    return redirect(url_for("cl.index"))


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------
def _resolve_plan():
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
    meta = {
        "batch_type": BATCH_TYPE,
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
    session["cl_batch_cfg"] = {**meta, "control_totals": include_ct, "format": fmt}
    session.modified = True
    return meta, include_ct


def _validate_and_warn(meta, transactions, resolved):
    terrors, twarnings = rules.validate_transactions(meta["batch_type"], transactions)
    warnings = list(terrors) + list(twarnings)
    errors = []
    missing = []
    for txn in transactions:
        for im, _e in txn:
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


@cl_bp.route("/generate", methods=["POST"])
def generate():
    fmt = request.form.get("format", "3xml")
    builtin_formats = {"3xml", "lbxxml", "78tml", "bundle", "oxz"}
    custom_base = None if fmt in builtin_formats else _base_for_format(fmt)
    if fmt not in builtin_formats and not custom_base:
        flash(f"Unknown format '{fmt}'. Choose .3xml, .LBXxml, .78tml, All + bundle, "
              f"or a registered format.", "error")
        return redirect(url_for("cl.index"))
    _, transactions, resolved, err = _resolve_plan()
    if err:
        flash(err, "error")
        return redirect(url_for("cl.index"))
    meta, include_ct = _build_meta()

    errors, warnings = _validate_and_warn(meta, transactions, resolved)
    if errors:
        for e in errors:
            flash("Cannot generate: " + e, "error")
        return redirect(url_for("cl.index"))
    for w in warnings:
        flash(w, "warn")

    ts = time.strftime("%Y%m%d_%H%M%S")
    base = f"cl_batch_{ts}"
    xml3_path = lbx_path = tml78_path = bundle_path = oxz_path = None
    custom_ext = custom_path = None

    if custom_base:
        if custom_base == "lbxxml":
            content = xmlgen.generate_lbxxml(meta, resolved)
        else:
            content = xmlgen.generate_3xml(meta, transactions, include_control_totals=include_ct)
        custom_ext = fmt
        custom_path = os.path.join(CL_OUTPUT_DIR, f"{base}.{fmt}")
        with open(custom_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    elif fmt == "3xml":
        content = xmlgen.generate_3xml(meta, transactions, include_control_totals=include_ct)
        xml3_path = os.path.join(CL_OUTPUT_DIR, base + ".3xml")
        with open(xml3_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    elif fmt == "lbxxml":
        content = xmlgen.generate_lbxxml(meta, resolved)
        lbx_path = os.path.join(CL_OUTPUT_DIR, base + ".LBXxml")
        with open(lbx_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    elif fmt == "78tml":
        content = xmlgen.generate_78tml(meta, transactions, include_control_totals=include_ct)
        tml78_path = os.path.join(CL_OUTPUT_DIR, base + ".78tml")
        with open(tml78_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    elif fmt == "oxz":
        oxz_bytes = oxi_writer.build_oxz(meta, transactions)
        oxz_path = os.path.join(CL_OUTPUT_DIR, base + ".oxz")
        with open(oxz_path, "wb") as fh:
            fh.write(oxz_bytes)
    elif fmt == "bundle":
        xml3 = xmlgen.generate_3xml(meta, transactions, include_control_totals=include_ct)
        lbx = xmlgen.generate_lbxxml(meta, resolved)
        tml78 = xmlgen.generate_78tml(meta, transactions, include_control_totals=include_ct)
        xml3_path = os.path.join(CL_OUTPUT_DIR, base + ".3xml")
        lbx_path = os.path.join(CL_OUTPUT_DIR, base + ".LBXxml")
        tml78_path = os.path.join(CL_OUTPUT_DIR, base + ".78tml")
        bundle_path = os.path.join(CL_OUTPUT_DIR, base + "_bundle.zip")
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
    db.insert_checklist_batch(
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
    return redirect(url_for("cl.index"))


def _base_for_format(fmt):
    for r in db.list_formats():
        if r["ext"].lower() == fmt.lower():
            return r["base"]
    return None


def _batch_file_path(b, kind):
    path = {"3xml": b["xml3_path"], "lbxxml": b["lbxxml_path"],
            "78tml": b.get("tml78_path"), "bundle": b["bundle_path"],
            "oxz": b.get("oxz_path")}.get(kind)
    if path is None and b.get("custom_ext") and kind == b["custom_ext"]:
        path = b.get("custom_path")
    return path


@cl_bp.route("/batch/<int:batch_id>/discard", methods=["POST"])
def discard_batch(batch_id):
    b = _owned_batch(batch_id)
    is_ajax = bool(request.headers.get("X-Requested-With"))
    if b:
        _cleanup_files(b.get("xml3_path"), b.get("lbxxml_path"),
                       b.get("tml78_path"), b.get("bundle_path"),
                       b.get("oxz_path"), b.get("custom_path"))
        db.delete_checklist_batch(batch_id)
    if is_ajax:
        return ("", 200) if b else ("Batch not found", 404)
    flash(f"Discarded batch #{batch_id}." if b else "Batch not found.",
          "ok" if b else "error")
    return redirect(url_for("cl.index"))


@cl_bp.route("/download/<int:batch_id>/<kind>")
def download(batch_id, kind):
    b = _owned_batch(batch_id)
    if not b:
        abort(404)
    path = _batch_file_path(b, kind)
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@cl_bp.route("/view/<int:batch_id>/<kind>")
def view_file(batch_id, kind):
    b = _owned_batch(batch_id)
    if not b:
        abort(404)
    path = _batch_file_path(b, kind)
    if kind in ("bundle", "oxz"):
        path = None
    if not path or not os.path.exists(path):
        abort(404)
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    return render_template("cl_view_file.html", filename=os.path.basename(path),
                           kind=kind, batch_id=batch_id, content=content)


# ---------------------------------------------------------------------------
# Claude (LLM) endpoints — degrade gracefully if AI isn't configured, same as
# the main page's. These call the shared, stateless core.llm functions.
# ---------------------------------------------------------------------------
@cl_bp.route("/image/<int:image_id>/ai_classify", methods=["POST"])
def ai_classify(image_id):
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
    item_type = result.get("item_type")
    if item_type not in CL_ITEM_TYPES:
        return jsonify({"ok": True, "item_type": im["item_type"],
                        "confidence": result.get("confidence"),
                        "reasoning": (result.get("reasoning") or "") +
                        f" (Claude suggested '{item_type}', not a Check-List type — kept as-is.)"}), 200
    return jsonify({"ok": True, "item_type": item_type,
                    "confidence": result.get("confidence"),
                    "reasoning": result.get("reasoning")}), 200


@cl_bp.route("/image/<int:image_id>/ai_micr", methods=["POST"])
def ai_micr(image_id):
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
        db.update_checklist_image(image_id, {"raw_micr": micr})
    return jsonify({"ok": True, "micr": micr,
                    "readable": bool(result.get("readable")) and bool(micr)}), 200


@cl_bp.route("/library/ai_fill_micr", methods=["POST"])
def ai_fill_micr():
    if not llm.available():
        return jsonify({"ok": False, "error": "Claude is not configured. "
                        "Add ANTHROPIC_API_KEY to a .env file."}), 200
    filled = failed = 0
    updated = []
    for im in db.list_checklist_images(_uid()):
        if im["item_type"] != "check":
            continue
        if (im.get("raw_micr") or "").strip():
            continue
        result = llm.read_micr_from_image(im["front_path"])
        micr = (result.get("micr") or "").strip() if not result.get("_error") else ""
        if micr:
            db.update_checklist_image(im["id"], {"raw_micr": micr})
            filled += 1
            updated.append({"id": im["id"], "micr": micr})
        else:
            failed += 1
    return jsonify({"ok": True, "filled": filled, "failed": failed, "updated": updated}), 200


def _normalize_amount(raw):
    s = "".join(ch for ch in (raw or "") if ch.isdigit() or ch == ".")
    if s.count(".") > 1:
        head, _, tail = s.partition(".")
        s = head + "." + tail.replace(".", "")
    if not s or s == ".":
        return ""
    try:
        return f"{float(s):.2f}"
    except ValueError:
        return ""


@cl_bp.route("/image/<int:image_id>/ai_amount", methods=["POST"])
def ai_amount(image_id):
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
        db.update_checklist_image(image_id, {"amount": amt})
    return jsonify({"ok": True, "amount": amt,
                    "readable": bool(result.get("readable")) and bool(amt)}), 200


@cl_bp.route("/library/ai_fill_amounts", methods=["POST"])
def ai_fill_amounts():
    if not llm.available():
        return jsonify({"ok": False, "error": "Claude is not configured. "
                        "Add ANTHROPIC_API_KEY to a .env file."}), 200
    filled = failed = 0
    total = 0.0
    updated = []
    for im in db.list_checklist_images(_uid()):
        if im["item_type"] != "check":
            continue
        cur = (im.get("amount") or "").strip()
        if cur and cur not in ("0", "0.0", "0.00"):
            continue
        result = llm.read_amount_from_image(im["front_path"])
        amt = _normalize_amount(result.get("amount")) if not result.get("_error") else ""
        if amt and amt != "0.00":
            db.update_checklist_image(im["id"], {"amount": amt})
            filled += 1
            total += float(amt)
            updated.append({"id": im["id"], "amount": amt})
        else:
            failed += 1
    return jsonify({"ok": True, "filled": filled, "failed": failed,
                    "total": f"{total:.2f}", "updated": updated}), 200


@cl_bp.route("/batch/<int:batch_id>/review", methods=["POST"])
def ai_review(batch_id):
    if not llm.available():
        return jsonify({"ok": False, "error": "Claude is not configured. "
                        "Add ANTHROPIC_API_KEY to a .env file."}), 200
    b = _owned_batch(batch_id)
    if not b:
        return jsonify({"ok": False, "error": "Batch not found."}), 404
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
