"""Production entrypoint for MidWare, driven by HOST/PORT environment variables.

    python wsgi.py

MIDWARE_DB is honoured by ``create_app``; HOST and PORT default to 0.0.0.0:5000.
The Flask development server is used deliberately: the proxy streams responses and
keeps a per-thread SQLite connection, and the app is not exposed to the public
internet directly.
"""

from __future__ import annotations

import os

from midware import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, threaded=True)
