"""Import images from an existing .3xml / .LBXxml batch file.

These files embed base64 TIFF images (the same ones the app generates), so a QA
engineer can drop a real .3xml in, pull every document image into the library,
and reuse them to compose new batches.

Returns a list of documents, each:
    {"item_type": "check|statement|stub|correspondence|unknown",
     "scanline": <rawMicr/ocrLine/Dataline or None>,
     "front": <bytes>, "back": <bytes|None>}
"""
import base64
import xml.etree.ElementTree as ET

# .3xml document elements -> our types
_DOC_TAGS_3XML = {
    "payment": "check",
    "statement": "statement",
    "stub": "stub",
    "correspondence": "correspondence",
}

# .LBXxml numeric ItemType -> our types (best-effort; the mapping is unconfirmed,
# so the user can correct the type in the library afterwards).
_ITEMTYPE_TO_TYPE = {"1": "check", "2": "stub", "3": "statement", "4": "correspondence"}


def _b64(text):
    if not text:
        return None
    try:
        return base64.b64decode(text)  # whitespace/newlines are ignored
    except Exception:
        return None


def _pick_images(el, image_tag, side_tag, data_tag):
    front = back = None
    for img in el.findall(image_tag):
        side = (img.findtext(side_tag) or "").strip().lower()
        data = _b64(img.findtext(data_tag))
        if data is None:
            continue
        if side == "front" and front is None:
            front = data
        elif side == "back" and back is None:
            back = data
        elif front is None:
            front = data
        elif back is None:
            back = data
    return front, back


def _parse_3xml(root):
    docs = []
    for tag, itype in _DOC_TAGS_3XML.items():
        for el in root.iter(tag):
            front, back = _pick_images(el, "image", "side", "imageData")
            if front is None:
                continue
            scan = el.findtext("rawMicr") or el.findtext("ocrLine")
            docs.append({"item_type": itype,
                         "scanline": (scan or "").strip() or None,
                         "front": front, "back": back})
    return docs


def _parse_lbxxml(root):
    docs = []
    for item in root.iter("Item"):
        code = (item.findtext("ItemType") or "").strip()
        itype = _ITEMTYPE_TO_TYPE.get(code, "unknown")
        front, back = _pick_images(item, "Image", "Side", "ImageData")
        if front is None:
            continue
        dl = (item.findtext("Dataline") or "").strip()
        scan = None if (not dl or dl.lower() == "null") else dl
        docs.append({"item_type": itype, "scanline": scan,
                     "front": front, "back": back})
    return docs


def parse_batch_xml(data):
    """data: raw bytes of a .3xml or .LBXxml file.
    Returns (kind, docs) where kind is '3xml' | 'LBXxml' | None."""
    # ET needs bytes when there's an encoding declaration; it also handles a BOM.
    root = ET.fromstring(data)
    docs = _parse_3xml(root)
    if docs:
        return "3xml", docs
    docs = _parse_lbxxml(root)
    if docs:
        return "LBXxml", docs
    return None, []


def analyze_batch_structure(data):
    """Analyse a transaction-structured batch (.3xml / .78tml) so the app can LEARN
    the rule for its transaction type from the real file instead of guessing.

    Returns a dict, or None if the file has no <transaction> structure:
      {
        "transaction_type": str | None,   # <transactionType>
        "doc_types": set,                  # our item types present (check/statement/…)
        "group": "one_each" | "check_plus_stub" | "multiples",
        "item_counts": [int, ...],         # docs per transaction
      }
    """
    try:
        root = ET.fromstring(data)
    except Exception:
        return None
    txns = root.findall(".//transaction")
    if not txns:
        return None
    ttype = (root.findtext(".//transactionType") or "").strip() or None
    doc_types = set()
    per_txn = []
    for txn in txns:
        counts = {"check": 0, "statement": 0, "stub": 0, "correspondence": 0}
        for tag, itype in _DOC_TAGS_3XML.items():
            n = len(txn.findall(tag))
            if n:
                counts[itype] += n
                doc_types.add(itype)
        per_txn.append(counts)
    totals = [sum(c.values()) for c in per_txn]

    if totals and all(t == 1 for t in totals):
        group = "one_each"
    elif totals and all(c["check"] == 1 and (c["statement"] + c["stub"]) == 1
                        and sum(c.values()) == 2 for c in per_txn):
        group = "check_plus_stub"
    else:
        group = "multiples"

    return {"transaction_type": ttype, "doc_types": doc_types,
            "group": group, "item_counts": totals}


def looks_like_xml(data):
    head = data[:200].lstrip(b"\xef\xbb\xbf").lstrip()
    return head[:1] == b"<"
