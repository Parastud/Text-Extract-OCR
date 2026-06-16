# Pickup 360 — label extraction: how it works

How a captured photo becomes structured fields (`cust_id`, `area`, `box_no`, …).
Audience: engineers. Covers the path from the phone shutter to a saved scan, with
emphasis on the OCR read and the field extraction.

---

## Pipeline at a glance

```
 Phone (Pickup360 app)                     Backend (api)                    OCR sidecar (ocr-service)
─────────────────────────        ─────────────────────────────────        ──────────────────────────
 tap shutter                     POST /api/pickup360/scans
   → 3-frame silent burst          → dedup check (exact + canonical)
   → keep sharpest frame           → create row, status "processing"
   → compress (≤2048px, q0.85)     → enrichScan() async:
   → upload  ───────────────►          [A] blur gate (sharp)
                                        [B] send bytes  ───────────────►  [C] decode + normalize (PIL)
                                                                          [D] OCR engine (RapidOCR)
                                        [E] field extraction  ◄────────       → lines[] + kv{}
                                        [F] readability gate
                                        [G] customer-id recovery
                                        → status ok | needs_rescan
```

Files:
- App capture: `Pickup360/src/app/scanner.tsx`, `Pickup360/src/services/imageQuality.ts`
- Blur gate: `api/helpers/pickup360/blur.js`
- OCR client: `api/services/pickup360/ocrClient.js`
- OCR sidecar: `api/ocr-service/app.py`
- Field extraction: `api/services/pickup360/labelOcr.js`
- Orchestration: `api/helpers/pickup360/enrich.js`
- Customer recovery: `api/helpers/pickup360/customerRecovery.js`

---

## [pre] Capture (on the phone)

On the shutter, the app takes a short **burst of frames** and keeps the **sharpest**
one (scored by Laplacian variance, the same metric the backend uses). A single
frame can be unlucky — motion blur, or the lens caught mid-focus — so the best of a
few is reliably more readable. The chosen frame is **compressed** (longest edge
capped at ~2048px, JPEG quality 0.85) before upload: smaller on the wire, with no
loss of any detail the OCR would have used (the backend downscales to ~2048 anyway).

The shutter button only unlocks once a barcode is decoded live, so the worker can't
shoot from too far or off-target.

> Implementation: `captureBestFrame()` and `compressForUpload()` in
> `Pickup360/src/services/imageQuality.ts`, wired in `scanner.tsx`.

---

## [A] Blur gate — `helpers/pickup360/blur.js`

Before OCR runs, the image is scored for sharpness so we never try to read a blurry
shot.

1. Greyscale, downscale to ≤1024px (resolution-invariant).
2. Convolve with a 3×3 **Laplacian** kernel (an edge / second-derivative filter).
3. Take the **variance** of the result.

A sharp label has crisp glyph edges → strong, varied response → **high variance**.
Blur smooths edges → response near zero → **low variance**. Calibrated on real
photos, the threshold is **200** (clear shots floor ~250; blurry cluster below 200).

Below threshold → `needs_rescan`, OCR skipped. The check never throws — if it fails,
the image is accepted (the on-device burst already pre-filters blur; this is a
safety net). Toggle/threshold live in `helpers/pickup360/constants.js`
(`BLUR_CHECK_ENABLED`, `BLUR_VARIANCE_THRESHOLD`).

---

## [B][C] Decode & normalize — `ocr-service/app.py`

The backend POSTs the **raw image bytes** to the sidecar (`ocrClient.ocrExtract`).
The sidecar decodes with PIL and normalizes:

```python
img = Image.open(io.BytesIO(image_bytes))
img = ImageOps.exif_transpose(img).convert("RGB")
```

- **`exif_transpose`** rotates the pixels upright. Phones record orientation in EXIF
  metadata instead of rotating the actual pixels — without this, portrait photos
  arrive sideways and every text line is rotated, which wrecks recognition.
- **`convert("RGB")`** gives the engine the 3-channel input it expects.

---

## [D] The OCR engine — RapidOCR (PP-OCR on ONNX)

We use **RapidOCR**: PaddleOCR's PP-OCR models exported to ONNX Runtime, running
**fully offline on our own server** — no external API, no per-scan cost. The model
is loaded **once at startup** and kept warm (~0.6s/label on CPU thereafter).

We send the **whole image** — we do **not** slice it ourselves. The engine works in
two passes:

1. **Detection** — scans the whole image and finds *where* the text is, putting a
   box around each text region it discovers (one around `CustID: 13369`, another
   around `AREA: Z004`, …). These boxes are found dynamically, wherever the text
   sits, at any angle.
2. **Recognition** — crops each detected box and reads it into characters, with a
   **confidence score** per line.

It then **sorts** the lines top-to-bottom, left-to-right (bucketing the y-coordinate
so a `KEY:` and its `value` stay on one logical line and reading order is stable).

The sidecar returns: `text` (all lines joined), `lines` (`[{text, score}]`), and a
generic `kv` map (any `"A:B"` line → `{A: B}`). It is deliberately
**carrier-agnostic** — it never knows what a "customer id" is. All carrier-specific
meaning is decided in Node.

> Why detection-based, not template/coordinate crops: a fixed-coordinate approach
> (cut at known positions → box #1 = customer id) only works if every photo is
> perfectly aligned, which never happens handheld. Because the engine *finds* the
> text, the worker can frame loosely, off-center, at an angle, and it still reads
> every field — with no per-layout template needed for the read.

---

## [E] Field extraction — `services/pickup360/labelOcr.js`

The labels are **self-describing** (`CustID:13369`, `AREA:Z004`, `BOX NO:1/2`), so
extraction is **keyword-anchored**: find the printed field name, take what follows.
The work is in tolerating the OCR's small mistakes.

### Synonyms
`FIELD_SYNONYMS` maps each configured `fieldKey` to the printed keywords that mark it
(`cust_id → [CUSTID, CUSTOMERID, CUSTOMER]`, `area → [AREA, ZONE]`, …).

### Lookalike folding (`deOcrKey`) — keys only
A printed **field name** never legitimately contains a digit, so a digit inside a key
is provably a misread. We fold them back: `1→I 0→O 5→S 8→B 6→G 2→Z`, so a read of
`Cust1D` or `CU5TID` still matches `CUSTID`. Applied to the **key only** — values keep
their digits (`13369` is untouched).

### Separator-agnostic matching (`valueInLine`)
The engine renders the separator inconsistently. We walk the line character by
character; the moment the accumulated (folded) prefix equals a synonym, the **rest of
the original line is the value**. So all of these read correctly:

```
CustID:13645    CustID.13645    Cust ID 13645    Cust1D13859   →  13645 / 13859
```

The value is taken from the **original** line (not the folded one), so spaces inside
store names / addresses survive.

### Per-field cleanup
- **`cleanZone`**: `"2.004"` / `"Z.004"` / `"2:004"` → **`Z004`** (the engine mangles
  the leading `Z` and the separator).
- **`cleanBox`**: `"NO: 2/2"` → **`2/2`** (strips the `BOX NO` residue).

### Barcode backfill — trust the QR over the photo
Anything the **scanned barcode already encodes** is taken from the barcode, not the
photo, because the barcode is exact and an OCR'd field is a guess:
- `awb` ← the scanned code,
- `carrier_name` ← parsed carrier,
- `area` ← `cleanZone(ocr) || parsed.zone` (OCR first, barcode as fallback).

`findCode` locates the barcode by scanning the OCR lines for the first string that
`parseCode()` recognizes as a known carrier pattern.

---

## [F] Readability gate

A read only counts if there's a solid signal — otherwise we refuse to save it:

```
readable = (a barcode was recognized)
        OR (a customer id was captured)
        OR (≥ 3 fields were filled)
```

If none hold → `needs_rescan`, and **no `meta` is written**. This is what stops a
far/blurry shot from inventing fake data and saving it as "done."

---

## [G] Customer-id recovery — `helpers/pickup360/customerRecovery.js`

If the customer id alone was missed (smudged / out of frame on that field), we
recover it without a rescan from a **sibling box of the same order**: every box of an
order ships to the same store → same customer id, so a sibling box (matched on the
exact order id from the **barcode**) that *did* capture it gives the value. The order
id comes from the barcode, not the OCR, so this **copies a known-good value — it does
not guess.**

Conversely, when a scan **does** capture the id, it's shared with sibling boxes that
missed it. If two boxes of one order disagree, a **conflict is logged loudly** rather
than letting a wrong id sit silently as "ok."

Only if recovery fails does the worker type the id in once (the manual-entry option in
the rescan modal).

---

## Status outcomes

| Status         | Meaning                                                        |
|----------------|----------------------------------------------------------------|
| `ok`           | Read (or recovered) all mandatory fields. Counts as done.      |
| `needs_rescan` | Blurry, unreadable, or missing a mandatory field. Flagged.     |
| `duplicate`    | Same parcel already scanned (exact or misread-tolerant key).   |

Design principle: the pipeline **fails loud (rescan), never quiet (a wrong value
saved as ok)**.

---

## Tuning / where to change things

| Want to change…                  | Edit                                                         |
|----------------------------------|-------------------------------------------------------------|
| Blur strictness                  | `helpers/pickup360/constants.js` → `BLUR_VARIANCE_THRESHOLD` |
| Sidecar URL                      | `constants.js` → `OCR_SERVICE_URL`                          |
| Field keywords (per carrier)     | `services/pickup360/labelOcr.js` → `FIELD_SYNONYMS`         |
| Zone / box formatting            | `labelOcr.js` → `cleanZone` / `cleanBox`                    |
| Readability threshold            | `labelOcr.js` → `readable = …`                              |
| Burst size / compression         | `Pickup360/src/app/scanner.tsx` (`BURST_FRAMES`), `imageQuality.ts` |

New carriers (MedPlus / Movin): the architecture is carrier-aware; each needs a short
keyword-tuning pass (`FIELD_SYNONYMS` + any per-field cleaners) against real labels
before it reads as well as Apollo.
