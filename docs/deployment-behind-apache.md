# Deploying HPCSentinel behind Apache at /sentinel/

HPCSentinel runs as its own containers and listens on loopback only (127.0.0.1:8000).
That is the default; `WEB_BIND` in `.env` changes it when the page must be opened directly from another computer.
Apache publishes it under a sub-path next to other applications such as RangerAccess.
HPCSentinel stays standalone: it shares no database, session or code with them.

## 1. Environment (.env)

    FORCE_SCRIPT_NAME=/sentinel
    BEHIND_PROXY=true
    ALLOWED_HOSTS=smshost.tail06bf7e.ts.net,localhost,127.0.0.1
    CSRF_TRUSTED_ORIGINS=https://smshost.tail06bf7e.ts.net

## 2. Apache

    ProxyPreserveHost On
    RequestHeader set X-Forwarded-Proto "https"
    ProxyPass        /sentinel/ http://127.0.0.1:8000/
    ProxyPassReverse /sentinel/ http://127.0.0.1:8000/

On SELinux hosts allow Apache to connect to the local port:

    sudo setsebool -P httpd_can_network_connect 1

Reload with `sudo systemctl reload httpd`.

## 3. Navigation

Add one link to the existing admin console: `<a href="/sentinel/">Sentinel</a>`.
No other change to the other application is required.

## 4. Fault isolation

The web container, worker container and database are separate from the other
application. A failure of the detection worker or of Prometheus cannot affect
the registration portal. Sign-in is separate (shared sign-in is a deferred item).
