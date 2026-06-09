#!/usr/bin/env python3
import argparse
import json
import ssl
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

_MESSAGES = []
_NEXT_ID = 1
_LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_message(author: str, text: str) -> dict:
    """Append a message under the lock and return the stored item."""
    global _NEXT_ID
    with _LOCK:
        item = {
            "id": _NEXT_ID,
            "author": author,
            "text": text,
            "timestamp": utc_now(),
        }
        _MESSAGES.append(item)
        _NEXT_ID += 1
    return item


def snapshot_messages(after: int = 0) -> list[dict]:
    """Return a copy of messages with id > ``after`` taken under the lock."""
    with _LOCK:
        return [item for item in _MESSAGES if item["id"] > after]


def clear_messages() -> None:
    """Drop all stored messages. ``_NEXT_ID`` stays monotonic so that
    polling clients using ``?after=`` never re-see a recycled id."""
    with _LOCK:
        _MESSAGES.clear()


def message_count() -> int:
    with _LOCK:
        return len(_MESSAGES)


class ChatHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_tsv(self, items: list[dict]) -> None:
        rows = [
            "\t".join(
                [
                    str(item["id"]),
                    quote(item["author"], safe=""),
                    quote(item["text"], safe=""),
                    item["timestamp"],
                ]
            )
            for item in items
        ]
        body = ("\n".join(rows) + ("\n" if rows else "")).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "message_count": len(_MESSAGES)})
            return

        if parsed.path != "/messages":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        params = parse_qs(parsed.query)
        after = int(params.get("after", ["0"])[0])
        items = snapshot_messages(after)
        if params.get("format", [""])[0] == "tsv":
            self._send_tsv(items)
            return
        self._send_json(HTTPStatus.OK, {"messages": items})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/messages":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return

        author = str(payload.get("author", "guest")).strip() or "guest"
        text = str(payload.get("text", "")).strip()
        if not text:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "empty_message"})
            return

        item = add_message(author, text)
        self._send_json(HTTPStatus.CREATED, item)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the wxsend HTTPS chat backend.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    certs = Path(__file__).resolve().parent.parent / "certs"
    parser.add_argument(
        "--cert",
        default=str(certs / "cert.pem"),
    )
    parser.add_argument(
        "--key",
        default=str(certs / "key.pem"),
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Open a wxPython monitor/admin window (requires wxPython).",
    )
    return parser.parse_args()


def run_gui(httpd: ThreadingHTTPServer, host: str, port: int) -> None:
    """Serve in a background thread and run a wxPython admin window on the
    main thread. wxPython is imported lazily so it stays an optional
    dependency for headless deployments."""
    try:
        import wx
    except ImportError:
        raise SystemExit(
            "The --gui option requires wxPython. Install it (e.g. "
            "`sudo apt install -y python3-wxgtk4.0`) or run without --gui."
        )

    class ServerFrame(wx.Frame):
        POLL_INTERVAL_MS = 1000

        def __init__(self) -> None:
            super().__init__(
                None,
                wx.ID_ANY,
                f"wxsend server — {host}:{port}",
                wx.DefaultPosition,
                wx.Size(720, 520),
            )
            self.last_id = 0

            panel = wx.Panel(self, wx.ID_ANY)
            sizer = wx.BoxSizer(wx.VERTICAL)

            self.status_label = wx.StaticText(panel, wx.ID_ANY, self._status_text())
            sizer.Add(self.status_label, 0, wx.ALL | wx.EXPAND, 8)

            self.transcript = wx.TextCtrl(
                panel,
                wx.ID_ANY,
                "",
                wx.DefaultPosition,
                wx.DefaultSize,
                wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
            )
            sizer.Add(self.transcript, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

            controls = wx.BoxSizer(wx.HORIZONTAL)
            self.author_input = wx.TextCtrl(
                panel, wx.ID_ANY, "admin", wx.DefaultPosition, wx.Size(120, -1)
            )
            self.message_input = wx.TextCtrl(
                panel, wx.ID_ANY, "", wx.DefaultPosition, wx.DefaultSize, wx.TE_PROCESS_ENTER
            )
            send_button = wx.Button(panel, wx.ID_ANY, "Send")
            clear_button = wx.Button(panel, wx.ID_ANY, "Clear")
            controls.Add(self.author_input, 0, wx.RIGHT, 8)
            controls.Add(self.message_input, 1, wx.RIGHT | wx.EXPAND, 8)
            controls.Add(send_button, 0, wx.RIGHT, 8)
            controls.Add(clear_button, 0)
            sizer.Add(controls, 0, wx.ALL | wx.EXPAND, 8)

            panel.SetSizer(sizer)

            send_button.Bind(wx.EVT_BUTTON, self.on_send)
            clear_button.Bind(wx.EVT_BUTTON, self.on_clear)
            self.message_input.Bind(wx.EVT_TEXT_ENTER, self.on_send)
            self.Bind(wx.EVT_CLOSE, self.on_close)

            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
            self.timer.Start(self.POLL_INTERVAL_MS)
            self.refresh()

        @staticmethod
        def _status_text() -> str:
            return f"Serving https://{host}:{port} — {message_count()} messages"

        def refresh(self) -> None:
            for item in snapshot_messages(self.last_id):
                self.transcript.AppendText("[{timestamp}] {author}: {text}\n".format(**item))
                self.last_id = item["id"]
            self.status_label.SetLabel(self._status_text())

        def on_timer(self, _event: "wx.TimerEvent") -> None:
            self.refresh()

        def on_send(self, _event: "wx.Event") -> None:
            text = self.message_input.GetValue().strip()
            if not text:
                return
            author = self.author_input.GetValue().strip() or "admin"
            add_message(author, text)
            self.message_input.SetValue("")
            self.refresh()

        def on_clear(self, _event: "wx.Event") -> None:
            clear_messages()
            self.transcript.SetValue("")
            self.refresh()

        def on_close(self, event: "wx.CloseEvent") -> None:
            self.timer.Stop()
            event.Skip()

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    print(f"wxsend backend listening on https://{host}:{port} (GUI mode)")

    app = wx.App()
    frame = ServerFrame()
    frame.Show(True)
    app.SetTopWindow(frame)
    app.MainLoop()

    httpd.shutdown()
    server_thread.join(timeout=2)


def main() -> None:
    args = parse_args()
    cert_path = Path(args.cert)
    key_path = Path(args.key)
    if not cert_path.exists() or not key_path.exists():
        raise SystemExit(
            "Missing certificate files. Run scripts/generate-dev-cert.sh first."
        )

    httpd = ThreadingHTTPServer((args.host, args.port), ChatHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    if args.gui:
        run_gui(httpd, args.host, args.port)
        return
    print(f"wxsend backend listening on https://{args.host}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
