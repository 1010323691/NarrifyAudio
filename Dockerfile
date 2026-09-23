FROM node:20-alpine AS frontend
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM python:3.12-slim AS api
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend ./backend
COPY alembic.ini ./
COPY src ./src
COPY --from=frontend /app/dist ./dist
EXPOSE 8642

# Lightweight image used by the PostgreSQL/Redis smoke environment.  It keeps
# the durable platform and LLM transport available without downloading the
# optional multi-gigabyte local TTS runtime.
FROM python:3.12-slim AS smoke-api
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY backend/requirements-smoke.txt ./backend/requirements-smoke.txt
RUN pip install --no-cache-dir -r backend/requirements-smoke.txt
COPY backend ./backend
COPY alembic.ini ./
COPY src ./src
COPY scripts ./scripts
COPY --from=frontend /app/dist ./dist
EXPOSE 8642
