"""`python -m app [--no-browser]` — start AutoClip Studio and open it in the browser."""
import asyncio
import logging
import sys
import threading
import webbrowser

import uvicorn

from .config import HOST, PORT


class _QuietCancel(logging.Filter):
    """Requests cut off by Ctrl+C end with CancelledError; that's expected, not an error."""

    def filter(self, record):
        cancelled = record.exc_info and issubclass(record.exc_info[0], asyncio.CancelledError)
        return not cancelled and "CancelledError" not in record.getMessage()


class Server(uvicorn.Server):
    def handle_exit(self, sig, frame):
        # Browsers keep <video> streams open, so a graceful shutdown would wait
        # forever. Stop renders and sign-in now and exit without waiting.
        from . import claude, render

        render.kill_running()
        claude.login_cancel()
        logging.getLogger("uvicorn.error").addFilter(_QuietCancel())
        self.force_exit = True
        super().handle_exit(sig, frame)


if __name__ == "__main__":
    url = f"http://{'localhost' if HOST in ('127.0.0.1', '0.0.0.0') else HOST}:{PORT}"
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"AutoClip Studio → {url}   (press Ctrl+C to stop)")
    Server(uvicorn.Config("app.main:app", host=HOST, port=PORT)).run()
