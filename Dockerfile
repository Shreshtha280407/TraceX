FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# System packages media_processing needs at runtime -- ffmpeg/ffprobe (video
# probe + frame extraction, app/modules/media_processing/video/) and
# tesseract-ocr (real local OCR, analysis/tesseract_ocr.py; tesseract-ocr-eng
# is pulled in automatically as its Recommends). Neither is a Python
# dependency uv could install -- both are real local binaries, no cloud/
# network calls. This layer only invalidates on a Dockerfile change, not on
# every source edit, same caching rationale as the uv sync layer below.
# No detector model weights are installed here -- see
# docs/architecture/media-processing-worker.md's "Model asset bootstrap":
# that is a separate, explicit, operator-invoked step
# (`bootstrap_models.py`), never baked into this image or run automatically.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first for better layer caching: this layer only invalidates
# when pyproject.toml / uv.lock change, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY app ./app
# `README.md` is required only because `pyproject.toml` declares it as the
# package readme (`uv run` validates project metadata on first invocation
# inside the container); `alembic.ini`/`migrations` let the required release-
# gate check `docker compose exec api uv run alembic upgrade head` run
# in-container instead of needing a host-side `uv` install.
COPY README.md alembic.ini ./
COPY migrations ./migrations

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
