# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime OS dependencies:
#   - curl: the container health check.
#   - WeasyPrint's native stack (Requirement 19.2/19.3): libgobject/GLib, Pango, cairo,
#     GDK-PixBuf and libffi. WeasyPrint is pure-Python but loads these shared libraries at
#     render time; without them PDF export raises PdfRenderUnavailable rather than rendering.
#   - shared-mime-info: WeasyPrint uses it to resolve embedded image mime types.
#   - libraqm0: gives Pillow native complex-text/RTL layout (direction="rtl"), so the QR sheet's
#     Hebrew site-name label is shaped and bidi-ordered deterministically across environments.
#   - fonts: DejaVu covers Latin; Noto (fonts-noto-core) covers Hebrew, so the RTL Hebrew
#     reports render real glyphs instead of tofu. (The QR sheet ships its own bundled font
#     and does not depend on these.)
# Installed in the shared base stage so both the dev and prod images have them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libcairo2 \
        libffi8 \
        shared-mime-info \
        libraqm0 \
        fonts-dejavu-core \
        fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic

# ---------------------------------------------------------------- development
FROM base AS dev
RUN pip install -e ".[dev]"
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------------------------------------------------------------- production
FROM base AS prod
RUN pip install .
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
