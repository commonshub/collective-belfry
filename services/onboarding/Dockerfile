FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 1000 belfry && useradd --uid 1000 --gid belfry --create-home belfry

WORKDIR /app
COPY pyproject.toml README.md ./
COPY belfry_onboarding ./belfry_onboarding
RUN pip install --no-cache-dir .

USER belfry
EXPOSE 8100

CMD ["uvicorn", "belfry_onboarding.app:app", "--host", "0.0.0.0", "--port", "8100"]
