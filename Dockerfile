# ---- Stage 1: build the React UI ----
FROM node:24-alpine AS ui-build
WORKDIR /app/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

# ---- Stage 2: Python backend ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
ENV OPENMATRIX_HOST=0.0.0.0
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock
COPY . .
# UI build output from stage 1 is served by FastAPI under /ui
COPY --from=ui-build /app/ui/dist /app/ui/dist
EXPOSE 8000
CMD ["python", "main.py"]
