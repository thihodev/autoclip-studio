"""`python -m app [--no-browser]` — start AutoClip Studio and open it in the browser."""
import sys
import threading
import webbrowser

import uvicorn

from .config import HOST, PORT

if __name__ == "__main__":
    url = f"http://{'localhost' if HOST in ('127.0.0.1', '0.0.0.0') else HOST}:{PORT}"
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"AutoClip Studio → {url}")
    uvicorn.run("app.main:app", host=HOST, port=PORT)
