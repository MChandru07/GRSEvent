"""Convenience launcher for the Flask app (workspace root).

The real application lives in the ``event-management-website/`` sub-folder, but
VS Code opens its terminal at this workspace root, so typing ``python app.py``
here used to fail with::

    FileNotFoundError: [Errno 2] No such file or directory:
    '...\\event-management-website\\event-management\\app.py'

(because this launcher searched for an ``event-management/`` folder while the
project folder is actually called ``event-management-website/``).

This file makes the app startable from *either* folder::

    python app.py                             # from the workspace root (this file)
    python event-management-website/app.py    # from the project folder (real entry point)
    flask run                                 # from the workspace root (Flask CLI finds this file)

Every command of the real entry point works here too, because the arguments are
forwarded to it::

    python app.py init-db
    python app.py create-admin --username raj
    python app.py check-db
    python app.py --port 5055

Importing the real entry point (instead of duplicating it) means ``.env``,
templates, static files, ``uploads/`` and the MySQL settings all keep resolving
to the ``event-management-website/`` folder.
"""

import importlib.util
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# The project folder keeps its own name; the older "event-management" spelling is
# still accepted so this launcher does not break if the folder is renamed back.
PROJECT_DIR = None
for _folder in ("event-management-website", "event-management"):
    _candidate = os.path.join(HERE, _folder)
    if os.path.isfile(os.path.join(_candidate, "app.py")):
        PROJECT_DIR = _candidate
        break

if PROJECT_DIR is None:                       # nothing to import – say so clearly
    raise FileNotFoundError(
        "The Flask entry point was not found. Expected "
        f"{os.path.join(HERE, 'event-management-website', 'app.py')!r}.\n"
        "Start the server from the project folder instead:\n"
        "    python event-management-website/app.py"
    )

ENTRY_POINT = os.path.join(PROJECT_DIR, "app.py")

# Make the project's own modules (config, db, auth, storage) importable when the
# real entry point is loaded from this directory.
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# Load the real app under a unique module name so this launcher can never end
# up importing itself (the Flask CLI imports this file as the module "app").
_spec = importlib.util.spec_from_file_location("event_management_app", ENTRY_POINT)

if _spec is None or _spec.loader is None:
    raise ImportError(f"Could not load the application entry point: {ENTRY_POINT!r}")
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

# Exposed as `app` so both `python app.py` and `flask run` work from here.
app = _module.app


def _warn_if_port_busy(port: int) -> None:
    """Windows lets a second server bind a port that is already in use, so a
    leftover background server (e.g. a `pythonw.exe app.py` started earlier)
    keeps answering requests with stale code instead of raising an error.
    Say so loudly instead of letting that go unnoticed."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", port)) != 0:
            return

    print(
        f"\n!! Port {port} is already in use by another running server.\n"
        f"   It may answer requests with outdated code/files.\n"
        f"   Stop it with the VS Code task \"Flask: stop servers on port 5000\",\n"
        f"   or use a different port:  $env:PORT=8080  then run this again.\n"
    )


if __name__ == "__main__":
    argv = sys.argv[1:]
    main = getattr(_module, "main", None)

    # "init-db", "create-admin", "check-db", "--port", "--help", … belong to the
    # real entry point: hand the command line over so both folders behave alike.
    if argv and callable(main):
        raise SystemExit(main(argv))

    port = int(os.environ.get("PORT", 5000))
    _warn_if_port_busy(port)
    app.run(
        host="0.0.0.0",
        port=port,
        debug=app.config.get("FLASK_DEBUG", False),
    )
