"""Build a scanner .OXZ batch (zip of TIFFs + one .oxi XML index).

This is the inverse of core/oxi_reader.py — it lets QA generate a *synthetic
scanner batch* from library images, to exercise the ingestion side of the
pipeline (not just the .3xml/.LBXxml/.78tml output side).

The .oxi faithfully reproduces a REAL accepted scanner batch (Falcon/OPEX), whose
structure was validated attribute-for-attribute against a known-good sample:

  <Batch FormatVersion="01.50" BaseMachine="AS3600_STANDALONE" ScanDevice="FalconV+_x64"
         SoftwareVersion=... OperatingMode="MANUAL_SCAN" DeveloperReserved="04,01" ...>
    <Transaction TransactionID="0001">
      <Group GroupID="0001">
        <Page DocumentLocator="1" BatchSequence="1" ItemStatus="VALID" IsVirtual="NO"
              PageType="PERSONAL_CHECK" PageName="CW_CHECK" Bin="1" DeskewStatus="YES" ...>
          <Image Index="1" RescanStatus="NOT_RESCAN" Side="FRONT" Depth="1" Format="TIFF"
                 Filename="00020.tif" .../>          <!-- filename: {docloc:04d}{index-1} -->
          <Image Index="2" ... Side="BACK"  Format="TIFF" Filename="00021.tif" .../>
          <Image Index="3" ... Side="FRONT" Depth="8" Format="JPEG" Filename="00022.jpg" .../>
          <Micr Status="INACTIVE" ... Value="d<routing>d<account>c<serial>" />
          <Ocr  Index="1" Side="FRONT" Name="OCR 1" Value="d.. .. .." />
          <AuditTrail Type="PRINTED"    Side="BACK" Apply="TRUE" Text="MM/DD/YYYY TT NNNNNN  DDDDD"/>
          <AuditTrail Type="ELECTRONIC" Side="BACK" Apply="TRUE" .../>
    ... <Barcode> on ENVELOPE pages ... <EndInfo ... IsModified="FALSE"/>

Rules that matter for acceptance: every status enum is populated (VALID / NO /
NOT_RESCAN / UNDETERMINED / INACTIVE); the Depth-8 JPEG is attached to CHECKS only;
resolution is a fixed 200 dpi for pages, 300 for checks/envelopes; envelopes carry a
Barcode; each page has two audit trails. Round-trips through oxi_reader.open_oxz().
"""
import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET

try:
    from PIL import Image
except Exception:                       # Pillow should be present; degrade safely
    Image = None

# our doc type -> scanner (PageType, PageName), matching real Falcon/OPEX batches.
# The real scanner only distinguishes check / envelope / generic page; downstream
# OCR classifies the rest, so statements / stubs / full-page all become CW_PAGE.
def _page_kind(item_type):
    if item_type == "check":
        return "PERSONAL_CHECK", "CW_CHECK"
    if item_type == "envelope":
        return "ENVELOPE", "CW_ENV"
    return "PAGE", "CW_PAGE"


def rawmicr_to_oxi(raw):
    """Turn a stored codeline into the scanner's (micr, ocr) pair.

    Our library stores rawMicr in the .3xml convention `T<routing>T O<account>O
    <serial>`. Real .oxi uses the codeline-symbol convention: `d<routing>d`
    (transit), `<account>c` (on-us), then the trailing serial — e.g.
    `d111100006d2200222c18480`, with the OCR value spaced. Returns (None, None)
    when there's nothing usable."""
    if not raw:
        return None, None
    transit = re.search(r"T(\d+)T", raw)
    onus = re.search(r"O(\d+)O", raw)
    rest = re.sub(r"O\d+O", " ", re.sub(r"T\d+T", " ", raw))
    trailing = re.findall(r"\d+", rest)
    parts = []
    if transit:
        parts.append(f"d{transit.group(1)}d")
    if onus:
        parts.append(f"{onus.group(1)}c")
    if trailing:
        parts.append(trailing[0])
    if not parts:                                   # already scanner-style / free text
        digits = re.sub(r"\s+", "", raw)
        return (digits, raw.strip()) if digits else (None, None)
    return "".join(parts), "   ".join(parts)


def _img_dims(path):
    """(width_px, height_px) in pixels, or ('','') if unreadable."""
    if not path or not os.path.exists(path) or Image is None:
        return "", ""
    try:
        with Image.open(path) as im:
            return im.width, im.height
    except Exception:
        return "", ""


def _jpeg_front(path):
    """A Depth-8 greyscale JPEG re-encode of the front image -> (bytes, w, h), or
    None. Real scanner batches include this greyscale/colour front (Index=3,
    Depth=8, Format=JPEG) alongside the bitonal front/back TIFFs; our library only
    keeps the bitonal TIFF, so we synthesize the JPEG from it."""
    if not path or not os.path.exists(path) or Image is None:
        return None
    try:
        with Image.open(path) as im:
            g = im.convert("L")                 # 1-bit -> 8-bit greyscale (Depth=8)
            buf = io.BytesIO()
            g.save(buf, format="JPEG", quality=85)
            return buf.getvalue(), g.width, g.height
    except Exception:
        return None


def _phys(px, res=300):
    """Physical page size the scanner records, e.g. 2558 px @ 300 dpi -> '8.527 IN'."""
    try:
        return f"{int(px) / float(res):.3f} IN"
    except (TypeError, ValueError, ZeroDivisionError):
        return ""


def _audit_date(date_dash):
    """'YYYY-MM-DD' -> 'MM/DD/YYYY' for the AuditTrail text."""
    try:
        y, m, d = date_dash.split("-")
        return f"{m}/{d}/{y}"
    except (AttributeError, ValueError):
        return date_dash or ""


def build_oxz(meta, transactions):
    """Return the bytes of a .oxz zip built from a resolved transaction plan.

    The .oxi faithfully reproduces the structure of a real accepted scanner batch
    (Falcon/OPEX): populated status enums, per-page audit trails, envelope
    barcodes, the d/c MICR codeline, and the 0-based image filename convention.

    meta:          batch config dict (batch_type, biller, operator, batch_date…)
    transactions:  list of transactions, each a list of (image, entry) pairs.
    """
    job_name = (meta.get("batch_type") or meta.get("biller") or "QA Batch").strip()
    date = (meta.get("batch_date") or "").replace("/", "-")
    dt = f"{date} 00:00:00" if date else ""
    transport = re.sub(r"\D", "", str(meta.get("transport_id") or "76")) or "76"
    lb_digits = re.sub(r"\D", "", str(meta.get("lockbox") or ""))
    batch_id = (lb_digits or "3809").zfill(9)[-9:]     # e.g. 000003809
    batch_no6 = batch_id[-6:]                            # audit-trail batch field
    audit_date = _audit_date(date)

    batch = ET.Element("Batch", {
        "FormatVersion": "01.50", "BaseMachine": "AS3600_STANDALONE",
        "ScanDevice": "FalconV+_x64", "SoftwareVersion": "06.10.36.00",
        "TransportId": transport, "BatchIdentifier": batch_id,
        "JobType": "UNSTRUCTURED", "OperatingMode": "MANUAL_SCAN",
        "JobName": job_name,
        "OperatorName": meta.get("operator") or "QA Operator",
        "StartTime": dt, "ReceiveDate": date, "ProcessDate": date,
        "ImageFilePath": "", "PluginMessage": "", "DeveloperReserved": "04,01",
    })

    files = {}          # arcname -> bytes  (images placed in the zip)
    seq = 0             # 1-based global counter: DocumentLocator / BatchSequence / ScanSequence
    num_txn = 0

    def add_img(page, index, side, depth, fmt, data, w, h, dpi):
        fname = f"{seq:04d}{index - 1}.{'jpg' if fmt == 'JPEG' else 'tif'}"
        files[fname] = data
        ET.SubElement(page, "Image", {
            "Index": str(index), "RescanStatus": "NOT_RESCAN",
            "ScantimeFinalBlankAreaDecision": "UNDETERMINED",
            "Side": side, "Type": "FULL", "Depth": str(depth), "Format": fmt,
            "Filename": fname, "Filesize": str(len(data)),
            "Length": str(w), "Height": str(h), "OffsetLength": "0", "OffsetHeight": "0",
            "ResolutionLength": str(dpi), "ResolutionHeight": str(dpi),
        })

    # scanner resolution is standard, not whatever the stored TIFF claims:
    # full pages 200 dpi, checks & envelopes 300 dpi — matching real batches.
    def _dpi(is_check, is_env):
        return 300 if (is_check or is_env) else 200

    for ti, txn in enumerate(transactions, start=1):
        num_txn += 1
        tx_el = ET.SubElement(batch, "Transaction", {"TransactionID": f"{ti:04d}"})
        grp_el = ET.SubElement(tx_el, "Group", {"GroupID": "0001"})
        for gi, (image, _entry) in enumerate(txn, start=1):
            seq += 1
            item_type = image.get("item_type") or "unknown"
            page_type, page_name = _page_kind(item_type)
            is_env = item_type == "envelope"
            is_check = item_type == "check"
            dpi = _dpi(is_check, is_env)
            fw, fh = _img_dims(image.get("front_path"))
            page = ET.SubElement(grp_el, "Page", {
                "DocumentLocator": str(seq), "BatchSequence": str(seq),
                "TransactionSequence": str(gi), "GroupSequence": str(gi),
                "ScanSequence": str(seq), "ScanTime": dt,
                "ItemStatus": "VALID", "IsVirtual": "NO", "PageType": page_type,
                "PageName": page_name, "SubPageName": "", "OperatorSelect": "NO",
                "Bin": "1", "Length": _phys(fw, dpi), "Height": _phys(fh, dpi),
                "EnvelopeDetect": "YES" if is_env else "NO",
                "AverageThickness": "1.00", "SkewDegrees": "0.00", "DeskewStatus": "YES",
                "FrontStreakDetectStatus": "INACTIVE", "BackStreakDetectStatus": "INACTIVE",
                "PlugInPageMessage": "",
            })

            # Index 1 = FRONT TIFF, Index 2 = BACK TIFF
            fp = image.get("front_path")
            if fp and os.path.exists(fp):
                with open(fp, "rb") as fh_img:
                    add_img(page, 1, "FRONT", 1, "TIFF", fh_img.read(), fw, fh, dpi)
            bp = image.get("back_path")
            if bp and os.path.exists(bp):
                bw, bh = _img_dims(bp)
                with open(bp, "rb") as fh_img:
                    add_img(page, 2, "BACK", 1, "TIFF", fh_img.read(), bw, bh, dpi)

            # Index 3 = Depth-8 greyscale JPEG of the FRONT — CHECKS ONLY
            # (real batches attach the colour/greyscale front only to checks).
            if is_check:
                jpg = _jpeg_front(fp)
                if jpg:
                    jdata, jw, jh = jpg
                    add_img(page, 3, "FRONT", 8, "JPEG", jdata, jw, jh, dpi)

            # MICR / OCR — checks only
            if is_check:
                micr, ocr = rawmicr_to_oxi(image.get("raw_micr"))
                ET.SubElement(page, "Micr", {
                    "Status": "INACTIVE", "RtStatus": "INACTIVE", "CheckType": "INACTIVE",
                    "Side": "FRONT", "Value": micr or ""})
                ET.SubElement(page, "Ocr", {
                    "Index": "1", "Side": "FRONT", "Value": ocr or micr or "", "Name": "OCR 1"})

            # Barcode — envelopes carry a (blank) USPS barcode element
            if is_env:
                ET.SubElement(page, "Barcode", {
                    "Index": "1", "Type": "USPS Delivery", "Side": "FRONT", "Value": ""})

            # two audit trails per page (printed + electronic), on the BACK
            audit_text = f"{audit_date} {transport} {batch_no6}  {seq:05d}"
            for atype in ("PRINTED", "ELECTRONIC"):
                ET.SubElement(page, "AuditTrail", {
                    "Type": atype, "Side": "BACK", "Apply": "TRUE", "Text": audit_text})

    ET.SubElement(batch, "EndInfo", {
        "EndTime": dt, "NumPages": str(seq), "NumGroups": str(num_txn),
        "NumTransactions": str(num_txn), "IsModified": "FALSE"})

    try:
        ET.indent(batch, space="\t")           # tab-indented, like the real samples
    except AttributeError:
        pass
    oxi_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n\n' + ET.tostring(batch, encoding="utf-8")

    # zip: the .oxi index + every referenced TIFF
    oxi_name = f"{re.sub(r'[^A-Za-z0-9_-]', '_', job_name)[:40] or 'batch'}.oxi"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(oxi_name, oxi_bytes)
        for arcname, data in files.items():
            z.writestr(arcname, data)
    return buf.getvalue()
