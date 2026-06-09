#!/usr/bin/env python3
"""wxPython desktop client for the wxsend chat backend.

Talks to the HTTPS backend over the standard library (``urllib`` + ``ssl``),
pinning the development certificate via ``--cacert``-style trust. Hostname
verification is disabled because the dev cert is issued for the CN ``127.0.0.1``
with no subjectAltName, which OpenSSL rejects for IP literals; pinning the exact
cert through ``cafile`` keeps the same trust model the curl-based wxLua client
relied on.

Usage:
    python3 legacy/client.py [base_url] [cert_path]
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

import wx

REQUEST_TIMEOUT = 5  # seconds


class ChatService:
    """HTTPS transport for the chat backend."""

    def __init__(self, base_url: str, cert_path: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.cert_path = cert_path
        try:
            context = ssl.create_default_context(cafile=cert_path)
            context.check_hostname = False  # see module docstring
            self._context = context
        except OSError:
            # Missing/unreadable cert: every request will fail and the UI
            # reports "Unable to reach backend", matching the curl behaviour.
            self._context = None

    def fetch_messages(self, after: int) -> list[dict]:
        """Return messages with id > ``after``. Raises on transport failure."""
        if self._context is None:
            raise OSError(f"certificate not available: {self.cert_path}")
        query = urllib.parse.urlencode({"after": after, "format": "tsv"})
        url = f"{self.base_url}/messages?{query}"
        with urllib.request.urlopen(url, context=self._context, timeout=REQUEST_TIMEOUT) as response:
            payload = response.read().decode("utf-8")
        return self._parse_tsv(payload)

    def send_message(self, author: str, text: str) -> None:
        """Post a message. Raises on transport or HTTP failure."""
        if self._context is None:
            raise OSError(f"certificate not available: {self.cert_path}")
        body = json.dumps({"author": author, "text": text}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/messages",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, context=self._context, timeout=REQUEST_TIMEOUT):
            pass

    @staticmethod
    def _parse_tsv(payload: str) -> list[dict]:
        messages = []
        for line in payload.splitlines():
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            raw_id, author, text, timestamp = parts
            if not raw_id.isdigit():
                continue
            messages.append(
                {
                    "id": int(raw_id),
                    "author": urllib.parse.unquote(author),
                    "text": urllib.parse.unquote(text),
                    "timestamp": timestamp,
                }
            )
        return messages


class ChatFrame(wx.Frame):
    POLL_INTERVAL_MS = 2000

    def __init__(self, service: ChatService, author_name: str) -> None:
        super().__init__(
            None,
            wx.ID_ANY,
            "wxsend",
            wx.DefaultPosition,
            wx.Size(760, 520),
        )
        self.service = service
        self.last_id = 0

        panel = wx.Panel(self, wx.ID_ANY)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        self.status_label = wx.StaticText(panel, wx.ID_ANY, "Disconnected")
        main_sizer.Add(self.status_label, 0, wx.ALL | wx.EXPAND, 8)

        self.transcript = wx.TextCtrl(
            panel,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        main_sizer.Add(self.transcript, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        controls = wx.BoxSizer(wx.HORIZONTAL)
        self.author_input = wx.TextCtrl(
            panel, wx.ID_ANY, author_name, wx.DefaultPosition, wx.Size(140, -1)
        )
        self.message_input = wx.TextCtrl(
            panel, wx.ID_ANY, "", wx.DefaultPosition, wx.DefaultSize, wx.TE_PROCESS_ENTER
        )
        send_button = wx.Button(panel, wx.ID_ANY, "Send")
        controls.Add(self.author_input, 0, wx.RIGHT, 8)
        controls.Add(self.message_input, 1, wx.RIGHT | wx.EXPAND, 8)
        controls.Add(send_button, 0)
        main_sizer.Add(controls, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        panel.SetSizer(main_sizer)
        self.CreateStatusBar(1)
        self.SetStatusText(service.base_url)

        send_button.Bind(wx.EVT_BUTTON, self.on_send)
        self.message_input.Bind(wx.EVT_TEXT_ENTER, self.on_send)
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self.timer.Start(self.POLL_INTERVAL_MS)

    def append_message(self, item: dict) -> None:
        self.transcript.AppendText(
            "[{timestamp}] {author}: {text}\n".format(**item)
        )

    def refresh_messages(self) -> None:
        try:
            items = self.service.fetch_messages(self.last_id)
        except (urllib.error.URLError, OSError):
            self.status_label.SetLabel("Unable to reach backend")
            return
        for item in items:
            self.append_message(item)
            self.last_id = item["id"]
        self.status_label.SetLabel("Connected")

    def on_send(self, _event: wx.Event) -> None:
        text = self.message_input.GetValue()
        if not text:
            return
        author = self.author_input.GetValue()
        try:
            self.service.send_message(author, text)
        except (urllib.error.URLError, OSError):
            self.status_label.SetLabel("Send failed")
            return
        self.message_input.SetValue("")
        self.refresh_messages()

    def on_timer(self, _event: wx.TimerEvent) -> None:
        self.refresh_messages()

    def on_close(self, event: wx.CloseEvent) -> None:
        self.timer.Stop()
        event.Skip()


def main() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_url = sys.argv[1] if len(sys.argv) > 1 else "https://127.0.0.1:8443"
    default_cert = os.path.normpath(
        os.path.join(script_dir, "..", "certs", "cert.pem")
    )
    cert_path = sys.argv[2] if len(sys.argv) > 2 else default_cert
    author_name = os.environ.get("USER") or "guest"

    service = ChatService(base_url, cert_path)

    app = wx.App()
    frame = ChatFrame(service, author_name)
    frame.Show(True)
    frame.refresh_messages()
    app.SetTopWindow(frame)
    app.MainLoop()


if __name__ == "__main__":
    main()
