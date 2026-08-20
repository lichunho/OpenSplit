# Slim Python 3.13 to match local dev, where the venv is built with py -3.13.
FROM python:3.13-slim

WORKDIR /app

# Installed before the app code so this layer caches across app changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app

# AWS Lambda Web Adapter: an extension that speaks the Lambda Runtime API on one
# side and plain HTTP to uvicorn on the other, so the app runs unmodified on
# Lambda behind a Function URL. Only the Lambda runtime reads /opt/extensions,
# so this is inert under `docker compose up`. The published image
# is multi-arch, so this pulls the arm64 binary on an arm64 build.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.1 /lambda-adapter /opt/extensions/lambda-adapter

# The adapter defaults to port 8080 and a "/" readiness check; point it at the
# port the CMD below actually binds and at the real health endpoint. ASYNC_INIT
# is on because main.py runs create_all() during lifespan, which on a cold start
# may also be waiting for Neon to resume from autosuspend — that can outlast the
# adapter's default readiness window.
ENV AWS_LWA_PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/healthz \
    AWS_LWA_ASYNC_INIT=true

# Shell form so ${PORT} expands: defaults to 8000, the port AWS_LWA_PORT above
# points the adapter at, and stays overridable for `docker compose up`.
#
# --proxy-headers is load-bearing, not hygiene: group.html renders the share
# link with request.url_for(), which is absolute. The Function URL terminates
# TLS and the adapter hands uvicorn plain HTTP, so without X-Forwarded-Proto the
# link — which *is* the credential for the whole group — renders as http:// and
# the Clipboard API stays disabled. Trusting all forwarders is safe because
# nothing but the local adapter can reach this socket.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
