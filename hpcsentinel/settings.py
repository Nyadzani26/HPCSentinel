"""Django settings for HPCSentinel.

Every value that differs between installations comes from an environment variable
(NFR-02, NFR-09); nothing secret is stored in the repository.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


DEBUG = env_bool("DEBUG", False)

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "insecure-development-key"
    else:
        raise RuntimeError("SECRET_KEY must be set (see .env.example).")

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "hpcsentinel.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context.sentinel",
            ],
        },
    },
]

WSGI_APPLICATION = "hpcsentinel.wsgi.application"

# PostgreSQL when POSTGRES_HOST is set (the Compose deployment); SQLite otherwise,
# which is used only by the automated tests and quick local checks.
if os.environ.get("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ["POSTGRES_USER"],
            "PASSWORD": os.environ["POSTGRES_PASSWORD"],
            "HOST": os.environ["POSTGRES_HOST"],
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

AUTH_USER_MODEL = "core.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

# Sessions: a sliding expiry after a period of inactivity (NFR-04); the cookie is
# not readable by scripts and is not sent with cross-site requests.
SESSION_IDLE_MINUTES = int(os.environ.get("SESSION_IDLE_MINUTES", "30"))
SESSION_COOKIE_AGE = SESSION_IDLE_MINUTES * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

LANGUAGE_CODE = "en-gb"
# The time zone people see: every time on screen, the report days and the date fields on the Settings page. What the
# platform stores and computes is always UTC. Change it with TIME_ZONE in .env (any name from the IANA list).
TIME_ZONE = os.environ.get("TIME_ZONE", "").strip() or "Africa/Johannesburg"
try:
    from zoneinfo import ZoneInfo
    ZoneInfo(TIME_ZONE)
except Exception:                                    # a typo or missing zone data must not stop the monitoring
    import sys
    print(f"WARNING: unknown time zone '{TIME_ZONE}' (TIME_ZONE); showing UTC instead.", file=sys.stderr)
    TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Optional reverse-proxy deployment (e.g. Apache at /sentinel/) ---
# FORCE_SCRIPT_NAME is the public path prefix ("" when served at the root).
FORCE_SCRIPT_NAME = os.environ.get("FORCE_SCRIPT_NAME", "").rstrip("/") or None
BEHIND_PROXY = env_bool("BEHIND_PROXY", False)
_prefix = FORCE_SCRIPT_NAME or ""
if BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
# Distinct cookie names/paths so this app never collides with RangerAccess on the same host.
SESSION_COOKIE_NAME = "hpcsentinel_sessionid"
CSRF_COOKIE_NAME = "hpcsentinel_csrftoken"
SESSION_COOKIE_PATH = _prefix + "/"
CSRF_COOKIE_PATH = _prefix + "/"

STATIC_URL = (_prefix + "/static/") if _prefix else "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# HPCSentinel settings
HPCSENTINEL_CONFIG = os.environ.get("HPCSENTINEL_CONFIG", "config/hpcsentinel.yml")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "")
MODEL_STORE = os.environ.get("MODEL_STORE", str(BASE_DIR / "model_store"))

# --- Email alerts (FR-31) ---
# Optional. With EMAIL_HOST empty the platform sends nothing. The password belongs in .env, never in the repository.
def _env_port(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:                                 # a typo must not stop the monitoring
        return default


EMAIL_HOST = os.environ.get("EMAIL_HOST", "").strip()
EMAIL_PORT = _env_port("EMAIL_PORT", 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)                 # port 465
EMAIL_USE_TLS = False if EMAIL_USE_SSL else env_bool("EMAIL_USE_TLS", True)      # port 587 (STARTTLS)
EMAIL_TIMEOUT = 20                                               # a mail server that hangs must not hang the worker
DEFAULT_FROM_EMAIL = os.environ.get("EMAIL_FROM", "").strip() or EMAIL_HOST_USER or "hpcsentinel@localhost"
# The public address of this site, used for the links in emails (for example https://smshost.tail06bf7e.ts.net). Empty: no links.
SITE_URL = os.environ.get("SITE_URL", "").strip().rstrip("/")
