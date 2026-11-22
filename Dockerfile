ARG BASE_REGISTRY=docker.io/library

FROM ${BASE_REGISTRY}/node:24-alpine AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ${BASE_REGISTRY}/python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app/backend
COPY backend/ ./
RUN pip install --no-cache-dir .
COPY --from=ui /ui/dist /app/frontend/dist
ENV PORTAL_STATIC=/app/frontend/dist
EXPOSE 8080
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn portal.main:app --host 0.0.0.0 --port 8080"]
