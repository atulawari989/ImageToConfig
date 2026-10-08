# ImageToConfig

A local web app for QA engineers who test a lockbox / check-processing pipeline
(ClearingWorks / CheckAlt-style). Instead of hand-building test batches, upload
scanned images or a real scanner batch (`.OXZ`), keep a deduped image library,
assemble a batch (with volume multiply / insert / replace), and generate
schema-accurate **`.3xml`** and **`.LBXxml`** files plus a convenience bundle.

---

## Run it

```bash
# from the project folder
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on *nix)
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000**.

> A virtual environment already exists at `.venv` if you set the project up
> through the installer flow — you can skip straight to `python app.py`.

### OCR (optional)

OCR/MICR auto-read uses **Tesseract**. If the Tesseract binary isn't installed
the app still runs fully — the top-right badge shows *"OCR: manual"* and you
just type the MICR/fields in yourself (which QA does anyway, since the read is
best-effort). To enable auto-read, install Tesseract:

```
winget install UB-Mannheim.TesseractOCR
```

The app looks for it on `PATH` and at the usual
`C:\Program Files\Tesseract-OCR\tesseract.exe`. You can also point at it with
the `TESSERACT_CMD` environment variable.

---

## What it does

1. **Upload images** (TIFF/PNG/JPG) *or* a **`.OXZ`** scanner batch, which is
   unpacked automatically.
2. Stores every **unique** image in a library. Dedup is both **exact**
   (SHA-256) **and perceptual** (average-hash): near-identical scans — same
   content, different bytes, as in volume test batches — are dropped on upload,
   and a **Remove duplicates** button cleans an existing library. Uploaded
   images are normalised to bitonal Group-4 TIFF (see *Image format*).
3. Auto-reads the document type (OCR + scanner hint); type and amount are
   editable per image.
4. **Compose a batch**: say how many **transactions** you want and how many
   **checks / statements / correspondence** go in each one. Documents are
   pulled from the library by type. Then per transaction you can **×N multiply**
   (volume testing), reorder, or remove; per document you can change type/amount.
   You can also add images one at a time with **+ Batch**.
5. **Generate** real `.3xml` + `.LBXxml` (embedded base64 TIFF) + `bundle.zip`.
   The plan is **validated against the batch type** first (see *Batch-type
   rules*) — it refuses to write a faulty file. Control totals are computed and
   the batch is saved to history with download links.

---

## Project layout

```
app.py               Flask routes + session-scoped batch plan
core/config.py       paths, item/batch-type constants
core/db.py           SQLite: images library + batches/batch_items history
core/imaging.py      normalise→TIFF, SHA-256 dedup, PNG previews
core/ocr.py          heuristic OCR + MICR parse + keyword classifier (degrades gracefully)
core/oxi_reader.py   .OXZ / .oxi parser
core/batch.py        pure plan operations (add/multiply/insert/replace/move/remove)
core/xmlgen.py       .3xml and .LBXxml writers
templates/index.html one-page UI
static/style.css     navy/ice document-processing theme
data/                created at runtime: app.db, library/, outputs/
```

---

## Batch-type rules & validation

Each batch (work) type constrains which document types it may hold and **how
documents group into transactions**. The app validates the plan against the
selected type and **refuses to generate a faulty file**, showing what's wrong.

| Batch type | Allowed docs | Grouping | Confirmed |
|---|---|---|---|
| **Singles** | check + stub/statement | 1 check **+** 1 stub per transaction (`itemCount=2`) | ✓ (sanity sample) |
| **Check-Only** | check | one check per transaction | ✓ |
| **Stub-Only** | stub / statement | one per transaction | ✓ |
| **Correspondence-Only** | correspondence | one per transaction | ✓ |
| **Multiples** | check + stub/statement | each check + the stubs that follow it | best-effort |
| **Deposit Only / Check-List** | check | one per transaction | best-effort |
| **Pmt Notice / Bill / AR-Only** | stub / statement | one per transaction | best-effort |
| **CSP-Add-Stub / CSP-With-Stub** | check + stub | check + stub per transaction | best-effort |
| **Purchases / Push / Image-Only** | mixed | one per transaction | best-effort |

The **Singles** rule (check *and* stub grouped in one transaction) is confirmed
by the sanity sample, which shows a single `<transaction><itemCount>2</itemCount>`
holding a `<payment>` + a `<statement>`. Adjust `core/rules.py` if your spec
differs for the best-effort types.

## MICR auto-read

Where a check's MICR line comes from, by source:

- **OXZ scanner batch** — the scanner's *magnetic* MICR read is embedded in the
  `.oxi` (`<Micr>`/`<Ocr>`); we transcode it (accurate).
- **Imported `.3xml` / `.LBXxml`** — the file's own `rawMicr` / `Dataline`.
- **Uploaded image** — read straight off the image with a **MICR (E-13B) OCR
  model**. Standard OCR can't read the magnetic check font, so a small
  `mcr.traineddata` model lives in `data/tessdata/`. On upload we crop the MICR
  band, OCR it with `-l mcr`, and format it as `T{routing}T O{account}O …`.
  This is best-effort — clear scans read well, faint ones may come back blank
  (shown as "⚠ no MICR" on the card) and can be typed in via Edit → Scanline.

  The model is not bundled. To enable image MICR auto-read, drop a MICR
  E-13B `mcr.traineddata` into `data/tessdata/` (e.g. from
  `github.com/BigPino67/Tesseract-MICR-OCR`).

## Image format

Embedded images match the samples exactly: **single-page, bitonal (1-bit),
CCITT Group-4 TIFF, photometric min-is-white (262=0)**.

- OXZ scanner TIFFs (already bitonal G4) are embedded **byte-for-byte**.
- Uploaded PNG/JPG/colour-TIFF images are **normalised** to that bitonal G4
  format on ingest (`core/imaging.py`), because the downstream system rejects
  colour/greyscale images.

## Schema notes — confirmed vs. best-effort

Reverse-engineered by inspecting the real sample files (base64 stripped) and
verified by generating output and diffing tag structure back against them.

### `.3xml` — **confirmed**
- Root `file > fileDate, testFile(Y/N), siteName, [lockboxNumber], cashLetter`.
- `lockboxNumber` is **present in the production sample, absent in the sanity
  sample** → emitted only when a Lockbox # is entered.
- `cashLetter > cashLetterName, batch`. `batch > processDate, userBatchId,
  transactionType, [busUnitName], siteName, transaction+`.
- Per transaction: `itemCount, transactionNumber, initiatorTraceNum`, then one
  document element: `payment` / `statement` / `correspondence`.
- `payment`: `documentId, sequenceNumber, rawMicr, [accountType],
  [checkTransType DR/CR], [secCode], checkAmount, image+`.
- `statement`: `ocrLine` instead of `rawMicr`, optional `transType`.
- `correspondence`: `customField correspondenceType`, `rawMicr`, `checkAmount`.
- Control-totals block (`controlCreditCount/Amount`, `controlDebitCount/Amount`)
  repeats at **batch, cashLetter, and file** levels — **present in the sanity
  sample, absent in production** → controlled by the *"include control totals"*
  checkbox.

### `.3xml` — **best-effort / unconfirmed**
- **`<stub>`**: no stub sample was available. It is modeled like `<statement>`
  (documentId / sequenceNumber / ocrLine / images, no rawMicr) and flagged in a
  code comment in `core/xmlgen.py`. Verify against a real stub sample if you get
  one.
- Production `clientTraceNum` and transaction-level `customField`s (DCN,
  BatchID, UniqueDocID, MICRtoCheckAlt, depositRouting, …) are recognised in the
  data model (`extra_json`) but not emitted by default — the prototype emits the
  simpler `initiatorTraceNum` form from the sanity sample.

### `.LBXxml` — **confirmed**
- Flat PascalCase: `File > FileDate, Batch, DebitCount, CreditCount`.
- `Batch > BatchNumber, BusinessUnitId, BusinessUnitName, Scanner, WorkType,
  Operator, SiteId, SiteName, BatchEntryProperties, Item+`.
- `Item > DailyItemSeqNum, ItemType, Dataline, Image+`. `Dataline` is the
  literal string **`null`** when there's no scanline.

### `.LBXxml` — **best-effort / unconfirmed**
- **`ItemType` numeric code.** In the one real sample (a Correspondence-Only
  batch) codes **2, 3, and 4** all appear, and the code is **independent** of
  whether `Dataline` held a real scanline or `null`. So the mapping is a guess:
  `check=1, stub=2, statement=3, correspondence=4` (unknown→2). Confirm against
  samples of other WorkTypes.

### `.OXZ` — **confirmed**
- Zip of TIFF (+ duplicate JPEG) page images + one `.oxi` XML index.
- `Batch > Transaction > Group > Page > Image`. `PageType` observed:
  `PERSONAL_CHECK, STUB, PAGE, ENVELOPE`.
- Ingest rule: take **`Format="TIFF"`** front+back, **skip JPEG** duplicates,
  **exclude `ENVELOPE`** pages (never carried into output). Generic `PAGE`
  falls back to OCR classification.

---

## Known prototype limitations

- **No auth / single user.** The batch plan lives in the Flask session; it's not
  multi-tenant.
- **Heuristic OCR**, not a certified MICR reader — always review the fields.
- **Best-effort grouping** for batch types without a sample (see the table
  above) — the Singles rule is confirmed; the rest follow standard lockbox
  semantics and should be checked against your spec.
- `.LBXxml` `ItemType` mapping and `.3xml` `<stub>` shape are unconfirmed (above).
