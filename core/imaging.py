"""Image ingestion: normalise to TIFF, hash for dedup, persist to the library.

Real .3xml/.LBXxml output embeds TIFF bytes, so we normalise every ingested
image to TIFF on the way in. Dedup is by SHA-256 of the stored front bytes.
"""
import hashlib
import os
import io

from PIL import Image

from .config import LIBRARY_DIR, ensure_dirs


def otsu_threshold(gray):
    """Compute an Otsu (auto) threshold for a PIL 'L' image — adapts per image
    instead of a fixed cutoff, so a colour photo / tinted cheque binarises
    cleanly instead of turning into noise."""
    hist = gray.histogram()[:256]
    total = sum(hist)
    if not total:
        return 128
    sum_all = sum(i * hist[i] for i in range(256))
    sum_b = w_b = 0
    best_var = -1.0
    level = 128
    for i in range(256):
        w_b += hist[i]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * hist[i]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between >= best_var:
            best_var = between
            level = i
    return level


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _to_bitonal_g4(im, dpi=(200, 200)):
    """Encode a PIL image as single-page, bitonal (1-bit) CCITT Group-4 TIFF.

    This matches the exact format the real .3xml/.LBXxml sample files embed
    (mode=1, compression=4 = Group 4 fax, photometric min-is-white). Anything
    else — colour/greyscale TIFF, PNG, JPG — is rejected by the downstream
    system, so every non-bitonal upload is normalised here.
    """
    if im.mode != "1":
        # Otsu auto-threshold (no dithering) — adapts per image so colour/tinted
        # cheques binarise cleanly instead of turning into speckle.
        g = im.convert("L")
        lvl = otsu_threshold(g)
        im = g.point(lambda x: 255 if x >= lvl else 0, mode="1")
    out = io.BytesIO()
    # photometric 0 (min-is-white) matches the scanner TIFFs in the samples;
    # Pillow inverts the bit data internally so the render stays correct.
    im.save(out, format="TIFF", compression="group4", dpi=dpi, tiffinfo={262: 0})
    return out.getvalue()


def to_tiff_bytes(data):
    """Return bitonal Group-4 TIFF bytes for arbitrary image `data`.

    A scanner TIFF that is already bitonal (mode=1) is passed through
    byte-for-byte, so OXZ-sourced images stay identical to the scanner output
    (and dedupe cleanly on re-upload). Everything else is normalised to the
    bitonal Group-4 format the samples use.
    """
    if data[:2] in (b"II", b"MM"):  # TIFF magic (little/big endian)
        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.mode == "1" and getattr(im, "n_frames", 1) == 1:
                    return data  # already correct format -> keep exact bytes
                return _to_bitonal_g4(im)
        except Exception:
            return data  # unreadable TIFF: keep as-is rather than lose it
    with Image.open(io.BytesIO(data)) as im:
        return _to_bitonal_g4(im)


def store_bytes(front_bytes, back_bytes=None, library_dir=None):
    """Persist front (+optional back) TIFF bytes. Returns (sha256, front_path,
    back_path). Filenames are derived from the front hash so identical images
    collapse onto the same files. `library_dir` defaults to the main LIBRARY_DIR
    — pass a different folder (e.g. the Check-List workspace's own directory)
    to keep that pool's files fully separate on disk."""
    ensure_dirs()
    library_dir = library_dir or LIBRARY_DIR
    front_tiff = to_tiff_bytes(front_bytes)
    sha = sha256_bytes(front_tiff)
    front_path = os.path.join(library_dir, f"{sha}_front.tif")
    with open(front_path, "wb") as f:
        f.write(front_tiff)
    back_path = None
    if back_bytes:
        back_tiff = to_tiff_bytes(back_bytes)
        back_path = os.path.join(library_dir, f"{sha}_back.tif")
        with open(back_path, "wb") as f:
            f.write(back_tiff)
    return sha, front_path, back_path


def to_png_preview(tiff_path, max_side=900):
    """Return PNG bytes for browser display (browsers can't show TIFF)."""
    with Image.open(tiff_path) as im:
        if im.mode not in ("L", "RGB", "RGBA"):
            im = im.convert("L")
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.save(out, format="PNG")
        return out.getvalue()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def rotate_file(path, direction):
    """Rotate a stored image 90° left/right or 180° and overwrite it in place,
    keeping the bitonal Group-4 / photometric-0 format. Returns the new sha256
    of the file's bytes (so callers can refresh the dedup key), or None on error.

    90° steps use transpose (lossless — no interpolation), ideal for bitonal art.
    """
    ops = {
        "left": Image.Transpose.ROTATE_90,     # counter-clockwise
        "right": Image.Transpose.ROTATE_270,   # clockwise
        "180": Image.Transpose.ROTATE_180,
    }
    op = ops.get(direction)
    if op is None:
        return None
    try:
        with Image.open(path) as im:
            dpi = im.info.get("dpi", (200, 200))
            rot = im.transpose(op)
    except Exception:
        return None
    if rot.mode != "1":
        rot = rot.convert("1")
    buf = io.BytesIO()
    rot.save(buf, format="TIFF", compression="group4", dpi=dpi, tiffinfo={262: 0})
    data = buf.getvalue()
    with open(path, "wb") as f:
        f.write(data)
    return sha256_bytes(data)


# ---------------------------------------------------------------------------
# perceptual hashing (near-duplicate detection)
# ---------------------------------------------------------------------------
def ahash(path, size=8):
    """64-bit average hash of an image. Two visually-similar scans (different
    bytes, same content) produce hashes a few bits apart — unlike sha256, which
    only catches byte-identical files."""
    try:
        with Image.open(path) as im:
            im = im.convert("L").resize((size, size))
            px = list(im.getdata())
    except Exception:
        return None
    avg = sum(px) / len(px) if px else 0
    bits = 0
    for i, p in enumerate(px):
        if p >= avg:
            bits |= (1 << i)
    return bits


def hamming(a, b):
    if a is None or b is None:
        return 999
    return bin(a ^ b).count("1")


# images within this many bits are treated as the same picture
DUP_THRESHOLD = 5
