# Pickup 360 — offline OCR + customer-ID recovery: deploy runbook

## What this release ships
- **Offline RapidOCR extraction** (replaces OpenAI vision): `services/pickup360/labelOcr.js`,
  `services/pickup360/ocrClient.js`, and the `ocr-service/` Python sidecar.
- **Misread-tolerant field matching** — `I↔1` digit/letter folding, separator-agnostic
  (`CustID:` / `CustID.` / `Cust ID` / `Cust1D13859`), plus zone & box_no cleanup.
- **Customer-ID recovery** (`helpers/pickup360/customerRecovery.js`): fills a missed id from
  another box of the same order (exact, matched on the barcode order id), back-propagates it to
  the order's other boxes, and logs a **conflict** if two boxes disagree.
- **Box completeness** (`helpers/pickup360/boxes.js`) → `box_status` in the scans response.
- **Dedup hardening** — canonical key (misread-tolerant) + rejection of unrecognised vision codes.
- **App**: capture guide (shutter gated on code-lock), missing-box warning, box status.

Measured on a 37-image Apollo batch: extraction **~89%**, **~97% with recovery**, vs the old
vision path's **~57%** (which also saved ~35% *wrong* ids as "ok").

## Deploy

1. **Backend** — deploy the branch. **No DB migration** required.
2. **OCR sidecar** (same host as the backend):
   ```bash
   cd ocr-service
   uv venv --python 3.12 .venv          # or python3.12 -m venv .venv
   uv pip install --python .venv/bin/python -r requirements.txt
   .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8001
   ```
   Run it as a service (systemd / pm2 `--interpreter none` / NSSM on Windows) so it survives reboots.
3. **Config** — `helpers/pickup360/constants.js`: `OCR_SERVICE_URL: "http://127.0.0.1:8001"`.
4. **Restart the backend.**

## Verify
- `curl http://127.0.0.1:8001/health` → `{"ok":true}`
- Scan a label → `meta` populated, status `ok`, `cust_id` present.

## Rollback
- The label-extraction path is OCR-only (no provider switch). To roll back, redeploy the
  previous backend build.

## Safety notes
- If the sidecar is **down**, scans fall to `needs_rescan` (recoverable) — they are
  **never saved wrong**.
- Genuinely unreadable photos (CustID out-of-frame/blurred) still need **manual entry** — that's
  the residual few %, and the capture guide is there to shrink it.
- New carriers (MedPlus/Movin): the architecture is carrier-aware; each needs a ~30-min keyword
  tuning pass against real labels before it reads as well as Apollo.
