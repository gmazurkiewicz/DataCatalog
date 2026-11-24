"""Command line entry point: `datacatalog` (or `python -m datacatalog`, `python run.py`).

Options:
  --host 0.0.0.0        allow access from other computers on the network
                        (e.g. when running on a Raspberry Pi); requires --password
  --port 8765           server port
  --password SECRET     password (user name: admin)
  --open PATH           open a drive right away, e.g. E:\\ or /mnt/drive
  --no-browser          do not open a browser
"""

import argparse
import sys
import threading
import webbrowser

from . import __version__


def main(argv=None):
    if sys.version_info < (3, 10):  # noqa: UP036 - run.py works without packaging
        sys.exit("Python 3.10 or newer is required.")
    from .server import serve

    ap = argparse.ArgumentParser(prog="datacatalog", description="DataCatalog - organize data on external drives")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--password")
    ap.add_argument("--open", dest="open_root")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = ap.parse_args(argv)
    if a.host not in ("127.0.0.1", "localhost", "::1") and not a.password:
        sys.exit("Network access requires a password: add --password YOUR_PASSWORD")
    app, httpd = serve(a.host, a.port, a.password, a.open_root)
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '::') else a.host}:{a.port}/"
    print(f"DataCatalog is running: {url}  (Ctrl+C to quit)")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
        if app.job:
            app.job.cancel.set()
        if app.thread:
            app.thread.join(timeout=10)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
