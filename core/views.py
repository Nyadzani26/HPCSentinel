from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render


def health(request):
    """Used by deployments to confirm the web container can reach its database."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception:  # noqa: BLE001 - report any database failure as unhealthy
        return JsonResponse({"status": "database unavailable"}, status=503)


@login_required
def home(request):
    return render(request, "home.html")
