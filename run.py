"""Development entrypoint for MidWare.

    python run.py

HOST and PORT environment variables override the bind address (defaults
0.0.0.0:5000).
"""

from __future__ import annotations

import os

from midware import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, threaded=True)
