# SZL Router — sovereign, OpenAI-compatible LLM gateway with a signed receipt
# on every answer. Minimal image: install the package + the optional signing
# extra (szl-receipt, git-only) so receipts are real DSSE/ECDSA-P256 signatures.
#
#   docker build -t szl-router .
#   docker run -p 8000:8000 -e GROQ_API_KEY=... szl-router
#
# No secrets are baked in — all upstream + signing keys come from the env at run
# time. With no SZL_RECEIPT_KEY_PEM set, the server generates an EPHEMERAL session
# signing key on boot and logs its public key (honest: not a persistent identity).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# git is needed only to pull the git-only szl-receipt signing library.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

RUN pip install --upgrade pip \
    && pip install . \
    && pip install "git+https://github.com/szl-holdings/szl-receipt.git@v0.1.0"

EXPOSE 8000

CMD ["python", "-m", "szl_router.app"]
