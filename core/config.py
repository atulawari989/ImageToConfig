"""Central paths and small config helpers for ImageToConfig."""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
LIBRARY_DIR = os.path.join(DATA_DIR, "library")   # stored image bytes (TIFF)
OUTPUT_DIR = os.path.join(DATA_DIR, "outputs")     # generated .3xml/.LBXxml/bundle.zip
REFERENCE_DIR = os.path.join(DATA_DIR, "reference")  # saved reference batch files (e.g. Check-List)
DB_PATH = os.path.join(DATA_DIR, "app.db")

# Check-List workspace (/cl/*): a fully separate image pool + output folder from
# the main library/outputs above, so the two never share files on disk.
CL_LIBRARY_DIR = os.path.join(DATA_DIR, "checklist_library")
CL_OUTPUT_DIR = os.path.join(DATA_DIR, "checklist_outputs")

# Document types used throughout the app. These map onto the concrete XML
# elements in the generators (see core/xmlgen.py). "fullpage" is a full-page
# document (invoice / letter / statement-style page) — it generates like a
# statement and needs no MICR line.
ITEM_TYPES = ["check", "stub", "statement", "fullpage", "correspondence",
              "unknown", "envelope"]

# Friendly labels for the type dropdowns (token -> display). Anything not listed
# just shows its raw token.
ITEM_TYPE_LABELS = {"fullpage": "Full Page Doc"}

# Batch (transaction) types confirmed / listed from the sample prompt.
BATCH_TYPES = [
    "Singles", "Multiples", "Check-Only", "Correspondence-Only", "Stub-Only",
    "Deposit Only", "Push", "Image-Only", "Pmt Notice", "CSP-Add-Stub",
    "CSP-With-Stub", "Bill", "AR-Only", "Purchases", "Check-List",
]


def ensure_dirs():
    for d in (DATA_DIR, LIBRARY_DIR, OUTPUT_DIR, REFERENCE_DIR, CL_LIBRARY_DIR, CL_OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)
