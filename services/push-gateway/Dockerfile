FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY push_gateway ./push_gateway
RUN pip install --no-cache-dir .

# Compose runs it as 10001 (secrets are group 10001, mode 640)
USER 10001:10001
EXPOSE 8200
CMD ["python", "-m", "push_gateway.app"]
