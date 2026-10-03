# TraceX offline appliance image: API + worker + web UI + offline Geo-IP.
#
#   docker build -t tracex-appliance:latest .
#
# Network is needed only while *building* (Python wheels, npm packages and the
# open Geo-IP databases). The resulting image runs fully offline.
#
# Behind a TLS-inspecting proxy, hand its CA bundle to the build as a secret
# (used by pip/uv/npm/urllib during the build only, never copied into an image):
#   docker build --secret id=build_ca,src=/path/to/ca-bundle.crt -t tracex-appliance .

# ---- 1. Web UI -----------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=secret,id=build_ca,required=false if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca PIP_CERT=/run/secrets/build_ca REQUESTS_CA_BUNDLE=/run/secrets/build_ca NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; npm ci --no-audit --no-fund
COPY frontend/ ./
# Same-origin API: the image serves the UI and /v1 from one port.
ENV VITE_API_BASE_URL=""
RUN npm run build

# ---- 2. Python environment -------------------------------------------------------
FROM python:3.11-slim AS python
ARG TRACEX_CANDIDATE_BACKENDS=0
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN --mount=type=secret,id=build_ca,required=false if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca PIP_CERT=/run/secrets/build_ca REQUESTS_CA_BUNDLE=/run/secrets/build_ca NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; pip install --no-cache-dir "uv==0.8.17"
WORKDIR /opt/tracex
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=secret,id=build_ca,required=false if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca PIP_CERT=/run/secrets/build_ca REQUESTS_CA_BUNDLE=/run/secrets/build_ca NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; if [ "$TRACEX_CANDIDATE_BACKENDS" = 1 ]; then uv sync --frozen --no-dev --extra ml --extra research --no-install-project; else uv sync --frozen --no-dev --extra ml --no-install-project; fi
COPY app ./app
COPY workers ./workers
RUN --mount=type=secret,id=build_ca,required=false if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca PIP_CERT=/run/secrets/build_ca REQUESTS_CA_BUNDLE=/run/secrets/build_ca NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; if [ "$TRACEX_CANDIDATE_BACKENDS" = 1 ]; then uv sync --frozen --no-dev --extra ml --extra research; else uv sync --frozen --no-dev --extra ml; fi

# ---- 3. Offline Geo-IP database (DB-IP country lite CC BY 4.0 + IPtoASN PDDL) ------
FROM python AS geoip
ARG TRACEX_GEOIP=1
COPY deploy/appliance/geoip-cache /opt/geoip-cache
RUN --mount=type=secret,id=build_ca,required=false if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca PIP_CERT=/run/secrets/build_ca REQUESTS_CA_BUNDLE=/run/secrets/build_ca NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; \
    if [ "$TRACEX_GEOIP" = "1" ]; then \
      if [ -f /opt/geoip-cache/compiled/manifest.json ]; then \
        (cd /opt/geoip-cache/compiled && sha256sum -c SHA256SUMS) && mkdir -p /opt/geoip && cp -a /opt/geoip-cache/compiled /opt/geoip/; \
      else /opt/tracex/.venv/bin/python -m app.engine.geoip --dir /opt/geoip download && rm -rf /opt/geoip/downloads; fi; \
    else mkdir -p /opt/geoip; fi
RUN if [ -d /opt/geoip/compiled ] && [ ! -f /opt/geoip/compiled/SHA256SUMS ]; then cd /opt/geoip/compiled && sha256sum * > SHA256SUMS; fi

# ---- 4. Runtime ----------------------------------------------------------------------
FROM python:3.11-slim AS runtime
ARG TRACEX_CANDIDATE_BACKENDS=0
RUN if [ "$TRACEX_CANDIDATE_BACKENDS" = 1 ]; then apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*; fi
RUN useradd --create-home --uid 10001 tracex && mkdir -p /data/evidence && chown -R tracex /data
WORKDIR /opt/tracex
COPY --from=python /opt/tracex /opt/tracex
COPY --from=web /web/dist /opt/tracex/web
COPY --from=geoip /opt/geoip /opt/tracex/geoip
COPY scripts ./scripts
COPY schemas ./schemas
COPY data_manifest.json feature_schema.json ./
ENV PATH="/opt/tracex/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TRACEX_EVIDENCE_ROOT=/data/evidence \
    TRACEX_GEOIP_DIR=/opt/tracex/geoip \
    TRACEX_WEB_DIR=/opt/tracex/web
USER tracex
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=10 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/v1/healthz', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
