# Public status surface for SZLHOLDINGS/llm-router-live (stdlib server.py + static files).
#
# Base: python:3.11-slim, pinned to the multi-arch OCI image-index digest that
# registry-1.docker.io returned for that tag on 2026-09-25 (Docker-Content-Digest).
# Bump the tag and the digest together.
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9

# Hugging Face runs Docker Spaces as UID 1000. Create that user explicitly so the
# image behaves the same locally and HOME is a real directory owned by the runtime user.
RUN useradd --create-home --uid 1000 user
ENV HOME=/home/user

WORKDIR /app
# .dockerignore keeps tests/, bytecode caches and build metadata out of the image,
# and therefore out of the directory that server.py serves as static files.
COPY --chown=1000:1000 . /app

USER 1000:1000
EXPOSE 7860

# Process liveness only: /healthz answers 200 whenever the server is running.
# /readyz remains the source-binding readiness contract used by the deploy verifier.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=4)"]

CMD ["python","server.py"]
