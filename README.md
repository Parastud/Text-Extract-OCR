# Pickup 360 OCR sidecar

Offline label extraction for Pickup 360, using **RapidOCR** (PP-OCR models on ONNX
Runtime). Replaces the OpenAI vision calls — no external API, no per-scan cost, and
on the Apollo labels it reads the fields (Customer ID, zone, store, box) **better**
than the vision model did. Runs on CPU (~0.6s/label); the OCR models ship inside the
pip package, so inference needs no network.

The Node backend calls it on localhost; it returns the raw OCR lines plus a generic
`KEY:VALUE` map. All carrier-specific mapping lives in the Node layer
(`helpers/pickup360/parseCode.js`, `services/pickup360/labelOcr.js`).

## Setup (one time)

Needs **Python 3.11 or 3.12** (NOT 3.13/3.14 — the ML wheels don't support them yet).
The repo was set up with [`uv`](https://docs.astral.sh/uv/), which fetches a pinned
Python automatically:

```bash
cd ocr-service
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt   # Windows
# (Linux/macOS: --python .venv/bin/python)
```

Plain pip alternative (with a 3.12 already installed):

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
# Windows
.venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8001
# Linux/macOS
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8001
```

Health check: `curl http://127.0.0.1:8001/health` → `{"ok":true}`.

Keep it running as a service alongside Node (systemd / pm2 `--interpreter none` /
NSSM on Windows). The backend uses it for every label extraction, so it must be up
whenever the backend is up.

## Backend config

In `helpers/pickup360/constants.js`:

- `OCR_SERVICE_URL: "http://127.0.0.1:8001"` — where this sidecar listens.

## Endpoint

`POST /extract` — body = raw image bytes (`Content-Type: application/octet-stream`).
Returns `{ text, lines: [{text, score}], kv: { NORMALIZED_KEY: value } }`.
