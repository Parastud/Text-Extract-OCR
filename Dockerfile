# Pickup 360 OCR sidecar — FastAPI + RapidOCR (PP-OCR models on ONNX Runtime).
# Pinned to Python 3.12: rapidocr-onnxruntime does NOT support 3.13+.
FROM python:3.12-slim

# rapidocr-onnxruntime pulls in OpenCV, which needs these shared libraries at
# runtime (libGL + glib). The slim image doesn't ship them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so this layer is cached when only app code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (the OCR models ship inside the pip package — no download needed).
COPY . .

# Render and most PaaS inject $PORT; default to 8001 for local runs.
ENV PORT=8001
EXPOSE 8001

# Single worker on purpose: RapidOCR loads the model into RAM once at startup;
# extra workers would multiply memory with no throughput gain on a small instance.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8001}"]
