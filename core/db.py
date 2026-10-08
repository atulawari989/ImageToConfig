"""SQLite access layer: the image library and generated-batch history."""
import sqlite3
import json
import time

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    username          TEXT UNIQUE NOT NULL,
    password_hash     TEXT NOT NULL,
    is_admin          INTEGER DEFAULT 0,
    last_seen_version TEXT DEFAULT '',
    created_at        TEXT
);

CREATE TABLE IF NOT EXISTS images (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER,                     -- owner (per-user library)
    sha256            TEXT NOT NULL,
    original_filename TEXT,
    front_path        TEXT NOT NULL,
    back_path         TEXT,
    item_type         TEXT DEFAULT 'unknown',
    source            TEXT DEFAULT 'upload',       -- upload | oxz
    raw_micr          TEXT,
    routing           TEXT,
    account           TEXT,
    check_number      TEXT,
    amount            TEXT,
    ocr_text          TEXT,
    extra_json        TEXT DEFAULT '{}',           -- accountType, checkTransType, secCode, correspondenceType, ...
    uploaded_at       TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER,                         -- owner (per-user history)
    batch_type    TEXT,
    biller        TEXT,
    site          TEXT,
    batch_date    TEXT,
    lockbox       TEXT,
    test_file     TEXT DEFAULT 'Y',
    format        TEXT DEFAULT '3xml',
    item_count    INTEGER DEFAULT 0,
    control_total TEXT,
    created_at    TEXT,
    xml3_path     TEXT,
    lbxxml_path   TEXT,
    tml78_path    TEXT,
    bundle_path   TEXT,
    oxz_path      TEXT,
    custom_ext    TEXT,
    custom_path   TEXT
);

CREATE TABLE IF NOT EXISTS batch_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id  INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    sequence  INTEGER,
    image_id  INTEGER REFERENCES images(id),
    item_type TEXT,
    amount    TEXT
);

-- Learned output formats (a new extension the app can generate, mapped to a
-- base schema). Persisted so they survive across sessions / restarts.
CREATE TABLE IF NOT EXISTS output_formats (
    ext        TEXT PRIMARY KEY,
    base       TEXT,
    created_at TEXT
);

-- Learned batch/work types (from ingested OXZ / XML samples).
CREATE TABLE IF NOT EXISTS learned_batch_types (
    name       TEXT PRIMARY KEY,
    allowed    TEXT,      -- json list of allowed item types
    grp        TEXT,
    descr      TEXT,
    confirmed  INTEGER DEFAULT 0,   -- 1 = learned from a real .3xml structure
    created_at TEXT
);

-- Saved reference batch files, kept whole for future reuse (e.g. Check-List).
-- The raw file lives on disk at `path`; this row is the catalogue entry.
CREATE TABLE IF NOT EXISTS reference_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_type   TEXT,                 -- e.g. 'Check-List'
    filename   TEXT,                 -- original file name shown to the user
    path       TEXT NOT NULL,        -- on-disk location of the saved bytes
    sha256     TEXT,                 -- dedup key (skip re-saving identical files)
    size       INTEGER,
    txn_count  INTEGER,              -- transactions in the file
    doc_count  INTEGER,              -- documents in the file
    note       TEXT,                 -- user note
    created_at TEXT,
    updated_at TEXT
);

-- Check-List workspace (/cl/*): a fully separate image + batch pool from the
-- main `images`/`batches`/`batch_items` above. Same shape, deliberately
-- isolated tables — nothing here is ever read by the main app routes.
CREATE TABLE IF NOT EXISTS checklist_images (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER,
    sha256            TEXT NOT NULL,
    original_filename TEXT,
    front_path        TEXT NOT NULL,
    back_path         TEXT,
    item_type         TEXT DEFAULT 'unknown',
    source            TEXT DEFAULT 'upload',
    raw_micr          TEXT,
    routing           TEXT,
    account           TEXT,
    check_number      TEXT,
    amount            TEXT,
    ocr_text          TEXT,
    extra_json        TEXT DEFAULT '{}',
    uploaded_at       TEXT
);

CREATE TABLE IF NOT EXISTS checklist_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER,
    batch_type    TEXT DEFAULT 'Check-List',
    biller        TEXT,
    site          TEXT,
    batch_date    TEXT,
    lockbox       TEXT,
    test_file     TEXT DEFAULT 'Y',
    format        TEXT DEFAULT '3xml',
    item_count    INTEGER DEFAULT 0,
    control_total TEXT,
    created_at    TEXT,
    xml3_path     TEXT,
    lbxxml_path   TEXT,
    tml78_path    TEXT,
    bundle_path   TEXT,
    oxz_path      TEXT,
    custom_ext    TEXT,
    custom_path   TEXT
);

CREATE TABLE IF NOT EXISTS checklist_batch_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id  INTEGER NOT NULL REFERENCES checklist_batches(id) ON DELETE CASCADE,
    sequence  INTEGER,
    image_id  INTEGER REFERENCES checklist_images(id),
    item_type TEXT,
    amount    TEXT
);
"""


def connect():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    # add columns if they don't exist yet (upgrade path for existing DBs)
    for col, default in [("tml78_path", "NULL"), ("format", "'3xml'"),
                         ("custom_ext", "NULL"), ("custom_path", "NULL"),
                         ("oxz_path", "NULL")]:
        try:
            conn.execute(f"ALTER TABLE batches ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    try:                       # owner id — INTEGER (not TEXT) so fresh DBs compare cleanly
        conn.execute("ALTER TABLE batches ADD COLUMN user_id INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE learned_batch_types ADD COLUMN confirmed INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # per-user images: older DBs have no user_id AND a global UNIQUE(sha256), which
    # blocks two users from holding the same image. Rebuild the table once to add
    # user_id and drop that global-unique constraint (dedup is per-user now).
    icols = [r[1] for r in conn.execute("PRAGMA table_info(images)").fetchall()]
    if "user_id" not in icols:
        # legacy_alter_table=ON so RENAME does NOT rewrite the FK that batch_items
        # holds on images (otherwise it silently repoints to images_old -> dangling).
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("ALTER TABLE images RENAME TO images_old")
        conn.executescript(SCHEMA)                      # recreates images (new schema)
        oldcols = [r[1] for r in conn.execute("PRAGMA table_info(images_old)").fetchall()]
        newcols = [r[1] for r in conn.execute("PRAGMA table_info(images)").fetchall()]
        common = [c for c in oldcols if c in newcols]   # everything except user_id
        cl = ", ".join(common)
        conn.execute(f"INSERT INTO images ({cl}) SELECT {cl} FROM images_old")
        conn.execute("DROP TABLE images_old")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")

    # REPAIR: an earlier build's rebuild left batch_items' foreign key pointing at
    # the dropped images_old. Rebuild batch_items so its FK targets images again.
    fks = conn.execute("PRAGMA foreign_key_list(batch_items)").fetchall()
    if any((fk[2] == "images_old") for fk in fks):
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript("""
            CREATE TABLE batch_items_fix (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id  INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                sequence  INTEGER,
                image_id  INTEGER REFERENCES images(id),
                item_type TEXT,
                amount    TEXT
            );
        """)
        conn.execute("INSERT INTO batch_items_fix (id, batch_id, sequence, image_id, item_type, amount) "
                     "SELECT id, batch_id, sequence, image_id, item_type, amount FROM batch_items")
        conn.execute("DROP TABLE batch_items")
        conn.execute("ALTER TABLE batch_items_fix RENAME TO batch_items")
        conn.execute("PRAGMA foreign_keys=ON")

    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# images (the reusable library)
# ----------------------------------------------------------------------------
def find_image_by_hash(sha256, user_id=None):
    conn = connect()
    if user_id is None:
        row = conn.execute("SELECT * FROM images WHERE sha256 = ?", (sha256,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM images WHERE sha256 = ? AND user_id = ?",
                           (sha256, user_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def insert_image(rec):
    """rec is a dict (must include user_id). Deduped by sha256 within the owner —
    returns (image_id, created_bool)."""
    existing = find_image_by_hash(rec["sha256"], rec.get("user_id"))
    if existing:
        return existing["id"], False
    conn = connect()
    cur = conn.execute(
        """INSERT INTO images
           (user_id, sha256, original_filename, front_path, back_path, item_type, source,
            raw_micr, routing, account, check_number, amount, ocr_text, extra_json, uploaded_at)
           VALUES (:user_id, :sha256, :original_filename, :front_path, :back_path, :item_type, :source,
            :raw_micr, :routing, :account, :check_number, :amount, :ocr_text, :extra_json, :uploaded_at)""",
        {
            "user_id": rec.get("user_id"),
            "sha256": rec["sha256"],
            "original_filename": rec.get("original_filename"),
            "front_path": rec["front_path"],
            "back_path": rec.get("back_path"),
            "item_type": rec.get("item_type", "unknown"),
            "source": rec.get("source", "upload"),
            "raw_micr": rec.get("raw_micr"),
            "routing": rec.get("routing"),
            "account": rec.get("account"),
            "check_number": rec.get("check_number"),
            "amount": rec.get("amount"),
            "ocr_text": rec.get("ocr_text"),
            "extra_json": json.dumps(rec.get("extra", {})),
            "uploaded_at": rec.get("uploaded_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    conn.commit()
    image_id = cur.lastrowid
    conn.close()
    return image_id, True


def get_image(image_id):
    conn = connect()
    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_images(user_id=None):
    conn = connect()
    if user_id is None:
        rows = conn.execute("SELECT * FROM images ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM images WHERE user_id = ? ORDER BY id DESC",
                            (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_image(image_id, fields):
    allowed = {"item_type", "raw_micr", "routing", "account", "check_number",
               "amount", "extra_json"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    vals.append(image_id)
    conn = connect()
    conn.execute(f"UPDATE images SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def update_sha(image_id, sha256):
    conn = connect()
    conn.execute("UPDATE images SET sha256 = ? WHERE id = ?", (sha256, image_id))
    conn.commit()
    conn.close()


def set_image_paths(image_id, front_path=None, back_path=None, sha256=None):
    """Attach/replace a side (or the dedup key) on an existing library image."""
    sets, vals = [], []
    for col, val in (("front_path", front_path), ("back_path", back_path),
                     ("sha256", sha256)):
        if val is not None:
            sets.append(f"{col} = ?")
            vals.append(val)
    if not sets:
        return
    vals.append(image_id)
    conn = connect()
    conn.execute(f"UPDATE images SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def delete_image(image_id):
    conn = connect()
    # batch_items.image_id references images(id); a past batch may still point at
    # this image. Unlink it first (keeps the batch history row, drops the FK) so
    # the delete doesn't hit a FOREIGN KEY constraint failure.
    conn.execute("UPDATE batch_items SET image_id = NULL WHERE image_id = ?", (image_id,))
    conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# batches (generation history)
# ----------------------------------------------------------------------------
def insert_batch(meta, items, user_id=None):
    conn = connect()
    cur = conn.execute(
        """INSERT INTO batches
           (user_id, batch_type, biller, site, batch_date, lockbox, test_file, format,
            item_count, control_total, created_at, xml3_path, lbxxml_path,
            tml78_path, bundle_path, oxz_path, custom_ext, custom_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, meta.get("batch_type"), meta.get("biller"), meta.get("site"),
         meta.get("batch_date"), meta.get("lockbox"), meta.get("test_file", "Y"),
         meta.get("format", "3xml"),
         len(items), meta.get("control_total"),
         time.strftime("%Y-%m-%d %H:%M:%S"),
         meta.get("xml3_path"), meta.get("lbxxml_path"),
         meta.get("tml78_path"), meta.get("bundle_path"), meta.get("oxz_path"),
         meta.get("custom_ext"), meta.get("custom_path")),
    )
    batch_id = cur.lastrowid
    for seq, it in enumerate(items, start=1):
        conn.execute(
            "INSERT INTO batch_items (batch_id, sequence, image_id, item_type, amount) VALUES (?, ?, ?, ?, ?)",
            (batch_id, seq, it.get("image_id"), it.get("item_type"), it.get("amount")),
        )
    conn.commit()
    conn.close()
    return batch_id


def list_batches(user_id=None):
    conn = connect()
    if user_id is None:
        rows = conn.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM batches WHERE user_id = ? ORDER BY id DESC",
                            (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_batch(batch_id):
    conn = connect()
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_batch(batch_id):
    conn = connect()
    conn.execute("DELETE FROM batch_items WHERE batch_id = ?", (batch_id,))
    conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# learned output formats + batch types (persisted, survive restarts)
# ----------------------------------------------------------------------------
def list_formats():
    conn = connect()
    rows = conn.execute("SELECT ext, base FROM output_formats ORDER BY created_at").fetchall()
    conn.close()
    return [{"ext": r["ext"], "base": r["base"] or "3xml"} for r in rows]


def add_format(ext, base):
    conn = connect()
    conn.execute("INSERT OR IGNORE INTO output_formats (ext, base, created_at) VALUES (?, ?, ?)",
                 (ext, base, time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()


def list_learned_batch_types():
    conn = connect()
    rows = conn.execute("SELECT name, allowed, grp, descr, confirmed "
                        "FROM learned_batch_types ORDER BY created_at").fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            allowed = json.loads(r["allowed"] or "[]")
        except Exception:
            allowed = []
        out.append({"name": r["name"], "allowed": allowed,
                    "group": r["grp"] or "multiples", "desc": r["descr"] or "",
                    "confirmed": bool(r["confirmed"])})
    return out


def add_learned_batch_type(name, allowed, grp, descr, confirmed=False):
    """Insert a learned type only if the name isn't known yet (best-effort learns,
    e.g. from an OXZ job name)."""
    conn = connect()
    conn.execute("INSERT OR IGNORE INTO learned_batch_types "
                 "(name, allowed, grp, descr, confirmed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (name, json.dumps(list(allowed)), grp, descr, 1 if confirmed else 0,
                  time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()


def upsert_learned_batch_type(name, allowed, grp, descr, confirmed=True):
    """Insert or REPLACE — used when a real .3xml sample re-teaches a type's rule."""
    conn = connect()
    conn.execute("INSERT OR REPLACE INTO learned_batch_types "
                 "(name, allowed, grp, descr, confirmed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (name, json.dumps(list(allowed)), grp, descr, 1 if confirmed else 0,
                  time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# reference files (saved whole batch files, e.g. Check-List, for future reuse)
# ----------------------------------------------------------------------------
def find_reference_by_hash(sha256):
    conn = connect()
    row = conn.execute("SELECT * FROM reference_files WHERE sha256 = ?", (sha256,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_reference_file(txn_type, filename, path, sha256, size,
                       txn_count=0, doc_count=0, note=""):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = connect()
    cur = conn.execute(
        """INSERT INTO reference_files
           (txn_type, filename, path, sha256, size, txn_count, doc_count, note,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (txn_type, filename, path, sha256, size, txn_count, doc_count, note, now, now))
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def list_reference_files(txn_type=None):
    conn = connect()
    if txn_type:
        rows = conn.execute("SELECT * FROM reference_files WHERE txn_type = ? "
                            "ORDER BY id DESC", (txn_type,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM reference_files ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_reference_file(rid):
    conn = connect()
    row = conn.execute("SELECT * FROM reference_files WHERE id = ?", (rid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_reference_file(rid, fields):
    allowed = {"filename", "path", "sha256", "size", "txn_count", "doc_count", "note"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(time.strftime("%Y-%m-%d %H:%M:%S"))
    vals.append(rid)
    conn = connect()
    conn.execute(f"UPDATE reference_files SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def delete_reference_file(rid):
    conn = connect()
    conn.execute("DELETE FROM reference_files WHERE id = ?", (rid,))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# users (accounts + login). Password hashing is done in the app layer; this
# layer only stores/reads the already-hashed value.
# ----------------------------------------------------------------------------
def count_users():
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return n


def create_user(username, password_hash, is_admin=False, last_seen_version=""):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, last_seen_version, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, password_hash, 1 if is_admin else 0, last_seen_version,
             time.strftime("%Y-%m-%d %H:%M:%S")))
        uid = cur.lastrowid
        conn.commit()
        return uid
    except sqlite3.IntegrityError:
        return None                       # username already taken
    finally:
        conn.close()


def get_user_by_username(username):
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user(uid):
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users():
    conn = connect()
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_user(uid):
    conn = connect()
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    conn.close()


def set_password(uid, password_hash):
    conn = connect()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, uid))
    conn.commit()
    conn.close()


def set_last_seen_version(uid, version):
    conn = connect()
    conn.execute("UPDATE users SET last_seen_version = ? WHERE id = ?", (version, uid))
    conn.commit()
    conn.close()


def assign_orphan_data(uid):
    """Give any pre-accounts images / batches (user_id IS NULL) to this user.
    Run once when the first admin is created so existing local data isn't lost."""
    conn = connect()
    conn.execute("UPDATE images  SET user_id = ? WHERE user_id IS NULL", (uid,))
    conn.execute("UPDATE batches SET user_id = ? WHERE user_id IS NULL", (uid,))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# Check-List workspace (/cl/*) — a separate image + batch pool. Mirrors the
# images/batches/batch_items functions above exactly, pointed at the
# checklist_* tables instead, so the two pools can never cross-contaminate.
# ----------------------------------------------------------------------------
def find_checklist_image_by_hash(sha256, user_id=None):
    conn = connect()
    if user_id is None:
        row = conn.execute("SELECT * FROM checklist_images WHERE sha256 = ?", (sha256,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM checklist_images WHERE sha256 = ? AND user_id = ?",
                           (sha256, user_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def insert_checklist_image(rec):
    """rec is a dict (must include user_id). Deduped by sha256 within the owner —
    returns (image_id, created_bool)."""
    existing = find_checklist_image_by_hash(rec["sha256"], rec.get("user_id"))
    if existing:
        return existing["id"], False
    conn = connect()
    cur = conn.execute(
        """INSERT INTO checklist_images
           (user_id, sha256, original_filename, front_path, back_path, item_type, source,
            raw_micr, routing, account, check_number, amount, ocr_text, extra_json, uploaded_at)
           VALUES (:user_id, :sha256, :original_filename, :front_path, :back_path, :item_type, :source,
            :raw_micr, :routing, :account, :check_number, :amount, :ocr_text, :extra_json, :uploaded_at)""",
        {
            "user_id": rec.get("user_id"),
            "sha256": rec["sha256"],
            "original_filename": rec.get("original_filename"),
            "front_path": rec["front_path"],
            "back_path": rec.get("back_path"),
            "item_type": rec.get("item_type", "unknown"),
            "source": rec.get("source", "upload"),
            "raw_micr": rec.get("raw_micr"),
            "routing": rec.get("routing"),
            "account": rec.get("account"),
            "check_number": rec.get("check_number"),
            "amount": rec.get("amount"),
            "ocr_text": rec.get("ocr_text"),
            "extra_json": json.dumps(rec.get("extra", {})),
            "uploaded_at": rec.get("uploaded_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    conn.commit()
    image_id = cur.lastrowid
    conn.close()
    return image_id, True


def get_checklist_image(image_id):
    conn = connect()
    row = conn.execute("SELECT * FROM checklist_images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_checklist_images(user_id=None):
    conn = connect()
    if user_id is None:
        rows = conn.execute("SELECT * FROM checklist_images ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM checklist_images WHERE user_id = ? ORDER BY id DESC",
                            (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_checklist_image(image_id, fields):
    allowed = {"item_type", "raw_micr", "routing", "account", "check_number",
               "amount", "extra_json"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    vals.append(image_id)
    conn = connect()
    conn.execute(f"UPDATE checklist_images SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def update_checklist_sha(image_id, sha256):
    conn = connect()
    conn.execute("UPDATE checklist_images SET sha256 = ? WHERE id = ?", (sha256, image_id))
    conn.commit()
    conn.close()


def set_checklist_image_paths(image_id, front_path=None, back_path=None, sha256=None):
    sets, vals = [], []
    for col, val in (("front_path", front_path), ("back_path", back_path),
                     ("sha256", sha256)):
        if val is not None:
            sets.append(f"{col} = ?")
            vals.append(val)
    if not sets:
        return
    vals.append(image_id)
    conn = connect()
    conn.execute(f"UPDATE checklist_images SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def delete_checklist_image(image_id):
    conn = connect()
    conn.execute("UPDATE checklist_batch_items SET image_id = NULL WHERE image_id = ?", (image_id,))
    conn.execute("DELETE FROM checklist_images WHERE id = ?", (image_id,))
    conn.commit()
    conn.close()


def insert_checklist_batch(meta, items, user_id=None):
    conn = connect()
    cur = conn.execute(
        """INSERT INTO checklist_batches
           (user_id, batch_type, biller, site, batch_date, lockbox, test_file, format,
            item_count, control_total, created_at, xml3_path, lbxxml_path,
            tml78_path, bundle_path, oxz_path, custom_ext, custom_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, meta.get("batch_type", "Check-List"), meta.get("biller"), meta.get("site"),
         meta.get("batch_date"), meta.get("lockbox"), meta.get("test_file", "Y"),
         meta.get("format", "3xml"),
         len(items), meta.get("control_total"),
         time.strftime("%Y-%m-%d %H:%M:%S"),
         meta.get("xml3_path"), meta.get("lbxxml_path"),
         meta.get("tml78_path"), meta.get("bundle_path"), meta.get("oxz_path"),
         meta.get("custom_ext"), meta.get("custom_path")),
    )
    batch_id = cur.lastrowid
    for seq, it in enumerate(items, start=1):
        conn.execute(
            "INSERT INTO checklist_batch_items (batch_id, sequence, image_id, item_type, amount) "
            "VALUES (?, ?, ?, ?, ?)",
            (batch_id, seq, it.get("image_id"), it.get("item_type"), it.get("amount")),
        )
    conn.commit()
    conn.close()
    return batch_id


def list_checklist_batches(user_id=None):
    conn = connect()
    if user_id is None:
        rows = conn.execute("SELECT * FROM checklist_batches ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM checklist_batches WHERE user_id = ? ORDER BY id DESC",
                            (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_checklist_batch(batch_id):
    conn = connect()
    row = conn.execute("SELECT * FROM checklist_batches WHERE id = ?", (batch_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_checklist_batch(batch_id):
    conn = connect()
    conn.execute("DELETE FROM checklist_batch_items WHERE batch_id = ?", (batch_id,))
    conn.execute("DELETE FROM checklist_batches WHERE id = ?", (batch_id,))
    conn.commit()
    conn.close()
