"""Optional Claude (Anthropic) integration for ImageToConfig.

This is the ONLY place the app talks to an LLM. Everything degrades gracefully:
if the `anthropic` package isn't installed, or no API key is configured, or the
API call fails, each helper returns a small "not available / error" dict and the
app falls back to its existing heuristics. The app never hard-depends on Claude.

Security / config
-----------------
The API key is read from the environment (ANTHROPIC_API_KEY), optionally loaded
from a local, gitignored `.env` file. The key is NEVER hardcoded in source and
never logged. See .env.example.

NOTE: enabling this sends document OCR text to Anthropic's cloud API. That text
can contain MICR / account numbers / PII. Use test or masked data.
"""
import json
import os

# Default model — see the claude-api reference. Overridable via env.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")

# Load a local .env (if present) so ANTHROPIC_API_KEY can live outside the shell.
try:
    from dotenv import load_dotenv
    from .config import BASE_DIR
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except Exception:
    pass

# NOTE: the `anthropic` SDK is heavy (~2s to import) — importing it at module
# load made the app take seconds to bind its port, so the user could hit the
# page before it was ready and see an error until they refreshed. We only check
# that the package EXISTS at load (fast, no code executed) and defer the real
# import to the first API call in _get_client().
import importlib.util as _ilu

_SDK_OK = _ilu.find_spec("anthropic") is not None
_client = None


def available():
    """True only if the SDK is importable AND an API key is configured."""
    return _SDK_OK and bool(os.environ.get("ANTHROPIC_API_KEY"))


def status():
    """Human-readable status for the UI badge."""
    if not _SDK_OK:
        return ("off", "AI: SDK not installed")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ("off", "AI: no API key")
    return ("on", f"AI: Claude ready ({MODEL})")


def _get_client():
    global _client
    if _client is None:
        import anthropic                   # deferred: heavy import, only when used
        _client = anthropic.Anthropic()    # reads ANTHROPIC_API_KEY from env
    return _client


def _json_call(system, user_text, schema, max_tokens=1024):
    """One structured (JSON-schema-constrained) Claude call. Returns a dict, or
    {'_error': msg} on any failure — callers must tolerate that."""
    if not available():
        return {"_error": "Claude is not configured (set ANTHROPIC_API_KEY)."}
    try:
        resp = _get_client().messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_text}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)
    except Exception as e:                 # network, auth, rate-limit, parse…
        return {"_error": f"{type(e).__name__}: {e}"}


def _vision_json_call(system, image_b64, media_type, prompt, schema, max_tokens=400):
    """One structured Claude call that includes an image. Returns a dict, or
    {'_error': msg} on any failure."""
    if not available():
        return {"_error": "Claude is not configured (set ANTHROPIC_API_KEY)."}
    try:
        resp = _get_client().messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": prompt},
            ]}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def _text_call(system, user_text, max_tokens=1500):
    """One plain-text Claude call. Returns str, or '' on failure."""
    if not available():
        return ""
    try:
        resp = _get_client().messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        return f"(Claude review unavailable: {type(e).__name__}: {e})"


# ---------------------------------------------------------------------------
# 1) Smart document classification (beyond the regex/OCR heuristics)
# ---------------------------------------------------------------------------
_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "item_type": {"type": "string",
                      "enum": ["check", "stub", "statement", "fullpage",
                               "correspondence", "unknown"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["item_type", "confidence", "reasoning"],
    "additionalProperties": False,
}


def classify_document(ocr_text, micr=None):
    """Classify a lockbox document from its OCR text. Returns a dict with
    item_type / confidence / reasoning, or {'_error': ...}."""
    sys = ("You classify scanned USA lockbox / remittance documents into exactly one "
           "type: check (a bank cheque, has a MICR line), stub (a small remittance "
           "coupon), statement (a billing statement with a scanline), fullpage (a "
           "full-page document such as an invoice or letter that fills the page and "
           "has no scanline), correspondence (a letter / note), or unknown. Judge only "
           "from the text provided.")
    user = f"MICR/scanline: {micr or '(none)'}\n\nOCR text:\n{(ocr_text or '')[:4000]}"
    return _json_call(sys, user, _CLASSIFY_SCHEMA, max_tokens=512)


# ---------------------------------------------------------------------------
# 2) Learn a new format / batch type from an uploaded sample
# ---------------------------------------------------------------------------
_ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "base_schema": {"type": "string", "enum": ["3xml", "lbxxml", "78tml"]},
        "batch_type_name": {"type": "string"},
        "allowed_types": {"type": "array",
                          "items": {"type": "string",
                                    "enum": ["check", "stub", "statement", "correspondence"]}},
        "summary": {"type": "string"},
    },
    "required": ["base_schema", "batch_type_name", "allowed_types", "summary"],
    "additionalProperties": False,
}


def analyze_sample(filename, content_preview):
    """Infer schema family + a batch-type name from an unknown sample file."""
    sys = ("You analyze batch/config files for a lockbox check-processing pipeline. "
           "Pipeline output schemas are: 3xml (nested file>cashLetter>batch>transaction), "
           "lbxxml (flat PascalCase), 78tml (like 3xml with site info inside the Front "
           "image). Given a sample, pick the closest base_schema, a short human batch_type "
           "name, and which document types it contains.")
    user = f"Filename: {filename}\n\nContent (first 4000 chars):\n{(content_preview or '')[:4000]}"
    return _json_call(sys, user, _ANALYZE_SCHEMA, max_tokens=700)


# ---------------------------------------------------------------------------
# 2b) Read the MICR / codeline from a check image (Claude vision)
# ---------------------------------------------------------------------------
_MICR_SCHEMA = {
    "type": "object",
    "properties": {
        "micr": {"type": "string"},
        "readable": {"type": "boolean"},
    },
    "required": ["micr", "readable"],
    "additionalProperties": False,
}


def read_micr_from_image(image_path):
    """Use Claude vision to read the MICR codeline from a check image.

    Returns {'micr': str, 'readable': bool} (micr in the .3xml rawMicr
    convention, transit-first), or {'_error': ...} on failure. The stored
    library image is a bitonal TIFF, so it's converted to PNG for the API
    (Anthropic vision accepts PNG/JPEG/GIF/WebP, not TIFF)."""
    if not available():
        return {"_error": "Claude is not configured (set ANTHROPIC_API_KEY)."}
    try:
        import base64
        import io as _io
        from PIL import Image
        with Image.open(image_path) as im:
            buf = _io.BytesIO()
            im.convert("L").save(buf, format="PNG")   # grayscale PNG
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
    except Exception as e:
        return {"_error": f"image read failed: {type(e).__name__}: {e}"}

    sys = ("You read the MICR / codeline printed along the bottom of a USA bank check. "
           "Transcribe it in the .3xml rawMicr convention: the transit/routing field "
           "first as T<routing>T, then the account field as O<account>O, then any "
           "trailing serial or auxiliary numbers, all space-separated. "
           "Example output: 'T121000248T O0155130O 4121176507'. "
           "Read digits exactly as printed — do not guess or invent numbers. "
           "If no MICR line is legible, set readable=false and micr to an empty string.")
    prompt = ("Read the MICR codeline from the bottom of this check and return it in the "
              "rawMicr convention described.")
    return _vision_json_call(sys, b64, "image/png", prompt, _MICR_SCHEMA, max_tokens=300)


# ---------------------------------------------------------------------------
# 2c) Read the payment amount from a check / document image (Claude vision)
# ---------------------------------------------------------------------------
_AMOUNT_SCHEMA = {
    "type": "object",
    "properties": {
        "amount": {"type": "string"},
        "readable": {"type": "boolean"},
    },
    "required": ["amount", "readable"],
    "additionalProperties": False,
}


def read_amount_from_image(image_path):
    """Use Claude vision to read the payment amount from a check/document image.
    Returns {'amount': '200.00', 'readable': bool} (plain decimal, no symbol), or
    {'_error': ...}. The bitonal TIFF is converted to PNG for the API."""
    if not available():
        return {"_error": "Claude is not configured (set ANTHROPIC_API_KEY)."}
    try:
        import base64
        import io as _io
        from PIL import Image
        with Image.open(image_path) as im:
            buf = _io.BytesIO()
            im.convert("L").save(buf, format="PNG")
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
    except Exception as e:
        return {"_error": f"image read failed: {type(e).__name__}: {e}"}

    sys = ("You read the payment AMOUNT from a scanned USA bank check or remittance document. "
           "Return the numeric dollar amount as a plain decimal with two places (e.g. '200.00', "
           "'1234.56') — no currency symbol and no thousands separators. On a check, prefer the "
           "courtesy amount (the boxed $ figure) and cross-check it against the written legal "
           "amount. Read digits exactly — never guess. If no amount is legible, set "
           "readable=false and amount to an empty string.")
    prompt = "Read the amount on this document and return it as a decimal like '200.00'."
    return _vision_json_call(sys, b64, "image/png", prompt, _AMOUNT_SCHEMA, max_tokens=200)


# ---------------------------------------------------------------------------
# 3) QA-review a generated batch file for likely repair-rejection issues
# ---------------------------------------------------------------------------
def review_batch(kind, xml_content):
    """Return a plain-text QA review of a generated batch file."""
    sys = ("You are a QA reviewer for a USA lockbox check-processing pipeline. Review the "
           "generated batch file below and flag anything likely to cause a downstream "
           "'repair' rejection: malformed structure, missing/empty required fields, "
           "control-total mismatches, MICR/rawMicr problems, or field-order issues. Be "
           "concrete and concise. If it looks clean, say so. Do not invent problems.")
    user = f"Format: .{kind}\n\nFile content (first 12000 chars):\n{(xml_content or '')[:12000]}"
    return _text_call(sys, user, max_tokens=1500)
