FROM python:3.11.9-slim-bookworm

WORKDIR /app

# psycopg2-binary ships a bundled libpq and numpy has prebuilt manylinux wheels,
# so we don't need apt, gcc, or libpq-dev at build time. Forcing --only-binary
# avoids surprise source builds if a new release lacks a wheel.
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

COPY . .

EXPOSE 8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
