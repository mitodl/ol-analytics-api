FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv venv --relocatable /app/.venv && \
    uv sync --frozen --no-dev

COPY src/ ./src/

FROM python:3.12-slim-bookworm

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000
CMD ["uvicorn", "ol_analytics_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
