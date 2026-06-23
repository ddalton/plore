FROM python:3.12-slim AS base
WORKDIR /app

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml ./
COPY plore ./plore
COPY db ./db
COPY langgraph.json ./

RUN pip install --upgrade pip && pip install ".[dev]"

EXPOSE 2024
# LangGraph server (threads, interrupts, resume). Override for ingestion:
#   docker compose run --rm agent plore-ingest --bundle /specs/bundle.json
CMD ["langgraph", "dev", "--host", "0.0.0.0", "--port", "2024"]
