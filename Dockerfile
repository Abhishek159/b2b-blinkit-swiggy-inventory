FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# deps first so the 45 MB data layer doesn't invalidate the pip layer on every code change
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# BOTH trees. app.py does FRONTEND = HERE.parent / "frontend", so a build rooted at
# backend/ (the literal port of render.yaml's rootDir) ships an image where /api/health
# returns 200 with correct store counts and /b2b.html 404s -- green everywhere you'd look.
COPY backend/  /app/backend/
COPY frontend/ /app/frontend/

# Turn a bad image into a FAILED BUILD rather than a dead public site.
RUN python /app/backend/selfcheck.py

WORKDIR /app/backend
EXPOSE 8080
# Fly injects no $PORT -- bind 8080 to match fly.toml's internal_port.
# No --workers: _SW_CACHE and the minted Swiggy session are module globals, so a second
# worker would double the outbound fan-out at instamart.in and halve the cache hit rate.
# keep-alive > Fly proxy's idle expectation avoids sporadic 502s on pooled connections.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-keep-alive", "75"]
