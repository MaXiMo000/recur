# Two stages so the Node toolchain never reaches the running image.
FROM node:22-alpine AS web
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py alembic.ini schema.sql ./
COPY migrations/ ./migrations/
COPY --from=web /build/dist ./web/dist

# Not root. A container process that never needs to write to its own filesystem
# should not be able to.
RUN useradd --create-home --uid 10001 recur && chown -R recur:recur /app
USER recur

EXPOSE 8000
# One worker per instance keeps the connection pool predictable; scale by
# instance count, which is what the platform is for.
# config.check() runs FIRST, before migrations. Otherwise a missing secret in
# production surfaces as a database traceback from Alembic, which says nothing
# about the actual cause.
CMD ["sh", "-c", "python -c 'import config; config.check()' && alembic upgrade head && exec uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
