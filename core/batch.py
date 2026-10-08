"""Batch-plan operations.

The plan is a list of **transactions**; each transaction is a list of entries:

    plan = [
        [ {"image_id":1,"item_type":"check","amount":"10.00"},
          {"image_id":7,"item_type":"stub","amount":"0.00"} ],   # transaction 1
        [ {"image_id":9,"item_type":"check","amount":"5.00"} ],  # transaction 2
    ]

This lets the user say exactly how many transactions they want and how many
checks / statements / correspondence go in each one (see `compose`). All
operations are pure: they take the plan and return a new plan, so it round-trips
cleanly through the Flask session.
"""
import copy
import itertools


def new_plan():
    return []


def entry(image_id, item_type="unknown", amount="0.00"):
    return {"image_id": int(image_id), "item_type": item_type, "amount": amount or "0.00"}


def add_single(plan, image_id, item_type="unknown", amount="0.00"):
    """Add one image as its own new transaction (the '+ Batch' quick-add)."""
    plan = _clone(plan)
    plan.append([entry(image_id, item_type, amount)])
    return plan


def add_to_transaction(plan, txn_index, image_id, item_type="unknown", amount="0.00", qty=1):
    plan = _clone(plan)
    if 0 <= txn_index < len(plan):
        for _ in range(max(1, int(qty))):
            plan[txn_index].append(entry(image_id, item_type, amount))
    return plan


def add_empty_transaction(plan):
    plan = _clone(plan)
    plan.append([])
    return plan


def remove_transaction(plan, txn_index):
    plan = _clone(plan)
    if 0 <= txn_index < len(plan):
        plan.pop(txn_index)
    return plan


def remove_item(plan, txn_index, item_index):
    plan = _clone(plan)
    if 0 <= txn_index < len(plan) and 0 <= item_index < len(plan[txn_index]):
        plan[txn_index].pop(item_index)
        if not plan[txn_index]:          # drop empty transaction
            plan.pop(txn_index)
    return plan


def multiply_transaction(plan, txn_index, total):
    """Repeat a whole transaction so it occurs `total` times (volume testing)."""
    plan = _clone(plan)
    if not (0 <= txn_index < len(plan)):
        return plan
    total = max(0, int(total))
    anchor = copy.deepcopy(plan[txn_index])
    if total == 0:
        plan.pop(txn_index)
        return plan
    plan[txn_index:txn_index + 1] = [copy.deepcopy(anchor) for _ in range(total)]
    return plan


def update_item(plan, txn_index, item_index, item_type=None, amount=None):
    plan = _clone(plan)
    if 0 <= txn_index < len(plan) and 0 <= item_index < len(plan[txn_index]):
        e = dict(plan[txn_index][item_index])
        if item_type is not None:
            e["item_type"] = item_type
        if amount is not None:
            e["amount"] = amount
        plan[txn_index][item_index] = e
    return plan


def move_transaction(plan, txn_index, delta):
    plan = _clone(plan)
    j = txn_index + delta
    if 0 <= txn_index < len(plan) and 0 <= j < len(plan):
        plan[txn_index], plan[j] = plan[j], plan[txn_index]
    return plan


def clear(plan=None):
    return []


def compose(pools, n_transactions, per_txn):
    """Build a plan of `n_transactions` transactions, each holding the requested
    number of each document role.

    pools:   {"check": [img,...], "stub": [img,...], "corr": [img,...]}
             (img is a library dict with id/item_type/amount)
    per_txn: {"check": n, "stub": n, "corr": n}

    Images are cycled from their pool if a transaction needs more than the pool
    has. Returns (plan, warnings).
    """
    warnings = []
    cyclers = {}
    for role, pool in pools.items():
        cyclers[role] = itertools.cycle(pool) if pool else None

    order = [("check", "check"), ("statement", "statement"),
             ("fullpage", "fullpage"), ("stub", "stub"), ("corr", "correspondence")]
    plan = []
    for _ in range(max(0, int(n_transactions))):
        txn = []
        for role, _label in order:
            count = int(per_txn.get(role, 0) or 0)
            if count <= 0:
                continue
            if not pools.get(role):
                warnings.append(f"No '{role}' images available — skipped {count} per transaction.")
                continue
            for _ in range(count):
                img = next(cyclers[role])
                txn.append(entry(img["id"], img["item_type"], img.get("amount") or "0.00"))
        if txn:
            plan.append(txn)
    # de-dupe warnings, preserve order
    seen = set()
    warnings = [w for w in warnings if not (w in seen or seen.add(w))]
    return plan, warnings


def grouped(txn):
    """Collapse a transaction's documents into rows of identical items with a
    count, so '1 check + 900 statements' shows as two rows (×1, ×900) instead
    of 901. Grouped by (image_id, item_type, amount), in first-seen order."""
    order, index = [], {}
    for e in txn:
        k = (e["image_id"], e["item_type"], str(e["amount"]))
        if k not in index:
            index[k] = len(order)
            order.append({"image_id": e["image_id"], "item_type": e["item_type"],
                          "amount": e["amount"], "count": 0})
        order[index[k]]["count"] += 1
    return order


def _rebuild(groups):
    out = []
    for g in groups:
        for _ in range(max(0, int(g["count"]))):
            out.append(entry(g["image_id"], g["item_type"], g["amount"]))
    return out


def move_group(plan, ti, image_id, o_type, o_amount, delta):
    """Reorder documents WITHIN a transaction: move the grouped row up/down so
    the user can swap item order (e.g. put the check before the statement)."""
    plan = _clone(plan)
    if not (0 <= ti < len(plan)):
        return plan
    groups = grouped(plan[ti])
    idx = next((i for i, g in enumerate(groups)
                if g["image_id"] == image_id and g["item_type"] == o_type
                and str(g["amount"]) == str(o_amount)), None)
    if idx is None:
        return plan
    j = idx + delta
    if 0 <= j < len(groups):
        groups[idx], groups[j] = groups[j], groups[idx]
        plan[ti] = _rebuild(groups)
    return plan


def group_set(plan, ti, image_id, o_type, o_amount, new_type=None, new_amount=None, qty=None):
    """Update one grouped row in transaction `ti` (identified by its current
    image_id / type / amount): change its type, amount and/or repeat-count.
    qty=0 removes the row; emptying the transaction removes it."""
    plan = _clone(plan)
    if not (0 <= ti < len(plan)):
        return plan
    new_entries = []
    for g in grouped(plan[ti]):
        match = (g["image_id"] == image_id and g["item_type"] == o_type
                 and str(g["amount"]) == str(o_amount))
        it = new_type if (match and new_type is not None) else g["item_type"]
        am = new_amount if (match and new_amount is not None) else g["amount"]
        cnt = qty if (match and qty is not None) else g["count"]
        for _ in range(max(0, int(cnt))):
            new_entries.append(entry(g["image_id"], it, am))
    if new_entries:
        plan[ti] = new_entries
    else:
        plan.pop(ti)
    return plan


def flatten(plan):
    return [e for txn in plan for e in txn]


def count_items(plan):
    return sum(len(txn) for txn in plan)


def _clone(plan):
    return [list(txn) for txn in plan]
