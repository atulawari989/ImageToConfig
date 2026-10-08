"""Parse a scanner .OXZ batch (zip of TIFF/JPEG + one .oxi XML index).

Verified against two real samples:
  - all_300_dpi_test_770035553058.oxz  (PageType STUB / PERSONAL_CHECK / PAGE / ENVELOPE)
  - 42006709_PCard1_7.oxz              (PageType PAGE / ENVELOPE only)

Structure (see README for the annotated sample):
  <Batch ...>
    <Transaction TransactionID="...">
      <Group GroupID="...">
        <Page DocumentLocator="..." PageType="STUB|PERSONAL_CHECK|PAGE|ENVELOPE" ...>
          <Image Index Side="FRONT|BACK" Format="TIFF|JPEG" Filename="00010.tif" .../>
          ...
        </Page>

Ingestion rules (confirmed from samples):
  * Take only Format="TIFF" images (that's what real output embeds).
  * Pair FRONT + BACK per page; JPEG is a colour duplicate and is skipped.
  * Exclude PageType="ENVELOPE" — the carrier envelope is never carried into
    .3xml/.LBXxml output.
  * PageType maps to our doc types: PERSONAL_CHECK->check, STUB->stub,
    PAGE->unknown (needs OCR classification), ENVELOPE->envelope (excluded).
"""
import os
import re
import zipfile
import xml.etree.ElementTree as ET

PAGETYPE_MAP = {
    "PERSONAL_CHECK": "check",
    "STUB": "stub",
    "PAGE": "unknown",       # scanner didn't classify -> fall back to OCR
    "ENVELOPE": "envelope",  # excluded from output
}


def transcode_micr(value):
    """Convert a scanner MICR/OCR codeline into the .3xml rawMicr convention.

    The scanner records the *real* magnetic MICR read (accurate — unlike OCR of
    the image), e.g.  `c00020705c d111100006d 5550055555c`, where the E-13B
    symbols are transcribed as:
        d = transit  ⑆  (routing)      -> T…T
        c = on-us    ⑈  (account)      -> O…O
    which we reorder transit-first to match the sample files:
        `c00020705c d111100006d 5550055555c` -> `T111100006T O00020705O 5550055555`
    Returns None if there's no usable codeline.
    """
    if not value:
        return None
    v = " ".join(value.split())
    m = re.search(r"d\s*(\d+)\s*d", v)          # transit field (routing)
    transit = m.group(1) if m else ""
    rest = re.sub(r"d\s*\d+\s*d", " ", v, count=1) if m else v
    onus = re.findall(r"\d+", rest)             # remaining on-us / aux digit groups
    parts = []
    if transit:
        parts.append(f"T{transit}T")
    if onus:
        parts.append(f"O{onus[0]}O")
        parts += onus[1:]                       # trailing aux / serial, unwrapped
    return " ".join(parts) or None


def _page_micr(page):
    """Prefer the spaced <Ocr> codeline, fall back to <Micr>; front side."""
    for tag in ("Ocr", "Micr"):
        for el in page.findall(tag):
            side = (el.get("Side") or "FRONT").upper()
            if side == "FRONT" and el.get("Value"):
                return transcode_micr(el.get("Value"))
    return None


def _batch_meta(root):
    a = root.attrib
    return {
        "job_name": a.get("JobName"),
        "job_type": a.get("JobType"),          # e.g. UNSTRUCTURED — the work/batch type
        "operating_mode": a.get("OperatingMode"),
        "operator": a.get("OperatorName"),
        "process_date": a.get("ProcessDate"),
        "receive_date": a.get("ReceiveDate"),
        "scan_device": a.get("ScanDevice"),
        "batch_identifier": a.get("BatchIdentifier"),
    }


def parse_oxi(oxi_bytes):
    """Parse .oxi XML bytes into (batch_meta, [page_dicts]).

    Each page dict: transaction_id, group_id, document_locator, page_type,
    item_type, front_filename, back_filename (may be None), barcode.
    Envelope pages are included here but flagged (excluded=True) so callers can
    show/skip them explicitly.
    """
    root = ET.fromstring(oxi_bytes)
    meta = _batch_meta(root)
    pages = []
    for txn in root.findall("Transaction"):
        tid = txn.get("TransactionID")
        for grp in txn.findall("Group"):
            gid = grp.get("GroupID")
            for page in grp.findall("Page"):
                ptype = (page.get("PageType") or "PAGE").upper()
                front = back = None
                for img in page.findall("Image"):
                    if (img.get("Format") or "").upper() != "TIFF":
                        continue  # skip JPEG colour duplicates
                    side = (img.get("Side") or "").upper()
                    if side == "FRONT" and front is None:
                        front = img.get("Filename")
                    elif side == "BACK" and back is None:
                        back = img.get("Filename")
                barcode = None
                bc = page.find("Barcode")
                if bc is not None:
                    barcode = bc.get("Value") or None
                pages.append({
                    "transaction_id": tid,
                    "group_id": gid,
                    "document_locator": page.get("DocumentLocator"),
                    "page_type": ptype,
                    "item_type": PAGETYPE_MAP.get(ptype, "unknown"),
                    "front_filename": front,
                    "back_filename": back,
                    "barcode": barcode,
                    "micr": _page_micr(page),   # accurate scanner MICR (transcoded)
                    "excluded": ptype == "ENVELOPE",
                })
    return meta, pages


def open_oxz(oxz_path):
    """Open an .OXZ and return (batch_meta, pages, zip_reader_fn).

    `zip_reader_fn(filename)` returns the raw bytes of a member (TIFF image).
    Caller is responsible for closing nothing — the zip is read eagerly for the
    .oxi and lazily for images via a still-open handle held on the returned fn.
    """
    zf = zipfile.ZipFile(oxz_path, "r")
    oxi_name = next((n for n in zf.namelist() if n.lower().endswith(".oxi")), None)
    if not oxi_name:
        zf.close()
        raise ValueError("No .oxi index file found inside the OXZ archive.")
    meta, pages = parse_oxi(zf.read(oxi_name))

    names = {os.path.basename(n): n for n in zf.namelist()}

    def read_member(filename):
        if filename is None:
            return None
        real = names.get(os.path.basename(filename), filename)
        try:
            return zf.read(real)
        except KeyError:
            return None

    return meta, pages, read_member, zf
