# Multi-stage: build the React UI, then serve API + UI from one image (FastAPI on :8000)
FROM node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package*.json ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS app
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# system deps: unixodbc for the Accura ODBC adapter; build tools for psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential unixodbc unixodbc-dev curl \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/ ./
# place the built UI where main.py serves it as static
COPY --from=frontend /fe/dist ./app/static
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
