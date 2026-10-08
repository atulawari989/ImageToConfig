"""USA lockbox / remittance batch-type rules.

Each batch (work) type constrains **which document types** it may contain and
**how documents are grouped into transactions**. Getting this wrong produces a
file the downstream system rejects — e.g. a `Singles` batch must place the
check and its stub/statement together in ONE transaction, which is confirmed by
the sanity sample (`Good_MICR...3xml`: one <transaction> with <itemCount>2</itemCount>
holding a <payment> + a <statement>).

Rules backed by a sample are marked confirmed=True. The rest follow standard
lockbox semantics and are best-effort (confirmed=False) — adjust as needed.

Document roles:
  check                -> <payment>       (a debit item, carries MICR)
  stub / statement     -> <stub>/<statement>  (remittance coupon / statement)
  correspondence       -> <correspondence>
"""

CHECK = {"check"}
STUB = {"stub", "statement", "fullpage"}   # fullpage groups like a statement
CORR = {"correspondence"}


# group strategies (see _group_* below): how a flat, ordered item list becomes
# a list of transactions (each transaction is a list of items).
# USA lockbox / remittance work-type matrix. "Remittance" = a stub, statement,
# or full-page document (coupon / invoice / statement page). These allowed sets
# and structures are ADVISORY — the app warns on a mismatch but still lets the
# user proceed (a real operator may knowingly build an off-spec batch).
_REMIT = {"stub", "statement", "fullpage"}          # non-check payment documents
_PAY = {"check"}

RULES = {
    "Singles": {
        "allowed": _PAY | _REMIT,
        "group": "check_plus_stub",
        "desc": "Retail single: each transaction = 1 check + 1 remittance (stub / statement / full-page).",
        "confirmed": True,
    },
    "Multiples": {
        "allowed": _PAY | _REMIT | {"correspondence"},
        "group": "multiples",
        "desc": "Wholesale / multiple: 1 check paying several remittances — a check plus the documents that follow it.",
        "confirmed": False,
    },
    "Check-Only": {
        "allowed": set(_PAY),
        "group": "one_each",
        "desc": "Checks only — one check per transaction, no remittance.",
        "confirmed": True,
    },
    "Deposit Only": {
        "allowed": set(_PAY),
        "group": "one_each",
        "desc": "Deposit: checks only — one check per transaction, no remittance.",
        "confirmed": False,
    },
    "Stub-Only": {
        "allowed": set(_REMIT),
        "group": "one_each",
        "desc": "Remittance only — one stub / statement / full-page per transaction, no check.",
        "confirmed": True,
    },
    "Correspondence-Only": {
        "allowed": {"correspondence", "fullpage"},
        "group": "one_each",
        "desc": "Non-payment documents only — one correspondence / letter per transaction.",
        "confirmed": True,
    },
    "Pmt Notice": {
        "allowed": set(_REMIT),
        "group": "one_each",
        "desc": "Payment notices — one remittance notice per transaction, no check.",
        "confirmed": False,
    },
    "Bill": {
        "allowed": set(_REMIT),
        "group": "one_each",
        "desc": "Bills / invoices — one billing document per transaction, no check.",
        "confirmed": False,
    },
    "AR-Only": {
        "allowed": set(_REMIT),
        "group": "one_each",
        "desc": "Accounts-receivable documents — one per transaction, no check.",
        "confirmed": False,
    },
    "Purchases": {
        "allowed": _PAY | _REMIT,
        "group": "multiples",
        "desc": "Purchases — a check with its purchase remittance(s).",
        "confirmed": False,
    },
    "Check-List": {
        "allowed": _PAY | _REMIT,
        "group": "multiples",
        "desc": "Check-list: checks + remittances (statement / stub / full-page), multiple "
                "documents per transaction (confirmed from a real Check-List sample).",
        "confirmed": True,
    },
    "Push": {
        "allowed": _PAY | _REMIT | {"correspondence"},
        "group": "multiples",
        "desc": "Push — any document mix, one or more documents per transaction.",
        "confirmed": False,
    },
    "Image-Only": {
        "allowed": _PAY | _REMIT | {"correspondence"},
        "group": "multiples",
        "desc": "Image capture — any document type, one or more per transaction.",
        "confirmed": False,
    },
    "CSP-Add-Stub": {
        "allowed": _PAY | _REMIT,
        "group": "check_plus_stub",
        "desc": "CSP add-stub — check + one added stub per transaction.",
        "confirmed": False,
    },
    "CSP-With-Stub": {
        "allowed": _PAY | _REMIT,
        "group": "check_plus_stub",
        "desc": "CSP with-stub — check + its stub per transaction.",
        "confirmed": False,
    },
}

DEFAULT_RULE = {
    "allowed": {"check", "stub", "statement", "fullpage", "correspondence"},
    "group": "one_each",
    "desc": "One document per transaction.",
    "confirmed": False,
}

# "fullpage" is a statement-style document — allow it anywhere statements are.
for _r in RULES.values():
    if "statement" in _r["allowed"]:
        _r["allowed"].add("fullpage")


def rule_for(batch_type):
    return RULES.get(batch_type, DEFAULT_RULE)


def validate_composition(batch_type, per_txn):
    """Check a per-transaction document mix against the batch-type rule, so the
    compose step can't build a structurally-invalid batch.

    per_txn: {"check": n, "statement": n, "stub": n, "corr": n}
    Returns a list of error strings (empty = OK).

      - check_plus_stub (Singles, CSP-*): exactly 1 check + 1 statement-or-stub,
        no correspondence.
      - one_each (Check-Only, Stub-Only, Correspondence-Only, ...): exactly 1
        document per transaction, of an allowed type.
      - multiples: any mix of allowed types (no structural constraint).
    """
    rule = rule_for(batch_type)
    allowed = rule["allowed"]
    chk = int(per_txn.get("check", 0) or 0)
    stmt = int(per_txn.get("statement", 0) or 0)
    fp = int(per_txn.get("fullpage", 0) or 0)
    stub = int(per_txn.get("stub", 0) or 0)
    corr = int(per_txn.get("corr", 0) or 0)
    total = chk + stmt + fp + stub + corr
    errors = []

    used = ([("check", chk)] if chk else []) + ([("statement", stmt)] if stmt else []) \
        + ([("fullpage", fp)] if fp else []) \
        + ([("stub", stub)] if stub else []) + ([("correspondence", corr)] if corr else [])
    if allowed is not None:
        bad = [name for name, _ in used if name not in allowed]
        if bad:
            errors.append(f"'{batch_type}' does not allow: {', '.join(bad)}. "
                          f"Allowed here: {', '.join(sorted(allowed))}.")
            return errors

    grp = rule["group"]
    if grp == "check_plus_stub":
        # full-page docs count on the stub/statement side of a check+stub pairing
        if not (chk == 1 and (stmt + fp + stub) == 1 and corr == 0):
            errors.append(
                f"'{batch_type}' needs exactly 1 check + 1 statement/stub per transaction. "
                f"Set Checks=1 and either Statements=1 or Stubs=1 (use 'How many "
                f"transactions' for volume) — a transaction can't hold {total} documents.")
    elif grp == "one_each":
        if total != 1:
            errors.append(
                f"'{batch_type}' allows exactly 1 document per transaction. "
                f"Set one count to 1 and the rest to 0 (use 'How many transactions' for volume).")
    return errors


def validate_transactions(batch_type, transactions):
    """Validate an explicit transaction plan (built by compose OR by hand) against
    the batch type — both allowed types and per-transaction structure.

      - check_plus_stub (Singles, CSP-*): each transaction = 1 check + 1 stub/statement.
      - one_each (Check-Only, ...): each transaction = exactly 1 document.
      - multiples: any number of allowed docs per transaction (e.g. 1 check + N statements).

    Returns (errors, warnings).
    """
    rule = rule_for(batch_type)
    allowed = rule["allowed"]
    errors, warnings = [], []

    # 1) allowed-type check
    bad = []
    for txn in transactions:
        for image, _e in txn:
            it = image.get("item_type") or "unknown"
            if it == "unknown":
                bad.append(f"#{image.get('id')} (type is 'unknown')")
            elif allowed is not None and it not in allowed:
                bad.append(f"#{image.get('id')} is '{it}'")
    if bad:
        seen = set()
        bad = [b for b in bad if not (b in seen or seen.add(b))]
        allowed_str = ", ".join(sorted(allowed)) if allowed else "any"
        errors.append(f"'{batch_type}' allows only [{allowed_str}]. These don't fit: {', '.join(bad)}.")
        return errors, warnings

    # 2) per-transaction structure
    grp = rule["group"]
    for i, txn in enumerate(transactions, start=1):
        chk = sum(1 for p in txn if _role(p) == "check")
        stub = sum(1 for p in txn if _role(p) == "stub")   # stub OR statement
        corr = sum(1 for p in txn if _role(p) == "corr")
        total = len(txn)
        if grp == "check_plus_stub" and not (chk == 1 and stub == 1 and corr == 0):
            errors.append(f"Transaction {i}: '{batch_type}' requires exactly 1 check + 1 "
                          f"statement/stub (got {total} document(s)).")
        elif grp == "one_each" and total != 1:
            errors.append(f"Transaction {i}: '{batch_type}' allows exactly 1 document "
                          f"per transaction (got {total}).")
        # 'multiples' (and unknown types) impose no structural limit

    if not rule["confirmed"] and not errors:
        warnings.append(f"Batch type '{batch_type}': {rule['desc']} (best-effort — no sample).")
    return errors, warnings


def validate_types(batch_type, transactions):
    """Validate that every document in an explicit transaction plan is an
    allowed type for this batch type. Grouping is defined by the plan itself
    (the user's composition), so we only police the *types* here.

    transactions: list of transactions, each a list of (image, entry).
    Returns (errors, warnings).
    """
    rule = rule_for(batch_type)
    allowed = rule["allowed"]
    errors, warnings = [], []
    bad = []
    for txn in transactions:
        for image, _e in txn:
            it = image.get("item_type") or "unknown"
            if it == "unknown":
                bad.append(f"#{image.get('id')} (type is 'unknown' — set a document type)")
            elif allowed is not None and it not in allowed:
                bad.append(f"#{image.get('id')} is '{it}'")
    if bad:
        allowed_str = ", ".join(sorted(allowed)) if allowed else "any"
        # de-dupe, keep order
        seen = set()
        bad = [b for b in bad if not (b in seen or seen.add(b))]
        errors.append(f"'{batch_type}' allows only [{allowed_str}]. "
                      f"These don't fit: {', '.join(bad)}.")
    if not rule["confirmed"]:
        warnings.append(f"Batch type '{batch_type}': {rule['desc']} (best-effort — no sample).")
    return errors, warnings


def _role(pair):
    """pair is (image, entry); classify by the image's item_type."""
    item_type = (pair[0].get("item_type") or "unknown")
    if item_type in CHECK:
        return "check"
    if item_type in STUB:
        return "stub"
    if item_type in CORR:
        return "corr"
    return "other"


# --- grouping strategies -----------------------------------------------------
def _group_one_each(items):
    return [[it] for it in items]


def _group_check_plus_stub(items):
    """Pair each check with a stub/statement, in plan order → 1 transaction each.

    Returns (transactions, error_or_None).
    """
    checks = [it for it in items if _role(it) == "check"]
    stubs = [it for it in items if _role(it) == "stub"]
    if len(checks) != len(stubs):
        return None, (f"needs an equal number of checks and stubs/statements "
                      f"(plan has {len(checks)} check(s) and {len(stubs)} stub/statement(s)). "
                      f"Add or remove items so every check has one stub.")
    txns = [[c, s] for c, s in zip(checks, stubs)]
    return txns, None


def _group_multiples(items):
    """Each check starts a new transaction; following stubs attach to it.

    Leading stubs (before any check) become their own transaction.
    """
    txns = []
    cur = []
    for it in items:
        if _role(it) == "check":
            if cur:
                txns.append(cur)
            cur = [it]
        else:
            if not cur:
                cur = [it]
            else:
                cur.append(it)
    if cur:
        txns.append(cur)
    return txns, None


def validate_and_group(batch_type, resolved):
    """resolved: list of (image, entry). Returns (transactions, errors, warnings).

    `transactions` is a list of lists of (image, entry), or None if invalid.
    Each item's effective type is image['item_type'].
    """
    rule = rule_for(batch_type)
    errors, warnings = [], []

    if not resolved:
        return None, ["The batch plan is empty — add items first."], []

    # 1) type check
    allowed = rule["allowed"]
    bad = []
    for image, _e in resolved:
        it = image.get("item_type") or "unknown"
        if it == "unknown":
            bad.append(f"#{image.get('id')} (type is 'unknown' — set a document type)")
        elif allowed is not None and it not in allowed:
            bad.append(f"#{image.get('id')} is '{it}'")
    if bad:
        allowed_str = ", ".join(sorted(allowed)) if allowed else "any"
        errors.append(f"'{batch_type}' allows only [{allowed_str}]. "
                      f"These don't fit: {', '.join(bad)}.")

    if errors:
        return None, errors, warnings

    # 2) grouping
    strat = rule["group"]
    if strat == "check_plus_stub":
        txns, err = _group_check_plus_stub([it for it in resolved])
        if err:
            errors.append(f"'{batch_type}': {err}")
            return None, errors, warnings
    elif strat == "multiples":
        txns, _ = _group_multiples([it for it in resolved])
    else:
        txns = _group_one_each([it for it in resolved])

    if not rule["confirmed"]:
        warnings.append(f"Batch type '{batch_type}' grouping is best-effort "
                        f"(no sample to confirm). {rule['desc']}")

    return txns, errors, warnings
