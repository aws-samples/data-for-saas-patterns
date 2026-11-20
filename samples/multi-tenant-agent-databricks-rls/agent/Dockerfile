# Python base pulled from the Amazon ECR Public mirror of the Docker Official
# Image (no Docker Hub rate limits). uv is not published to ECR Public — Astral
# ships it only to GHCR / Docker Hub — so we install it into this base below
# rather than pulling an external uv image.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim-bookworm

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-cache

COPY app.py ./

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
