# AwaazDesk production image (used by Render; runs anywhere Docker does).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
COPY static ./static
COPY sample_business.json ./

RUN useradd --create-home --uid 10001 awaaz && mkdir -p data && chown awaaz data
USER awaaz

EXPOSE 8000
# --proxy-headers: behind Render's TLS proxy, trust X-Forwarded-Proto/For so the
# same-origin check sees https and rate limits see the real visitor IP.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --ws-max-size 1048576"]
