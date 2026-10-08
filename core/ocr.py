"""Best-effort OCR + MICR reading and document classification.

This is a *heuristic* reader, not a certified MICR engine. Everything it
produces is meant to be corrected by QA before a batch is generated.

If the Tesseract binary isn't installed, OCR silently degrades: we return
empty text and fall back to scanner-provided page types / filenames. The app
stays fully usable; QA just types the MICR/fields in by hand.
"""
import re
import os

from .config import DATA_DIR

try:
    import pytesseract
    from PIL import Image
    _PIL_OK = True
except Exception:  # pragma: no cover
    _PIL_OK = False


_TESSERACT_OK = None  # tri-state cache: None=unknown, True/False once probed

# MICR-trained model (E-13B), downloaded into data/tessdata/mcr.traineddata.
_MCR_DIR = os.path.join(DATA_DIR, "tessdata")
_MCR_OK = None


def tesseract_available():
    """Probe once whether a runnable Tesseract binary exists."""
    global _TESSERACT_OK
    if _TESSERACT_OK is not None:
        return _TESSERACT_OK
    if not _PIL_OK:
        _TESSERACT_OK = False
        return False
    # Common Windows install location, in case it isn't on PATH.
    candidates = [
        os.environ.get("TESSERACT_CMD"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Programs\Tesseract-OCR\tesseract.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            pytesseract.pytesseract.tesseract_cmd = c
            break
    try:
        pytesseract.get_tesseract_version()
        _TESSERACT_OK = True
    except Exception:
        _TESSERACT_OK = False
    return _TESSERACT_OK


def micr_available():
    """Whether the MICR (E-13B) model is present so we can read a check's
    codeline straight off the image."""
    global _MCR_OK
    if _MCR_OK is None:
        _MCR_OK = tesseract_available() and os.path.exists(
            os.path.join(_MCR_DIR, "mcr.traineddata"))
    return _MCR_OK


# The mcr model transcribes E-13B symbols as: a = transit ⑆ (routing),
# c = on-us ⑈ (account). We reorder transit-first to match the .3xml convention.
def _aba_valid(r):
    """US routing numbers are 9 digits with an ABA checksum. Requiring this
    rejects almost all OCR garbage (only a real codeline passes), so we never
    auto-fill a plausible-but-wrong routing."""
    if len(r) != 9 or not r.isdigit():
        return False
    d = [int(c) for c in r]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) +
            (d[2] + d[5] + d[8])) % 10 == 0


def _format_micr_ocr(raw):
    v = " ".join((raw or "").split()).lower()
    compact = v.replace(" ", "")          # OCR often splits the digits
    routing = None
    for src in (compact, v):
        for m in re.finditer(r"a(\d{9})a", src):
            if _aba_valid(m.group(1)):
                routing = m.group(1)
                break
        if routing:
            break
    if not routing:                       # no valid routing -> not confident
        return None
    parts = ["T%sT" % routing]
    onus = re.findall(r"c(\d+)c", compact)
    if onus:
        parts.append("O%sO" % onus[0])
        parts += onus[1:]                 # trailing aux / serial, unwrapped
    return " ".join(parts)


def read_micr(src):
    """Read a check's MICR line straight off the image using the E-13B model.

    `src` may be an image path OR the raw bytes of the ORIGINAL upload — read
    from the full-quality original (colour/greyscale), NOT the harshly binarised
    stored copy, then Otsu-threshold just the MICR band for a clean OCR.

    Returns a formatted `T{routing}T O{account}O {aux}` string, or None if no
    codeline is confidently found (so non-checks / faint scans stay blank rather
    than garbage). Best-effort — QA should verify it.
    """
    if not micr_available():
        return None
    try:
        import io
        from . import imaging
        if isinstance(src, (bytes, bytearray)):
            im = Image.open(io.BytesIO(src))
        else:
            im = Image.open(src)
        gray = im.convert("L")
    except Exception:
        return None
    w, h = gray.size
    tessdir = _MCR_DIR.replace("\\", "/")
    best = None

    def _try(img, psm):
        nonlocal best
        lvl = imaging.otsu_threshold(img)
        bw = img.point(lambda x: 255 if x >= lvl else 0, mode="1")
        cfg = "-l mcr --psm %d --tessdata-dir %s" % (psm, tessdir)
        try:
            raw = pytesseract.image_to_string(bw, config=cfg)
        except Exception:
            return None
        parsed = _format_micr_ocr(raw)
        if parsed and "O" in parsed:      # routing + account -> confident match
            return parsed
        if parsed and best is None:
            best = parsed                 # routing only -> fallback
        return None

    # 1) the MICR line sits in the bottom strip — try several crop positions
    for top in (0.86, 0.80, 0.90, 0.74, 0.68):
        band = gray.crop((0, int(h * top), w, h))
        band = band.resize((band.width * 3, band.height * 3))
        hit = _try(band, 7)
        if hit:
            return hit

    # 2) full-image sweeps (handles photos where the cheque doesn't fill the
    #    frame or sits at an angle) — sparse-text + single-block modes
    scale = 2 if max(w, h) < 1800 else 1
    full = gray.resize((w * scale, h * scale)) if scale > 1 else gray
    for psm in (11, 6, 4):
        hit = _try(full, psm)
        if hit:
            return hit
    return best


def ocr_text(image_path):
    """Return raw OCR text for an image, or '' if OCR is unavailable/fails."""
    if not tesseract_available():
        return ""
    try:
        with Image.open(image_path) as im:
            if im.mode not in ("L", "RGB"):
                im = im.convert("L")
            return pytesseract.image_to_string(im)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# MICR parsing
# ---------------------------------------------------------------------------
# Real MICR lines use special symbols. In the sample files they are transcribed
# as: T...T = transit/routing, O...O = on-us/account, bare digits = check no.
# e.g. "T021001208T O4315499O 1069".  OCR of a raw image won't emit those
# symbols reliably, so we also fall back to plain digit-run heuristics.
_ROUTING_RE = re.compile(r"[T⑆]\s*(\d{9})\s*[T⑆]")
_ONUS_RE = re.compile(r"[O⑈]\s*([\d\-]{3,})\s*[O⑈]")
_DIGIT_RUN = re.compile(r"\d[\d\-]{2,}")


def parse_micr(text):
    """Pull routing / account / check-number out of a candidate MICR string.

    Returns a dict; any field may be None. Works both on a clean scanline
    (with T/O markers) and on messy OCR output.
    """
    if not text:
        return {"raw_micr": None, "routing": None, "account": None, "check_number": None}

    line = _pick_micr_line(text)
    routing = account = check_number = None

    m = _ROUTING_RE.search(line)
    if m:
        routing = m.group(1)
    onus = _ONUS_RE.findall(line)
    if onus:
        # longest on-us group is usually the account
        account = max(onus, key=len)

    if routing is None or account is None:
        runs = _DIGIT_RUN.findall(line)
        runs = [r.replace("-", "") for r in runs]
        if routing is None:
            nine = next((r for r in runs if len(r) == 9), None)
            routing = nine or (runs[0] if runs else None)
        if account is None:
            # exclude the routing number's own digits from the candidates —
            # otherwise a scanline with a single trailing 'O' (account'O'aux,
            # no closing 'O') falls back to "longest digit run in the line",
            # which is routing itself whenever the real account is shorter
            # than 9 digits. That silently sets account = routing, making two
            # different checks that share a routing number look identical.
            candidates = [r for r in runs if r != routing]
            if candidates:
                account = max(candidates, key=len)

    # a short trailing digit run that isn't routing/account -> check number
    trailing = _DIGIT_RUN.findall(line)
    for r in reversed(trailing):
        rc = r.replace("-", "")
        if rc not in (routing, account) and 1 <= len(rc) <= 6:
            check_number = rc
            break

    raw = line.strip() or None
    return {"raw_micr": raw, "routing": routing, "account": account,
            "check_number": check_number}


def micr_key(raw_micr):
    """Comparable (routing, account) key parsed from a stored/candidate MICR
    string, or None if no confident routing number could be pulled out.

    Used to tell two visually-similar documents apart by content: routing is
    the most reliable field (9 digits, checked earlier against ABA rules by
    whichever reader produced the string), so it alone is enough to prove two
    checks are different even when the account/check-number parse is fuzzy.
    """
    if not raw_micr:
        return None
    parsed = parse_micr(raw_micr)
    routing = parsed.get("routing")
    if not routing:
        return None
    return (routing, parsed.get("account"))


def _pick_micr_line(text):
    """Choose the most MICR-looking line from a block of OCR text."""
    best, best_score = "", -1
    for ln in text.splitlines():
        if "T" in ln or "O" in ln or sum(c.isdigit() for c in ln) >= 6:
            score = sum(c.isdigit() for c in ln) + 5 * (ln.count("T") + ln.count("O"))
            if score > best_score:
                best, best_score = ln, score
    return best or text.strip().splitlines()[0] if text.strip() else ""


# ---------------------------------------------------------------------------
# Document-type classification (keyword fallback)
# ---------------------------------------------------------------------------
_KEYWORDS = [
    ("check", ["pay to the order", "pay to the", "memo", "void after", "dollars"]),
    ("statement", ["statement", "account summary", "billing statement", "balance due"]),
    ("stub", ["remittance", "stub", "amount enclosed", "invoice", "please detach",
              "payment coupon", "return this portion"]),
    ("correspondence", ["dear ", "sincerely", "to whom it may concern", "letter"]),
]


def classify(text, scanner_hint=None):
    """Guess a document type. `scanner_hint` is the OXZ PageType if we have one.

    Precedence: an explicit scanner page type wins; otherwise keyword match on
    OCR text; otherwise 'unknown'.
    """
    hint = (scanner_hint or "").upper()
    if hint == "PERSONAL_CHECK":
        return "check"
    if hint == "STUB":
        return "stub"
    if hint == "ENVELOPE":
        return "envelope"
    # "PAGE" (generic) or no hint -> fall through to OCR keywords

    low = (text or "").lower()
    for label, kws in _KEYWORDS:
        if any(k in low for k in kws):
            return label
    return "unknown"


def read_image(image_path, scanner_hint=None):
    """One-shot: OCR + MICR parse + classify. Returns a dict of best guesses."""
    text = ocr_text(image_path)
    micr = parse_micr(text)
    item_type = classify(text, scanner_hint)
    return {
        "ocr_text": text or None,
        "item_type": item_type,
        **micr,
    }
