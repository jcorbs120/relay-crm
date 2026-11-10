"""Relay CRM desktop launcher.

Starts the CRM on 127.0.0.1 (this computer only) and shows it in its own
window using the Microsoft Edge WebView2 engine built into Windows 10/11.
If that engine is missing it falls back to the default browser.
"""
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from relay import APP_NAME, __version__, paths

PREFERRED_PORT = 5055


def setup_logging():
    handler = logging.handlers.RotatingFileHandler(paths.log_path(), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr and not getattr(sys, "frozen", False):
        root.addHandler(logging.StreamHandler())
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def already_running(port):
    """If another copy of Relay is already serving, reuse it instead of starting a second one."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as r:
            return b"relay-crm" in r.read()
    except Exception:
        return False


def free_port(preferred):
    for port in (preferred, 0):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            p = s.getsockname()[1]
            s.close()
            return p
        except OSError:
            s.close()
    raise RuntimeError("No free port")


def start_server(port):
    from werkzeug.serving import make_server
    from relay.app import create_app
    app = create_app()
    srv = make_server("127.0.0.1", port, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="relay-http")
    t.start()
    return srv


def wait_until_up(url, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url + "/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.15)
    return False


def show_window(url):
    """Native window. Returns False if no embedded browser engine is available."""
    try:
        import webview
    except Exception as e:
        logging.warning("pywebview unavailable: %s", e)
        return False
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True                 # backups / CSV exports
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # Microsoft sign-in page opens in your browser
    except Exception:
        pass
    try:
        webview.create_window(APP_NAME, url, width=1380, height=900, min_size=(900, 600), text_select=True)
        storage = str(paths.data_dir() / "webview")
        kw = {"private_mode": False, "storage_path": storage}
        if os.name == "nt":
            kw["gui"] = "edgechromium"
        webview.start(**kw)
        return True
    except Exception as e:
        logging.exception("embedded window failed: %s", e)
        return False


def selftest():
    """Used by the build pipeline: start the server, load key pages, exit 0 if healthy."""
    import tempfile
    os.environ.setdefault("RELAY_DATA_DIR", tempfile.mkdtemp(prefix="relay-selftest-"))
    port = free_port(0)
    srv = start_server(port)
    url = f"http://127.0.0.1:{port}"
    ok = wait_until_up(url)
    try:
        for path in ("/", "/contacts", "/pipeline", "/reports/pipeline", "/settings"):
            with urllib.request.urlopen(url + path, timeout=10) as r:
                ok = ok and r.status == 200 and b"Relay CRM" in r.read()
        import webview  # noqa: F401  (make sure the window engine was bundled)
    except Exception as e:
        logging.exception("selftest failed: %s", e)
        ok = False
    srv.shutdown()
    print("SELFTEST", "OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)


def main():
    setup_logging()
    if "--selftest" in sys.argv:
        return selftest()
    log = logging.getLogger("relay.desktop")
    log.info("%s %s starting; data in %s", APP_NAME, __version__, paths.data_dir())
    headless = "--browser" in sys.argv or os.environ.get("RELAY_BROWSER") == "1"

    if already_running(PREFERRED_PORT):
        url = f"http://127.0.0.1:{PREFERRED_PORT}"
        if headless or not show_window(url):
            webbrowser.open(url)
        return

    port = free_port(PREFERRED_PORT)
    url = f"http://127.0.0.1:{port}"
    try:
        srv = start_server(port)
    except Exception:
        log.exception("could not start")
        _fatal("Relay CRM could not start. Details are in:\n" + str(paths.log_path()))
        return
    if not wait_until_up(url):
        _fatal("Relay CRM did not start in time. Details are in:\n" + str(paths.log_path()))
        return
    log.info("serving on %s", url)

    if not headless and show_window(url):
        srv.shutdown()          # window closed -> app exits
        return
    # Fallback: open in the default browser and keep serving until the process is stopped.
    webbrowser.open(url)
    if os.name == "nt" and getattr(sys, "frozen", False):
        _info(f"Relay CRM is running in your web browser at {url}\n\nKeep this message open while you work. Click OK to quit Relay CRM.")
        srv.shutdown()
        return
    print(f"Relay CRM running at {url}  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()


def _fatal(msg):
    logging.error(msg)
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x10)
    else:
        print(msg, file=sys.stderr)


def _info(msg):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x40)


if __name__ == "__main__":
    main()
