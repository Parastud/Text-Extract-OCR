"""
Pickup 360 OCR sidecar.

A tiny FastAPI service that wraps RapidOCR (PP-OCR models on ONNX Runtime) so the
Node backend can read shipping labels fully offline — no external API, no per-scan
cost. The model is loaded once at startup and kept warm.

It is deliberately carrier-agnostic: it returns the raw OCR lines plus a generic
KEY:VALUE map parsed from lines like "CustID:13369" / "BOX NO: 1/2". All
carrier-specific mapping (which key is the customer id, code detection, etc.) lives
in the Node layer, next to the org's field config.

Run:  uvicorn app:app --host 127.0.0.1 --port 8001
"""
import io
import re

import numpy as np
from fastapi import FastAPI, Request
from PIL import Image, ImageOps
from rapidocr_onnxruntime import RapidOCR

app = FastAPI(title="pickup360-ocr")

# Loaded once; ~0.6s/label on CPU thereafter. Models ship inside the package, so
# this needs no network at startup or inference.
engine = RapidOCR()


def _normalize_key(k: str) -> str:
    """'Box No' / 'CustID' / 'RPR DATE' -> 'BOXNO' / 'CUSTID' / 'RPRDATE'."""
    return re.sub(r"[^A-Z0-9]", "", (k or "").upper())


def _read(image_bytes: bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
    result, _elapsed = engine(np.array(img))
    if not result:
        return []
    # Top-to-bottom, left-to-right so a "KEY: value" pair stays on one line and
    # the reading order is stable.
    result.sort(key=lambda r: (round(r[0][0][1] / 20), r[0][0][0]))
    return [{"text": t, "score": float(sc)} for (_box, t, sc) in result]


def parse_kv(lines):
    """Generic KEY:VALUE map from OCR lines ('CustID:13369' -> {'CUSTID': '13369'})."""
    kv = {}
    for ln in lines:
        t = ln["text"]
        if ":" in t:
            left, right = t.split(":", 1)
            key, val = _normalize_key(left), right.strip()
            if key and val and key not in kv:  # first occurrence wins
                kv[key] = val
    return kv


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/extract")
async def extract(request: Request):
    """
    Body = raw image bytes (Content-Type: application/octet-stream).
    Returns: { text, lines: [{text,score}], kv: {NORMALIZED_KEY: value} }.
    """
    body = await request.body()
    if not body:
        return {"text": "", "lines": [], "kv": {}}

    try:
        lines = _read(body)
    except Exception as e:  # unreadable / not an image — let Node flag a rescan
        return {"text": "", "lines": [], "kv": {}, "error": str(e)}

    return {"text": " ".join(l["text"] for l in lines), "lines": lines, "kv": parse_kv(lines)}
