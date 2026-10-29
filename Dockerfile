# SZL Router — sovereign, OpenAI-compatible LLM gateway with a signed receipt
# on every answer. Minimal image: install the package + the optional signing
# extra so receipts are real DSSE/ECDSA-P256 signatures. Keep the existing
# v0.1.0 receipt release pinned to its immutable source commit.
#
#   docker build -t szl-router .
#   docker run -p 8000:8000 -e GROQ_API_KEY=... szl-router
#
# No secrets are baked in — all upstream + signing keys come from the env at run
# time. With no SZL_RECEIPT_KEY_PEM set, the server generates an EPHEMERAL session
# signing key on boot and logs its public key (honest: not a persistent identity).
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# git is needed to install the source-pinned szl-receipt signing library.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY szl_router ./szl_router
COPY router_control ./router_control

RUN python -m pip install . \
    && python -m pip install "git+https://github.com/szl-holdings/szl-receipt.git@c3230711846e6d845b83b5e97a3c3e58fb1de107" \
    && python -m pip check

RUN groupadd --gid 10001 szl \
    && useradd --uid 10001 --gid szl --no-create-home --home-dir /nonexistent szl

USER 10001:10001

EXPOSE 8000

CMD ["python", "-m", "szl_router.app"]
