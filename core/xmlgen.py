"""Generators for .3xml and .LBXxml, matching the real sample files.

Structure/tag names were verified by inspecting (base64 stripped):
  - Good_MICR_Aisha902__5_.3xml   (sanity sample: control-totals blocks)
  - Gainwell...__1_.3xml          (production sample: lockboxNumber + txn customFields)
  - Gen_020_CorrOnly.LBXxml       (flat PascalCase)

Confirmed vs best-effort choices are called out in comments and in the README.
"""
import base64
import json
import os
import time
from xml.sax.saxutils import escape

from . import imaging


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _b64_tiff(path):
    """Base64 of the TIFF, wrapped at 76 chars per line (MIME) with '\\n' —
    matching the real testing sample files (Good_MICR .3xml and the .LBXxml),
    whose <imageData>/<ImageData> blocks are line-wrapped at 76 characters."""
    if not path:
        return None
    return base64.encodebytes(imaging.read_bytes(path)).decode("ascii").rstrip("\n")


def _extra(image):
    try:
        return json.loads(image.get("extra_json") or "{}")
    except Exception:
        return {}


def _file_date(meta):
    """The <fileDate> value: use the user's chosen file date if given, else now.
    A date-only value (no time) gets the current time appended, matching the
    sample's 'YYYY/MM/DD HH:MM:SS' shape."""
    fd = (meta.get("file_date") or "").strip()
    if not fd:
        return time.strftime("%Y/%m/%d %H:%M:%S")
    if ":" not in fd:                       # date only -> add a time component
        return fd + " " + time.strftime("%H:%M:%S")
    return fd


def _images_block(image, indent, front_extra=None):
    """Front (+Back if present) <image> elements.
    `front_extra` is an optional list of pre-indented lines to inject into the
    Front image element (used by .78tml for fileDate/siteName/subsiteName)."""
    out = []
    for side, key in (("Front", "front_path"), ("Back", "back_path")):
        path = image.get(key)
        if not path or not os.path.exists(path):    # skip missing files, don't crash
            continue
        b64 = _b64_tiff(path)
        out.append(f"{indent}<image>")
        if side == "Front" and front_extra:
            out += front_extra
        out.append(f"{indent}  <format>TIFF</format>")
        out.append(f"{indent}  <side>{side}</side>")
        out.append(f"{indent}  <imageData>{b64}</imageData>")
        out.append(f"{indent}</image>")
    return out


# ---------------------------------------------------------------------------
# control totals
# ---------------------------------------------------------------------------
def compute_control_totals(resolved_items):
    """Only checks (payment elements) count as debits in control totals.
    Amounts are always 0.000000 — the original sample files never populate
    controlDebitAmount/controlCreditAmount even when checks have amounts.
    """
    cc = dc = 0
    for image, entry in resolved_items:
        it = image.get("item_type") or entry.get("item_type") or "unknown"
        if it == "check":                 # only real checks are debits
            dc += 1
    return {"cc": cc, "ca": 0, "dc": dc, "da": 0}


def _control_block(ct, indent):
    return [
        f"{indent}<controlCreditCount>{ct['cc']}</controlCreditCount>",
        f"{indent}<controlCreditAmount>{ct['ca']:.6f}</controlCreditAmount>",
        f"{indent}<controlDebitCount>{ct['dc']}</controlDebitCount>",
        f"{indent}<controlDebitAmount>{ct['da']:.6f}</controlDebitAmount>",
    ]


# ---------------------------------------------------------------------------
# .3xml
# ---------------------------------------------------------------------------
def _document_element(image, entry, seq, doc_id, indent):
    """Emit the payment / statement / correspondence / stub element,
    matching the exact field set and order from the original .3xml samples."""
    it = image.get("item_type") or entry.get("item_type") or "unknown"
    L = []
    scanline = (image.get("raw_micr") or "").strip()

    if it in ("statement", "fullpage", "unknown", "envelope"):
        L.append(f"{indent}<statement>")
        L.append(f"{indent}  <documentId>{doc_id}</documentId>")
        L.append(f"{indent}  <sequenceNumber>{seq}</sequenceNumber>")
        L.append(f"{indent}  <ocrLine>{escape(scanline)}</ocrLine>")
        L.append(f"{indent}  <transType>CR</transType>")
        L += _images_block(image, indent + "  ")
        L.append(f"{indent}</statement>")

    elif it == "stub":
        L.append(f"{indent}<stub>")
        L.append(f"{indent}  <documentId>{doc_id}</documentId>")
        L.append(f"{indent}  <sequenceNumber>{seq}</sequenceNumber>")
        L.append(f"{indent}  <ocrLine>{escape(scanline)}</ocrLine>")
        L.append(f"{indent}  <transType>CR</transType>")
        L += _images_block(image, indent + "  ")
        L.append(f"{indent}</stub>")

    elif it == "correspondence":
        L.append(f"{indent}<correspondence>")
        L.append(f"{indent}  <documentId>{doc_id}</documentId>")
        L.append(f"{indent}  <sequenceNumber>{seq}</sequenceNumber>")
        L.append(f"{indent}  <rawMicr>{escape(scanline)}</rawMicr>")
        L += _images_block(image, indent + "  ")
        L.append(f"{indent}</correspondence>")

    else:  # check / unknown -> <payment>
        L.append(f"{indent}<payment>")
        L.append(f"{indent}  <documentId>{doc_id}</documentId>")
        L.append(f"{indent}  <sequenceNumber>{seq}</sequenceNumber>")
        L.append(f"{indent}  <rawMicr>{escape(scanline)}</rawMicr>")
        L.append(f"{indent}  <accountType>Checking</accountType>")
        L.append(f"{indent}  <checkTransType>DR</checkTransType>")
        L.append(f"{indent}  <secCode>ARC</secCode>")
        L += _images_block(image, indent + "  ")
        L.append(f"{indent}</payment>")

    return L


def generate_3xml(meta, transactions, include_control_totals=True):
    """meta keys: batch_type, biller(busUnitName), site, batch_date, lockbox,
    test_file(Y/N), cash_letter_name.

    `transactions` is a list of transactions, each a list of (image, entry).
    A transaction with >1 document gets itemCount>1 (e.g. Singles = check +
    statement grouped together, as in the sanity sample)."""
    site = meta.get("site") or "Worldwide"
    L = ['<?xml version="1.0" encoding="UTF-8"?>', "<file>"]
    L.append(f"  <fileDate>{escape(_file_date(meta))}</fileDate>")
    L.append(f"  <testFile>{meta.get('test_file', 'Y')}</testFile>")
    L.append(f"  <siteName>{escape(site)}</siteName>")
    if meta.get("lockbox"):
        L.append(f"  <lockboxNumber>{escape(str(meta['lockbox']))}</lockboxNumber>")
    L.append("  <cashLetter>")
    L.append(f"    <cashLetterName>{escape(meta.get('cash_letter_name') or 'CLName')}</cashLetterName>")
    L.append("    <batch>")
    L.append(f"      <processDate>{escape(meta.get('batch_date') or time.strftime('%Y/%m/%d'))}</processDate>")
    L.append(f"      <userBatchId>{escape(str(meta.get('user_batch_id', '1')))}</userBatchId>")
    L.append(f"      <transactionType>{escape(meta.get('batch_type') or 'Singles')}</transactionType>")
    if meta.get("biller"):
        L.append(f"      <busUnitName>{escape(meta['biller'])}</busUnitName>")
    L.append(f"      <siteName>{escape(site)}</siteName>")

    flat = []
    for i, txn in enumerate(transactions, start=1):
        L.append("      <transaction>")
        L.append(f"        <itemCount>{len(txn)}</itemCount>")
        L.append(f"        <transactionNumber>{i}</transactionNumber>")
        L.append(f"        <initiatorTraceNum>{i}</initiatorTraceNum>")
        # documentId is the document's position WITHIN the transaction (resets to
        # 1 each transaction), matching the sample — i.e. documentId == sequenceNumber.
        for seq, (image, entry) in enumerate(txn, start=1):
            L += _document_element(image, entry, seq=seq, doc_id=seq, indent="        ")
            flat.append((image, entry))
        L.append("      </transaction>")

    if include_control_totals:
        ct = compute_control_totals(flat)
        L += _control_block(ct, "      ")
    L.append("    </batch>")
    if include_control_totals:
        L += _control_block(ct, "    ")
    L.append("  </cashLetter>")
    if include_control_totals:
        L += _control_block(ct, "  ")
    L.append("</file>")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# .LBXxml
# ---------------------------------------------------------------------------
# ItemType is a numeric code. Observed 2/3/4 in a Correspondence-Only sample,
# independent of whether Dataline held a real scanline or the literal "null".
# So this mapping is BEST-EFFORT / UNCONFIRMED (documented in README).
ITEMTYPE_CODE = {"check": 1, "stub": 2, "statement": 3, "correspondence": 4,
                 "fullpage": 3, "unknown": 2, "envelope": 2}

WORKTYPE_MAP = {
    "Check-Only": "Check-Only",
    "Correspondence-Only": "Correspondence-Only",
    "Stub-Only": "Stub-Only",
    "Singles": "Singles",
    "Multiples": "Multiples",
}


def _lbx_image(path, side):
    b64 = _b64_tiff(path)
    return (f"<Image><Format>TIFF</Format><Side>{side}</Side>"
            f"<ImageData>{b64}</ImageData></Image>")


def generate_lbxxml(meta, resolved_items):
    """Flat PascalCase .LBXxml matching Gen_020_CorrOnly.LBXxml."""
    work_type = WORKTYPE_MAP.get(meta.get("batch_type"), meta.get("batch_type") or "Correspondence-Only")
    process_date = (meta.get("batch_date") or time.strftime("%Y/%m/%d")).replace("/", "").replace("-", "")

    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<File>"]
    parts.append(f"<FileDate>{time.strftime('%Y-%m-%d %H:%M:%S')}</FileDate>")
    parts.append("<Batch>")
    parts.append(f"<BatchNumber>{escape(meta.get('batch_number') or (meta.get('biller') or 'Batch') + '_1')}</BatchNumber>")
    parts.append("<BusinessUnitId>-1</BusinessUnitId>")
    parts.append(f"<BusinessUnitName>{escape(meta.get('biller') or 'BusinessUnit LBX')}</BusinessUnitName>")
    parts.append("<Scanner>0</Scanner>")
    parts.append(f"<WorkType>{escape(work_type)}</WorkType>")
    parts.append(f"<Operator>{escape(meta.get('operator') or 'QA Operator')}</Operator>")
    parts.append("<SiteId>-1</SiteId>")
    parts.append(f"<SiteName>{escape(meta.get('site') or 'FSB_Processor')}</SiteName>")
    parts.append(f"<BatchEntryProperties>checkBursting=true,processDate={process_date}</BatchEntryProperties>")

    credit = debit = 0
    seq = int(meta.get("start_seq", 65030))
    for image, entry in resolved_items:
        it = image.get("item_type") or "unknown"
        code = ITEMTYPE_CODE.get(it, 2)
        dataline = image.get("raw_micr") or "null"
        # checks are debits (DR); remittance docs (stub/statement/full-page) are
        # credits (CR), matching the .3xml transType convention.
        if it == "check":
            debit += 1
        elif it in ("statement", "stub", "fullpage"):
            credit += 1
        item = [f"<Item><DailyItemSeqNum>{seq}</DailyItemSeqNum><ItemType>{code}</ItemType>"]
        item.append(f"<Dataline>{escape(dataline)}</Dataline>")
        if image.get("front_path") and os.path.exists(image["front_path"]):
            item.append(_lbx_image(image["front_path"], "Front"))
        if image.get("back_path") and os.path.exists(image["back_path"]):
            item.append(_lbx_image(image["back_path"], "Back"))
        item.append("</Item>")
        parts.append("".join(item))
        seq += 10

    parts.append("</Batch>")
    parts.append(f"<DebitCount>{debit}</DebitCount><CreditCount>{credit}</CreditCount>")
    parts.append("</File>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# .78tml
# ---------------------------------------------------------------------------
def _78tml_document_element(image, entry, seq, doc_id, indent, meta):
    """Like the .3xml document element, but the Front image of payment elements
    gets <fileDate>, <siteName>, <subsiteName> injected inside <image>."""
    it = image.get("item_type") or entry.get("item_type") or "unknown"
    L = []
    scanline = (image.get("raw_micr") or "").strip()

    front_extra = None
    if it == "check":
        img_indent = indent + "    "
        front_extra = [
            f"{img_indent}<fileDate>{escape(_file_date(meta))}</fileDate>",
            f"{img_indent}<siteName>{escape(meta.get('site') or 'Worldwide')}</siteName>",
            f"{img_indent}<subsiteName>{escape(meta.get('subsite') or '')}</subsiteName>",
        ]

    if it in ("statement", "fullpage", "unknown", "envelope"):
        L.append(f"{indent}<statement>")
        L.append(f"{indent}  <documentId>{doc_id}</documentId>")
        L.append(f"{indent}  <sequenceNumber>{seq}</sequenceNumber>")
        L.append(f"{indent}  <ocrLine>{escape(scanline)}</ocrLine>")
        L.append(f"{indent}  <transType>CR</transType>")
        L += _images_block(image, indent + "  ")
        L.append(f"{indent}</statement>")

    elif it == "stub":
        L.append(f"{indent}<stub>")
        L.append(f"{indent}  <documentId>{doc_id}</documentId>")
        L.append(f"{indent}  <sequenceNumber>{seq}</sequenceNumber>")
        L.append(f"{indent}  <ocrLine>{escape(scanline)}</ocrLine>")
        L.append(f"{indent}  <transType>CR</transType>")
        L += _images_block(image, indent + "  ")
        L.append(f"{indent}</stub>")

    elif it == "correspondence":
        L.append(f"{indent}<correspondence>")
        L.append(f"{indent}  <documentId>{doc_id}</documentId>")
        L.append(f"{indent}  <sequenceNumber>{seq}</sequenceNumber>")
        L.append(f"{indent}  <rawMicr>{escape(scanline)}</rawMicr>")
        L += _images_block(image, indent + "  ")
        L.append(f"{indent}</correspondence>")

    else:
        L.append(f"{indent}<payment>")
        L.append(f"{indent}  <documentId>{doc_id}</documentId>")
        L.append(f"{indent}  <sequenceNumber>{seq}</sequenceNumber>")
        L.append(f"{indent}  <rawMicr>{escape(scanline)}</rawMicr>")
        L.append(f"{indent}  <accountType>Checking</accountType>")
        L.append(f"{indent}  <checkTransType>DR</checkTransType>")
        L.append(f"{indent}  <secCode>ARC</secCode>")
        L += _images_block(image, indent + "  ", front_extra=front_extra)
        L.append(f"{indent}</payment>")

    return L


def generate_78tml(meta, transactions, include_control_totals=True):
    """Like .3xml but: no <fileDate> at file level, no <siteName> at batch
    level; instead <fileDate>/<siteName>/<subsiteName> appear inside each
    payment's Front <image> element."""
    site = meta.get("site") or "Worldwide"
    L = ['<?xml version="1.0" encoding="UTF-8"?>', "<file>"]
    L.append("  ")
    L.append(f"  <testFile>{meta.get('test_file', 'Y')}</testFile>")
    L.append(f"  <siteName>{escape(site)}</siteName>")
    if meta.get("lockbox"):
        L.append(f"  <lockboxNumber>{escape(str(meta['lockbox']))}</lockboxNumber>")
    L.append("  <cashLetter>")
    L.append(f"    <cashLetterName>{escape(meta.get('cash_letter_name') or 'CLName')}</cashLetterName>")
    L.append("    <batch>")
    L.append(f"      <processDate>{escape(meta.get('batch_date') or time.strftime('%Y/%m/%d'))}</processDate>")
    L.append(f"      <userBatchId>{escape(str(meta.get('user_batch_id', '1')))}</userBatchId>")
    L.append(f"      <transactionType>{escape(meta.get('batch_type') or 'Singles')}</transactionType>")
    if meta.get("biller"):
        L.append(f"      <busUnitName>{escape(meta['biller'])}</busUnitName>")
    L.append("      ")

    flat = []
    for i, txn in enumerate(transactions, start=1):
        L.append("      <transaction>")
        L.append(f"        <itemCount>{len(txn)}</itemCount>")
        L.append(f"        <transactionNumber>{i}</transactionNumber>")
        L.append(f"        <initiatorTraceNum>{i}</initiatorTraceNum>")
        # documentId resets per transaction (== sequenceNumber), as in .3xml.
        for seq, (image, entry) in enumerate(txn, start=1):
            L += _78tml_document_element(image, entry, seq=seq, doc_id=seq,
                                         indent="        ", meta=meta)
            flat.append((image, entry))
        L.append("      </transaction>")

    if include_control_totals:
        ct = compute_control_totals(flat)
        L += _control_block(ct, "      ")
    L.append("    </batch>")
    if include_control_totals:
        L += _control_block(ct, "    ")
    L.append("  </cashLetter>")
    if include_control_totals:
        L += _control_block(ct, "  ")
    L.append("</file>")
    return "\n".join(L)
