FROM python:3.12-slim

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
