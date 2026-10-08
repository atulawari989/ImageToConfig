"""App version + changelog, powering the "What's new" notice after login.

Bump APP_VERSION and add a CHANGELOG entry whenever you ship an update. Each
logged-in user is shown the entries newer than the version they last acknowledged,
then the badge clears when they dismiss it.
"""

APP_VERSION = "1.3"

# Newest first. `version` must match APP_VERSION for the latest entry.
CHANGELOG = [
    {
        "version": "1.3",
        "date": "2026-07-21",
        "title": "Accounts & multi-user",
        "items": [
            "Login / logout with per-user accounts (passwords are hashed).",
            "Your image library and batch history are now private to your account.",
            "Admins can create and manage user accounts.",
            "This 'What's new' notice — see what changed after each update.",
        ],
    },
    {
        "version": "1.2",
        "date": "2026-07-20",
        "title": "Per-transaction batches & scanner output",
        "items": [
            "Compose a different document mix per transaction.",
            "Dedicated Check-List files section to save & reuse those batches.",
            "Complete .oxz scanner batches (full .oxi index + greyscale JPEG).",
            "AI reads dollar amounts off checks, with running plan totals.",
        ],
    },
    {
        "version": "1.1",
        "date": "2026-07-19",
        "title": "AI assistant",
        "items": [
            "Optional Claude AI: classify documents, read MICR & amounts (vision).",
            "Learns a transaction type's real rules from imported .3xml samples.",
            "AI QA-review of a generated batch before you ship it.",
            "Templates reference page; MICR now required on checks before generate.",
        ],
    },
    {
        "version": "1.0",
        "date": "2026-07-18",
        "title": "Core generator",
        "items": [
            "Ingest images / .OXZ, auto-dedup, compose visually.",
            "Generate schema-accurate .3xml / .LBXxml / .78tml / .oxz + bundle.",
            "Plan validated against batch-type rules before any file is written.",
        ],
    },
]


def latest_version():
    return APP_VERSION


def entries_since(last_seen):
    """Changelog entries newer than the version the user last acknowledged.
    If they've never seen one (empty), return just the latest so we don't dump
    the whole history on first login."""
    if not last_seen:
        return CHANGELOG[:1]
    out = []
    for e in CHANGELOG:
        if e["version"] == last_seen:
            break
        out.append(e)
    return out


def has_update(last_seen):
    return bool(last_seen) and last_seen != APP_VERSION
