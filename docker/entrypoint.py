"""Container entrypoint - dispatches the image's three run modes.

    docker run <img>                  -> web: migrate, collectstatic, gunicorn
    docker run <img> scheduler        -> migrate, then runscheduler loop
    docker run <img> python manage.py shell   -> exec verbatim
"""
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "traveldoor.settings")

# `python docker/entrypoint.py` puts /app/docker on sys.path, not /app —
# add the project root so the `traveldoor` package is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django

django.setup()

from django.core.management import call_command  # noqa: E402  (needs django.setup())


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "web"

    if mode == "web":
        call_command("migrate", interactive=False)
        call_command("collectstatic", interactive=False)
        os.execvp(
            "gunicorn",
            [
                "gunicorn",
                "traveldoor.wsgi:application",
                "--bind", "0.0.0.0:{}".format(os.environ.get("PORT", "8000")),
                "--workers", os.environ.get("GUNICORN_WORKERS", "3"),
                "--threads", os.environ.get("GUNICORN_THREADS", "2"),
                "--timeout", "60",
                "--graceful-timeout", "30",
                # trust Dokploy/Traefik X-Forwarded-* headers
                "--forwarded-allow-ips", "*",
                "--access-logfile", "-",
                "--error-logfile", "-",
            ],
        )

    if mode == "scheduler":
        call_command("migrate", interactive=False)
        os.execvp("python", ["python", "manage.py", "runscheduler"])

    # Anything else is exec'd as-is (manage.py commands, shells, ...).
    os.execvp(sys.argv[1], sys.argv[1:])
    return 0  # pragma: no cover - execvp never returns


if __name__ == "__main__":
    sys.exit(main())
