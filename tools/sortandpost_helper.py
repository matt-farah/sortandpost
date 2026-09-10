#!/usr/bin/env python3
"""sortandpost file-manager helper -- run this on the HOST, outside Docker.

The sortandpost web app runs in a container and can't open Finder / Explorer or
browse the host filesystem. This ~200-line stdlib-only agent does both. Start it
at login pointing at the same app-data folder Docker mounts:

    python3 tools/sortandpost_helper.py --app-data /Users/you/sortandpost-data

It listens on 127.0.0.1 and writes ``<app-data>/local/helper.json`` with a port
and a random token; the container reads that file through its ``/data`` mount and
hands them to the browser, which calls this agent directly.

Security: 127.0.0.1 only; every request needs the ``X-SP-Token`` header; CORS is
granted only to ``localhost`` / ``127.0.0.1`` origins; ``/reveal`` and ``/open``
act only on paths inside a folder you've registered in the app; subprocesses are
argv arrays, never a shell.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import secrets
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HELPER_VERSION = "1"
_HEARTBEAT_SEC = 60


def _platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "win"
    return "linux"


# --------------------------------------------------------------------------- #
# state shared with the request handler
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self, app_data: Path, token: str) -> None:
        self.app_data = app_data
        self.token = token

    def allowed_roots(self) -> list[Path]:
        """The ``host`` folders registered in the app
        (``sync/devices/*/library.json``). ``/reveal`` + ``/open`` are confined
        to these."""
        out: list[Path] = []
        pattern = str(self.app_data / "sync" / "devices" / "*" / "library.json")
        for f in glob.glob(pattern):
            try:
                data = json.loads(Path(f).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows = data.get("roots") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                continue
            for row in rows:
                host = row.get("host") if isinstance(row, dict) else row
                if isinstance(host, str) and host.strip():
                    with contextlib.suppress(OSError):
                        out.append(Path(host).resolve())
        return out


def _under_allowed(target: Path, roots: list[Path]) -> bool:
    return any(target == r or r in target.parents for r in roots)


def _safe_abspath(raw: object) -> Path | None:
    if not isinstance(raw, str) or "\x00" in raw or not raw.strip():
        return None
    p = Path(raw)
    if not p.is_absolute():
        return None
    try:
        return p.resolve()
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# openers
# --------------------------------------------------------------------------- #
def _spawn(args: list[str]) -> None:
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal(path: Path) -> None:
    match _platform():
        case "darwin":
            _spawn(["open", "-R", str(path)])
        case "win":
            _spawn(["explorer", f"/select,{path}"])
        case _:
            target = path.parent if path.parent.is_dir() else path
            _spawn(["xdg-open", str(target)])


def open_default(path: Path) -> None:
    match _platform():
        case "darwin":
            _spawn(["open", str(path)])
        case "win":
            os.startfile(str(path))  # type: ignore[attr-defined]  # win only
        case _:
            _spawn(["xdg-open", str(path)])


def list_dir(path: Path | None, roots: list[Path]) -> dict[str, object]:
    """Sub-directories of *path* (dir names only). ``path=None`` returns the
    starting points: registered roots + home + filesystem root / drive letters."""
    if path is None:
        starts: list[Path] = list(dict.fromkeys(roots))
        home = Path.home()
        if home not in starts:
            starts.append(home)
        if _platform() == "win":
            starts += [Path(f"{d}:\\") for d in "CDEFGH" if Path(f"{d}:\\").exists()]
        else:
            starts.append(Path("/"))
        return {
            "path": None,
            "parent": None,
            "entries": [{"name": str(p), "path": str(p), "dir": True} for p in starts],
        }

    entries: list[dict[str, object]] = []
    try:
        for child in sorted(path.iterdir(), key=lambda c: c.name.casefold()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child), "dir": True})
            except OSError:
                continue
    except (OSError, PermissionError):
        entries = []
    parent = str(path.parent) if path.parent != path else None
    return {"path": str(path), "parent": parent, "entries": entries}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class _Server(ThreadingHTTPServer):
    allow_reuse_address = True

    def server_bind(self) -> None:
        # http.server.HTTPServer.server_bind() calls socket.getfqdn(), a
        # reverse-DNS lookup that can block for many seconds on macOS. We only
        # bind 127.0.0.1 and never use server_name, so skip it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class Handler(BaseHTTPRequestHandler):
    config: Config  # set on the server instance, mirrored here in __init__

    def __init__(self, *args: object, config: Config, **kwargs: object) -> None:
        self.config = config
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def log_message(self, *_: object) -> None:  # quiet
        pass

    # -- helpers ---------------------------------------------------------- #
    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        host = urlparse(origin).hostname or ""
        if host in ("localhost", "127.0.0.1"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "X-SP-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code: int, body: dict[str, object] | None = None) -> None:
        payload = json.dumps(body or {}).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authed(self) -> bool:
        sent = self.headers.get("X-SP-Token", "")
        if not sent:
            self._send(401, {"error": "missing token"})
            return False
        if not secrets.compare_digest(sent, self.config.token):
            self._send(403, {"error": "bad token"})
            return False
        return True

    def _body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    # -- routes --------------------------------------------------------- #
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        route = urlparse(self.path)
        if route.path == "/health":
            roots = [str(r) for r in self.config.allowed_roots()]
            self._send(
                200,
                {"ok": True, "version": HELPER_VERSION, "os": _platform(), "roots": roots},
            )
            return
        if not self._authed():
            return
        if route.path == "/listdir":
            qs = parse_qs(route.query)
            raw = qs.get("path", [None])[0]
            target = _safe_abspath(raw) if raw else None
            if raw and target is None:
                self._send(400, {"error": "bad path"})
                return
            self._send(200, list_dir(target, self.config.allowed_roots()))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        route = urlparse(self.path)
        if not self._authed():
            return
        if route.path not in ("/reveal", "/open"):
            self._send(404, {"error": "not found"})
            return
        target = _safe_abspath(self._body().get("path"))
        if target is None:
            self._send(400, {"error": "bad path"})
            return
        if not _under_allowed(target, self.config.allowed_roots()):
            self._send(403, {"error": "path is not inside a registered library folder"})
            return
        if not target.exists():
            self._send(404, {"error": "no such file"})
            return
        (reveal if route.path == "/reveal" else open_default)(target)
        self._send(200, {"ok": True})


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def _write_helper_json(app_data: Path, port: int, token: str, stop: threading.Event) -> None:
    path = app_data / "local" / "helper.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    while not stop.is_set():
        path.write_text(
            json.dumps(
                {
                    "port": port,
                    "token": token,
                    "os": _platform(),
                    "pid": os.getpid(),
                    "started_at": started,
                    "heartbeat_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        stop.wait(_HEARTBEAT_SEC)
    path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-data",
        required=True,
        type=Path,
        help="host path of the folder Docker mounts at /data",
    )
    parser.add_argument("--port", type=int, default=41847)
    args = parser.parse_args(argv)

    app_data: Path = args.app_data.expanduser().resolve()
    if not (app_data / "local").is_dir() and not (app_data / "sync").is_dir():
        parser.error(f"{app_data} does not look like a sortandpost app-data folder")

    token = secrets.token_hex(24)
    config = Config(app_data, token)
    server = _Server(
        ("127.0.0.1", args.port),
        lambda *a: Handler(*a, config=config),
    )
    port = server.server_address[1]

    stop = threading.Event()
    hb = threading.Thread(
        target=_write_helper_json, args=(app_data, port, token, stop), daemon=True
    )
    hb.start()

    print(f"sortandpost-helper listening on http://127.0.0.1:{port}  (app-data: {app_data})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
