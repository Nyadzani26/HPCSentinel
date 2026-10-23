# Pinned to Debian bookworm (OpenSSL 3.0). The plain python:3.12-slim tag moved to a Debian with OpenSSL 3.5, and from
# the Ranger head node its secure handshake to smtp.gmail.com ended with SSLEOFError (the host's own OpenSSL worked), which
# stopped email alerts. Move off bookworm only after sending a test email from Settings on the new image.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Collect static files at build time; the key here is a throw-away build value.
RUN SECRET_KEY=build-only python manage.py collectstatic --noinput

# Run as an unprivileged user and give it the model store directory.
RUN useradd --create-home app && mkdir -p /app/model_store && chown -R app /app/model_store
USER app

EXPOSE 8000
