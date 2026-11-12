#!/usr/bin/env python3
"""Start the Relay CRM team server.

    python run.py                          # serve on port 5055 to your network, open a browser
    python run.py --local                  # this computer only (127.0.0.1)
    python run.py --port 8443 --cert server.crt --key server.key     # HTTPS
    python run.py --behind-proxy           # running behind IIS / nginx / an Azure App Gateway
    python run.py --data-dir D:\\RelayData  # keep the database somewhere else
    python run.py --service                # no browser, for Windows services / systemd

Every option can also be set in relay.conf in the data folder (created the
first time you run). Command-line options win over the file.
"""
import argparse
import configparser
import logging
import logging.handlers
import os
import socket
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
if not getattr(sys, "frozen", False):
    sys.path.insert(0, HERE)

CONF_TEMPLATE = """# Relay CRM server settings. Restart Relay after editing.
[server]
# 0.0.0.0 = reachable by your team on the network; 127.0.0.1 = this computer only
host = 0.0.0.0
port = 5055
# HTTPS: point these at a PEM certificate and private key (ask IT for one that
# matches the server name, e.g. relay.yourfirm.local). Leave blank for HTTP.
cert =
key =
# Set to yes when Relay runs behind IIS / nginx / a load balancer that handles HTTPS
behind_proxy = no
threads = 16
"""


def read_conf(data_dir):
    path = os.path.join(data_dir, "relay.conf")
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(CONF_TEMPLATE)
        except OSError:
            pass
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    return (dict(cp["server"]) if cp.has_section("server") else {}), path


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return socket.gethostname()


def truthy(v):
    return str(v).strip().lower() in ("1", "yes", "true", "on")


def main():
    ap = argparse.ArgumentParser(description="Relay CRM team server")
    ap.add_argument("--data-dir", help="folder for the database, logs, keys and relay.conf")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--local", action="store_true", help="only this computer (127.0.0.1)")
    ap.add_argument("--cert", help="TLS certificate (PEM) for HTTPS")
    ap.add_argument("--key", help="TLS private key (PEM) for HTTPS")
    ap.add_argument("--behind-proxy", action="store_true", help="trust X-Forwarded-* from a reverse proxy")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--service", action="store_true", help="run headless (implies --no-browser)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    # The data folder must be fixed before any crm module is imported.
    if args.data_dir:
        os.environ["RELAY_DATA_DIR"] = os.path.abspath(args.data_dir)
    from crm import paths
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    conf, conf_path = read_conf(paths.DATA_DIR)

    host = "127.0.0.1" if args.local else (args.host or os.environ.get("RELAY_HOST") or conf.get("host") or "0.0.0.0")
    port = args.port or int(os.environ.get("RELAY_PORT") or conf.get("port") or 5055)
    cert = args.cert or conf.get("cert") or ""
    key = args.key or conf.get("key") or ""
    proxy = args.behind_proxy or truthy(os.environ.get("RELAY_BEHIND_PROXY", "")) or truthy(conf.get("behind_proxy", "no"))
    threads = int(conf.get("threads") or 16)
    https = bool(cert and key)
    if proxy:
        os.environ["RELAY_BEHIND_PROXY"] = "1"
    if https or proxy:
        os.environ.setdefault("RELAY_SECURE_COOKIES", "1")

    log_file = os.path.join(paths.DATA_DIR, "relay.log")
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")],
    )
    log = logging.getLogger("relay")

    from crm import __version__
    from crm.app import create_app
    app = create_app()

    scheme = "https" if https else "http"
    local_url = f"{scheme}://127.0.0.1:{port}"
    team_url = f"{scheme}://{lan_ip() if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"\n  Relay CRM {__version__} is running.")
    print(f"    On this computer:   {local_url}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"    For your team:      {team_url}")
    print(f"    Data folder:        {paths.DATA_DIR}")
    print(f"    Settings file:      {conf_path}")
    if host not in ("127.0.0.1", "localhost") and not https and not proxy:
        print("\n  NOTE: Relay is reachable on your network over plain HTTP. That's fine on a trusted office LAN,")
        print("  but for anything wider, add a certificate (--cert/--key or relay.conf) or put it behind HTTPS.")
    print("\n  Keep this window open. Press Ctrl+C to stop.\n")
    log.info("Relay %s starting on %s:%s https=%s proxy=%s data=%s", __version__, host, port, https, proxy, paths.DATA_DIR)

    if not (args.no_browser or args.service):
        threading.Timer(1.5, lambda: webbrowser.open(local_url)).start()

    if args.debug:
        app.run(host=host, port=port, debug=True, use_reloader=False,
                ssl_context=(cert, key) if https else None)
    elif https:
        from cheroot import wsgi
        from cheroot.ssl.builtin import BuiltinSSLAdapter
        server = wsgi.Server((host, port), app, numthreads=threads)
        server.ssl_adapter = BuiltinSSLAdapter(cert, key)
        try:
            server.start()
        except KeyboardInterrupt:
            server.stop()
    else:
        from waitress import serve
        serve(app, host=host, port=port, threads=threads, ident="Relay")


if __name__ == "__main__":
    main()
