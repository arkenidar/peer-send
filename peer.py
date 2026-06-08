#!/usr/bin/env python3
"""peer.py — symmetric full-mesh P2P chat node (server + client + tray in one).

Every instance is identical: it hosts its own in-memory message store *and*
gossips messages to the peers it knows about. On top of the mesh:

- **Tray-native** (wxPython): closing the window minimizes to the system tray;
  a desktop toast fires on a new remote message while the window is hidden.
- **Room-gated**: a shared ``--room`` token gates who may join/gossip.
- **Discovery seam**: ``Static`` (--peer seeds), ``Tracker`` (--tracker URL,
  rendezvous keyed on hash(room)) and ``Pex`` (membership over /health).
- **NAT traversal**: a publicly-reachable super-peer (``--serve-tracker``
  ``--relay``) reverse-proxies gossip to desktop peers behind NAT via an HTTP
  long-poll inbox (``--via-relay``).
- **Reachability**: the advertised address (``--public-url``) is decoupled from
  the bind address (``--host``/``--port``).

``server.py`` and ``client.py`` are kept alongside, untouched; this is a separate
JSON protocol that interoperates peer-to-peer only.

Transport mirrors the rest of the repo: HTTPS with the dev certificate pinned via
``ssl`` (``cafile``); hostname verification is disabled because the dev cert is
CN-only (``/CN=127.0.0.1``). wxPython is imported lazily so ``--no-gui`` peers
(e.g. a VPS super-peer) need no GUI stack.

Usage:
    python3 peer.py [--port 8443] [--peer URL ...] [--room TOKEN] ...
    python3 peer.py --no-gui --serve-tracker --relay --public-url https://vps:8443
"""
import argparse
import json
import os
import queue
import ssl
import threading
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

HTTP_TIMEOUT = 5.0          # seconds, ordinary requests
RELAY_POLL_TIMEOUT = 30.0   # seconds, client side of the long-poll
LONGPOLL_WAIT = 25.0        # seconds, server holds the inbox open
PULL_INTERVAL = 2.0         # seconds, anti-entropy pull
DISCOVERY_INTERVAL = 5.0    # seconds, discovery refresh / announce
PEER_TTL = 30.0             # seconds, discovered peer staleness
TRACKER_TTL = 60.0          # seconds, tracker roster staleness


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Local-first message store (lock-guarded). Mirrors backend/server.py helpers,
# but ids are the composite ``origin:seq`` so they never collide across peers.
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, peer_id: str) -> None:
        self.peer_id = peer_id
        self._messages: list[dict] = []
        self._seen: set[tuple] = set()
        self._self_seq = 0
        self._lock = threading.Lock()

    def add_local(self, author: str, text: str) -> dict:
        """Create a message this peer originates and return it."""
        with self._lock:
            self._self_seq += 1
            msg = {
                "origin": self.peer_id,
                "seq": self._self_seq,
                "author": author,
                "text": text,
                "timestamp": utc_now(),
            }
            msg["id"] = f"{self.peer_id}:{self._self_seq}"
            self._seen.add((self.peer_id, self._self_seq))
            self._messages.append(msg)
        return msg

    def merge(self, msg: dict) -> bool:
        """Insert a foreign/pulled message under the lock. True if newly added.

        Dedup on ``(origin, seq)`` makes gossip idempotent and stops flood loops.
        """
        try:
            key = (str(msg["origin"]), int(msg["seq"]))
        except (KeyError, TypeError, ValueError):
            return False
        item = {
            "origin": key[0],
            "seq": key[1],
            "author": str(msg.get("author", "guest")),
            "text": str(msg.get("text", "")),
            "timestamp": str(msg.get("timestamp", utc_now())),
            "id": f"{key[0]}:{key[1]}",
        }
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            self._messages.append(item)
        return True

    def snapshot(self) -> list[dict]:
        """All messages, ordered best-effort by wall clock then (origin, seq)."""
        with self._lock:
            items = list(self._messages)
        items.sort(key=lambda m: (m["timestamp"], m["origin"], m["seq"]))
        return items

    def clear(self) -> None:
        """Drop local messages. ``_self_seq`` stays monotonic so our own ids never
        recycle; ``_seen`` is cleared too, so anti-entropy pull may re-populate
        the store from peers that still hold the history (documented behaviour)."""
        with self._lock:
            self._messages.clear()
            self._seen.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._messages)


# ---------------------------------------------------------------------------
# Known-peer registry. Static seeds are permanent; discovered peers age out.
# ---------------------------------------------------------------------------
class PeerRegistry:
    def __init__(self, self_url: str, ttl: float = PEER_TTL) -> None:
        self.self_url = self_url.rstrip("/")
        self.ttl = ttl
        self._static: set[str] = set()
        self._discovered: dict[str, dict] = {}  # url -> {peer_id, last_seen}
        self._lock = threading.Lock()

    def add_static(self, url: str) -> None:
        url = url.rstrip("/")
        if url and url != self.self_url:
            with self._lock:
                self._static.add(url)

    def add(self, url: str, peer_id: str | None = None) -> None:
        if not url:
            return
        url = url.rstrip("/")
        if url == self.self_url or url in self._static:
            return
        with self._lock:
            entry = self._discovered.setdefault(url, {"peer_id": None, "last_seen": 0.0})
            if peer_id:
                entry["peer_id"] = peer_id
            entry["last_seen"] = time.time()

    def urls(self) -> list[str]:
        now = time.time()
        with self._lock:
            stale = [u for u, e in self._discovered.items() if now - e["last_seen"] > self.ttl]
            for u in stale:
                del self._discovered[u]
            return sorted(self._static | set(self._discovered))

    def snapshot(self) -> list[dict]:
        with self._lock:
            out = [{"url": u, "peer_id": None} for u in self._static]
            out += [{"url": u, "peer_id": e["peer_id"]} for u, e in self._discovered.items()]
        return out

    def count(self) -> int:
        return len(self.urls())


# ---------------------------------------------------------------------------
# Discovery seam: each implementation's refresh() returns (url, peer_id) pairs.
# A future DhtDiscovery drops in here unchanged.
# ---------------------------------------------------------------------------
class StaticDiscovery:
    def __init__(self, seeds: list[str]) -> None:
        self.seeds = [s.rstrip("/") for s in seeds]

    def refresh(self) -> list[tuple]:
        return [(s, None) for s in self.seeds]


class TrackerDiscovery:
    """Announce self to each tracker and learn the room roster back."""

    def __init__(self, peer: "Peer", trackers: list[str]) -> None:
        self.peer = peer
        self.trackers = [t.rstrip("/") for t in trackers]

    def refresh(self) -> list[tuple]:
        out: list[tuple] = []
        body = {
            "room": self.peer.room,
            "peer_id": self.peer.peer_id,
            "url": self.peer.public_url,
        }
        for t in self.trackers:
            try:
                data = self.peer.http(f"{t}/tracker/announce", "POST", body=body)
                for p in data.get("peers", []):
                    if p.get("url"):
                        out.append((p["url"], p.get("peer_id")))
            except Exception:
                pass
        return out


class PexDiscovery:
    """Peer-exchange: read each known peer's advertised membership from /health."""

    def __init__(self, peer: "Peer") -> None:
        self.peer = peer

    def refresh(self) -> list[tuple]:
        out: list[tuple] = []
        for url in self.peer.registry.urls():
            try:
                data = self.peer.http(f"{url}/health", "GET")
                if data.get("url"):
                    out.append((data["url"], data.get("peer_id")))
                for p in data.get("peers", []):
                    if p.get("url"):
                        out.append((p["url"], p.get("peer_id")))
            except Exception:
                pass
        return out


# ---------------------------------------------------------------------------
# Super-peer services (only active when the corresponding flag is set).
# ---------------------------------------------------------------------------
class TrackerService:
    """Rendezvous: room -> {peer_id: (url, last_seen)}; keyed on the room token."""

    def __init__(self, ttl: float = TRACKER_TTL) -> None:
        self.ttl = ttl
        self._rooms: dict[str, dict] = {}
        self._lock = threading.Lock()

    def announce(self, room: str, peer_id: str, url: str) -> list[dict]:
        with self._lock:
            self._rooms.setdefault(room, {})[peer_id] = {"url": url, "last_seen": time.time()}
            return self._roster(room)

    def peers(self, room: str) -> list[dict]:
        with self._lock:
            return self._roster(room)

    def _roster(self, room: str) -> list[dict]:
        now = time.time()
        members = self._rooms.get(room, {})
        stale = [pid for pid, e in members.items() if now - e["last_seen"] > self.ttl]
        for pid in stale:
            del members[pid]
        return [{"peer_id": pid, "url": e["url"]} for pid, e in members.items()]


class RelayService:
    """Reverse tunnel: a per-(room, peer_id) inbox other peers enqueue into and a
    NAT'd peer drains via long-poll. Pure stdlib HTTP, no websockets."""

    def __init__(self) -> None:
        self._inboxes: dict[tuple, queue.Queue] = {}
        self._lock = threading.Lock()

    def _inbox(self, room: str, peer_id: str) -> queue.Queue:
        key = (room, peer_id)
        with self._lock:
            q = self._inboxes.get(key)
            if q is None:
                q = queue.Queue()
                self._inboxes[key] = q
            return q

    def enqueue(self, room: str, peer_id: str, msg: dict) -> None:
        self._inbox(room, peer_id).put(msg)

    def poll(self, room: str, peer_id: str, stop: threading.Event, wait: float = LONGPOLL_WAIT) -> list[dict]:
        q = self._inbox(room, peer_id)
        deadline = time.time() + wait
        out: list[dict] = []
        while time.time() < deadline and not stop.is_set():
            try:
                out.append(q.get(timeout=1.0))
                break
            except queue.Empty:
                continue
        while True:  # drain anything else already queued
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                break
        return out


# ---------------------------------------------------------------------------
# HTTP request handler. The owning Peer is attached as ``server.peer``.
# ---------------------------------------------------------------------------
class PeerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def peer(self) -> "Peer":
        return self.server.peer  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _room_ok(self) -> bool:
        return self.headers.get("X-Room", "") == self.peer.room

    # -- GET -----------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/health":
            self._send_json(HTTPStatus.OK, {
                "status": "ok",
                "peer_id": self.peer.peer_id,
                "url": self.peer.public_url,
                "room": self.peer.room,
                "role": self.peer.role,
                "message_count": self.peer.store.count(),
                "peers": self.peer.registry.snapshot(),
            })
            return

        if path == "/messages":
            if not self._room_ok():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "wrong_room"})
                return
            self._send_json(HTTPStatus.OK, {"messages": self.peer.store.snapshot()})
            return

        if path == "/tracker/peers" and self.peer.tracker is not None:
            room = params.get("room", [""])[0]
            self._send_json(HTTPStatus.OK, {"peers": self.peer.tracker.peers(room)})
            return

        relay_pid = self._relay_peer_id(path, "inbox")
        if relay_pid is not None and self.peer.relay is not None:
            room = params.get("room", [self.peer.room])[0]
            msgs = self.peer.relay.poll(room, relay_pid, self.peer.stop)
            self._send_json(HTTPStatus.OK, {"messages": msgs})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    # -- POST ----------------------------------------------------------------
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return

        if path == "/gossip":
            if not self._room_ok():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "wrong_room"})
                return
            sender = self.headers.get("X-Sender-Url", "")
            self.peer.handle_gossip(payload, sender)
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return

        if path == "/messages":
            author = str(payload.get("author", "guest")).strip() or "guest"
            text = str(payload.get("text", "")).strip()
            if not text:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "empty_message"})
                return
            msg = self.peer.send(author, text)
            self._send_json(HTTPStatus.CREATED, msg)
            return

        if path == "/tracker/announce" and self.peer.tracker is not None:
            roster = self.peer.tracker.announce(
                str(payload.get("room", "")),
                str(payload.get("peer_id", "")),
                str(payload.get("url", "")),
            )
            self._send_json(HTTPStatus.OK, {"peers": roster})
            return

        relay_pid = self._relay_peer_id(path, "gossip")
        if relay_pid is not None and self.peer.relay is not None:
            room = self.headers.get("X-Room", self.peer.room)
            self.peer.relay.enqueue(room, relay_pid, payload)
            self._send_json(HTTPStatus.OK, {"status": "queued"})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    @staticmethod
    def _relay_peer_id(path: str, leaf: str) -> str | None:
        """Match /relay/<peer_id>/<leaf>; return <peer_id> or None."""
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "relay" and parts[2] == leaf:
            return parts[1]
        return None


# ---------------------------------------------------------------------------
# The peer: store + transport + discovery + background workers.
# ---------------------------------------------------------------------------
class Peer:
    def __init__(
        self,
        *,
        peer_id: str,
        room: str,
        name: str,
        public_url: str,
        bind_host: str,
        bind_port: int,
        cert_path: str,
        key_path: str,
        seeds: list[str],
        trackers: list[str],
        serve_tracker: bool,
        relay: bool,
        via_relay: str | None,
    ) -> None:
        self.peer_id = peer_id
        self.room = room
        self.name = name
        self.public_url = public_url.rstrip("/")
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.cert_path = cert_path
        self.key_path = key_path
        self.via_relay = via_relay.rstrip("/") if via_relay else None

        self.store = Store(peer_id)
        self.registry = PeerRegistry(self.public_url)
        for s in seeds:
            self.registry.add_static(s)

        self.tracker = TrackerService() if serve_tracker else None
        self.relay = RelayService() if relay else None

        self.discoveries: list = [StaticDiscovery(seeds)]
        if trackers:
            self.discoveries.append(TrackerDiscovery(self, trackers))
        self.discoveries.append(PexDiscovery(self))

        self.role = self._role(serve_tracker, relay, via_relay)

        # Pinned-cert client context (outbound), like client.py's ChatService.
        try:
            ctx = ssl.create_default_context(cafile=cert_path)
            ctx.check_hostname = False
            self._client_ctx = ctx
        except OSError:
            self._client_ctx = None

        self.stop = threading.Event()
        self._push_q: queue.Queue = queue.Queue()
        self._httpd: ThreadingHTTPServer | None = None
        self._threads: list[threading.Thread] = []

    @staticmethod
    def _role(serve_tracker: bool, relay: bool, via_relay: str | None) -> str:
        tags = []
        if serve_tracker:
            tags.append("tracker")
        if relay:
            tags.append("relay")
        if via_relay:
            tags.append("via-relay")
        return f"super-peer({','.join(tags)})" if (serve_tracker or relay) else (
            "peer(via-relay)" if via_relay else "peer")

    # -- outbound HTTP -------------------------------------------------------
    def http(self, url: str, method: str = "GET", body: dict | None = None,
             timeout: float | None = None, extra_headers: dict | None = None) -> dict:
        headers = {"X-Room": self.room, "X-From-Peer": self.peer_id}
        if extra_headers:
            headers.update(extra_headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, context=self._client_ctx,
                                    timeout=timeout or HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    # -- message paths -------------------------------------------------------
    def send(self, author: str, text: str) -> dict:
        msg = self.store.add_local(author, text)
        self._push_q.put((msg, None))
        return msg

    def handle_gossip(self, msg: dict, sender_url: str) -> None:
        if self.store.merge(msg):
            self._push_q.put((msg, sender_url.rstrip("/") if sender_url else None))

    # -- background workers --------------------------------------------------
    def _push_worker(self) -> None:
        while not self.stop.is_set():
            try:
                item = self._push_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            msg, exclude = item
            for url in self.registry.urls():
                if exclude and url == exclude:
                    continue
                try:
                    self.http(f"{url}/gossip", "POST", body=msg,
                              extra_headers={"X-Sender-Url": self.public_url})
                except Exception:
                    pass  # pull will heal

    def _pull_worker(self) -> None:
        while not self.stop.wait(PULL_INTERVAL):
            for url in self.registry.urls():
                try:
                    data = self.http(f"{url}/messages", "GET")
                    for m in data.get("messages", []):
                        if self.store.merge(m):
                            self._push_q.put((m, url))
                except Exception:
                    pass

    def _discovery_worker(self) -> None:
        while True:
            for d in self.discoveries:
                try:
                    for url, pid in d.refresh():
                        self.registry.add(url, pid)
                except Exception:
                    pass
            if self.stop.wait(DISCOVERY_INTERVAL):
                break

    def _relay_worker(self) -> None:
        """NAT'd peer: drain our inbox on the relay (long-poll) and merge."""
        inbox = f"{self.via_relay}/relay/{self.peer_id}/inbox?room={quote(self.room)}"
        while not self.stop.is_set():
            try:
                data = self.http(inbox, "GET", timeout=RELAY_POLL_TIMEOUT)
                for m in data.get("messages", []):
                    if self.store.merge(m):
                        self._push_q.put((m, self.via_relay))
            except Exception:
                self.stop.wait(1.0)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        cert, key = Path(self.cert_path), Path(self.key_path)
        if not cert.exists() or not key.exists():
            raise SystemExit(
                "Missing certificate files. Run backend/scripts/generate-dev-cert.sh first."
            )
        httpd = ThreadingHTTPServer((self.bind_host, self.bind_port), PeerHandler)
        httpd.daemon_threads = True
        httpd.peer = self  # type: ignore[attr-defined]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self._httpd = httpd

        workers = [
            threading.Thread(target=httpd.serve_forever, name="http", daemon=True),
            threading.Thread(target=self._push_worker, name="push", daemon=True),
            threading.Thread(target=self._pull_worker, name="pull", daemon=True),
            threading.Thread(target=self._discovery_worker, name="discovery", daemon=True),
        ]
        if self.via_relay:
            workers.append(threading.Thread(target=self._relay_worker, name="relay", daemon=True))
        for t in workers:
            t.start()
        self._threads = workers

    def stop_all(self) -> None:
        self.stop.set()
        self._push_q.put(None)
        if self._httpd is not None:
            self._httpd.shutdown()
        for t in self._threads:
            t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# wxPython GUI + system tray (imported lazily; absent under --no-gui).
# ---------------------------------------------------------------------------
def run_gui(peer: Peer, use_tray: bool) -> None:
    try:
        import wx
        import wx.adv
    except ImportError:
        raise SystemExit(
            "The GUI requires wxPython. Install it (e.g. "
            "`sudo apt install -y python3-wxgtk4.0`) or run with --no-gui."
        )

    def make_icon() -> "wx.Icon":
        bmp = wx.Bitmap(32, 32)
        dc = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(wx.Colour(38, 42, 48)))
        dc.Clear()
        dc.SetBrush(wx.Brush(wx.Colour(60, 170, 100)))
        dc.SetPen(wx.Pen(wx.Colour(230, 230, 230), 2))
        dc.DrawCircle(16, 16, 11)
        dc.SelectObject(wx.NullBitmap)
        icon = wx.Icon()
        icon.CopyFromBitmap(bmp)
        return icon

    class PeerTray(wx.adv.TaskBarIcon):
        def __init__(self, frame: "PeerFrame") -> None:
            super().__init__()
            self.frame = frame
            self.SetIcon(frame.icon, f"peer {peer.peer_id} · room {peer.room}")
            self.Bind(wx.adv.EVT_TASKBAR_LEFT_DOWN, lambda _e: self.frame.toggle_show())

        def CreatePopupMenu(self) -> "wx.Menu":
            menu = wx.Menu()
            show = menu.Append(wx.ID_ANY, "Hide window" if self.frame.IsShown() else "Show window")
            clear = menu.Append(wx.ID_ANY, "Clear messages")
            menu.AppendSeparator()
            quit_item = menu.Append(wx.ID_EXIT, "Quit")
            menu.Bind(wx.EVT_MENU, lambda _e: self.frame.toggle_show(), show)
            menu.Bind(wx.EVT_MENU, lambda _e: self.frame.do_clear(), clear)
            menu.Bind(wx.EVT_MENU, lambda _e: self.frame.do_quit(), quit_item)
            return menu

    class PeerFrame(wx.Frame):
        POLL_MS = 1500

        def __init__(self) -> None:
            super().__init__(None, wx.ID_ANY, f"peer {peer.peer_id} · {peer.room}",
                             wx.DefaultPosition, wx.Size(760, 540))
            # Cache one icon and keep toast references alive: wxPython on GTK
            # segfaults if the GdkWindow behind a tray icon / NotificationMessage
            # is garbage-collected while GTK still references it.
            self.icon = make_icon()
            self._notifications: list = []
            self.tray = PeerTray(self) if use_tray else None
            self._shown_ids: set[str] = set()
            self._first_refresh = True

            panel = wx.Panel(self)
            sizer = wx.BoxSizer(wx.VERTICAL)
            self.status_label = wx.StaticText(panel, wx.ID_ANY, self._status_text())
            sizer.Add(self.status_label, 0, wx.ALL | wx.EXPAND, 8)
            self.transcript = wx.TextCtrl(
                panel, wx.ID_ANY, "", wx.DefaultPosition, wx.DefaultSize,
                wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
            sizer.Add(self.transcript, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

            controls = wx.BoxSizer(wx.HORIZONTAL)
            self.author_input = wx.TextCtrl(panel, wx.ID_ANY, peer.name,
                                            wx.DefaultPosition, wx.Size(130, -1))
            self.message_input = wx.TextCtrl(panel, wx.ID_ANY, "", wx.DefaultPosition,
                                             wx.DefaultSize, wx.TE_PROCESS_ENTER)
            send_btn = wx.Button(panel, wx.ID_ANY, "Send")
            clear_btn = wx.Button(panel, wx.ID_ANY, "Clear")
            controls.Add(self.author_input, 0, wx.RIGHT, 8)
            controls.Add(self.message_input, 1, wx.RIGHT | wx.EXPAND, 8)
            controls.Add(send_btn, 0, wx.RIGHT, 8)
            controls.Add(clear_btn, 0)
            sizer.Add(controls, 0, wx.ALL | wx.EXPAND, 8)
            panel.SetSizer(sizer)

            send_btn.Bind(wx.EVT_BUTTON, self.on_send)
            clear_btn.Bind(wx.EVT_BUTTON, lambda _e: self.do_clear())
            self.message_input.Bind(wx.EVT_TEXT_ENTER, self.on_send)
            self.Bind(wx.EVT_CLOSE, self.on_close)

            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, lambda _e: self.refresh(), self.timer)
            self.timer.Start(self.POLL_MS)
            self.refresh()

        def _status_text(self) -> str:
            return (f"{peer.peer_id} · room {peer.room} · {peer.role} · "
                    f"{peer.registry.count()} peers · {peer.store.count()} msgs")

        def refresh(self) -> None:
            if not self:  # underlying C++ frame already destroyed; do not paint
                return
            snap = peer.store.snapshot()
            ids = [m["id"] for m in snap]
            new_ids = [i for i in ids if i not in self._shown_ids]
            if new_ids:
                if self.IsShown():
                    # Only touch GTK widgets while visible; updating a hidden
                    # TextCtrl crashes wxGTK (gtk_text_buffer assertions).
                    self.transcript.SetValue("".join(
                        "[{timestamp}] {author}: {text}\n".format(**m) for m in snap))
                    self.transcript.ShowPosition(self.transcript.GetLastPosition())
                    self.status_label.SetLabel(self._status_text())
                else:
                    fresh_remote = [m for m in snap
                                    if m["id"] in new_ids and m["origin"] != peer.peer_id]
                    if fresh_remote and not self._first_refresh:
                        self._toast(fresh_remote)
                self._shown_ids = set(ids)
            elif self.IsShown():
                self.status_label.SetLabel(self._status_text())
            if self.tray is not None:
                self.tray.SetIcon(self.icon,
                                  f"peer {peer.peer_id} · {peer.store.count()} msgs")
            self._first_refresh = False

        def _toast(self, msgs: list[dict]) -> None:
            try:
                if len(msgs) <= 3:
                    notes = [(f"{m['author']} · {peer.room}", m["text"]) for m in msgs]
                else:
                    notes = [(f"peer · {peer.room}", f"{len(msgs)} new messages")]
                for title, body in notes:
                    note = wx.adv.NotificationMessage(title, body)
                    self._notifications.append(note)  # keep alive past .Show()
                    note.Show()
                del self._notifications[:-20]  # bound growth, retain recent refs
            except Exception:
                pass

        def on_send(self, _event) -> None:
            text = self.message_input.GetValue().strip()
            if not text:
                return
            author = self.author_input.GetValue().strip() or peer.name
            peer.send(author, text)
            self.message_input.SetValue("")
            self.refresh()

        def do_clear(self) -> None:
            peer.store.clear()
            self._shown_ids.clear()
            self.transcript.SetValue("")
            self.refresh()

        def toggle_show(self) -> None:
            if self.IsShown():
                self.Hide()
            else:
                self._shown_ids.clear()  # rebuild transcript incl. msgs missed while hidden
                self.Show()
                self.Raise()
                self.refresh()

        def on_close(self, event) -> None:
            # Minimize to tray only on a graceful, vetoable close (the X button /
            # WM_DELETE_WINDOW). A forced destroy can't be vetoed, so quit then.
            if self.tray is not None and event.CanVeto():
                event.Veto()
                self.Hide()
            else:
                self.do_quit()

        def do_quit(self) -> None:
            self.timer.Stop()
            if self.tray is not None:
                self.tray.RemoveIcon()
                self.tray.Destroy()
            peer.stop_all()
            self.Destroy()

    app = wx.App()
    frame = PeerFrame()
    frame.Show(True)
    app.SetTopWindow(frame)
    app.MainLoop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    certs = here / "backend" / "certs"
    p = argparse.ArgumentParser(description="Run a wxsend full-mesh P2P chat peer.")
    p.add_argument("--host", default="127.0.0.1", help="local bind host")
    p.add_argument("--port", type=int, default=8443, help="local bind port")
    p.add_argument("--public-url", default=None,
                   help="address others use to reach this peer (default https://host:port)")
    p.add_argument("--peer", action="append", default=[], metavar="URL",
                   help="static seed peer (repeatable)")
    p.add_argument("--tracker", action="append", default=[], metavar="URL",
                   help="tracker to announce to / discover via (repeatable)")
    p.add_argument("--room", default="lobby", help="room token (join gate / rendezvous key)")
    p.add_argument("--serve-tracker", action="store_true", help="run the tracker service")
    p.add_argument("--relay", action="store_true", help="run the relay (reverse-tunnel) service")
    p.add_argument("--via-relay", default=None, metavar="URL",
                   help="reach this peer through the relay at URL (for NAT'd peers)")
    p.add_argument("--cert", default=str(certs / "cert.pem"))
    p.add_argument("--key", default=str(certs / "key.pem"))
    p.add_argument("--id", default=None, help="stable peer id (default random)")
    p.add_argument("--name", default=os.environ.get("USER") or "guest", help="author name")
    p.add_argument("--no-gui", action="store_true", help="run headless (no wxPython)")
    p.add_argument("--no-tray", action="store_true", help="GUI without a tray icon")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    peer_id = args.id or uuid.uuid4().hex[:8]

    if args.public_url:
        public_url = args.public_url
    elif args.via_relay:
        public_url = f"{args.via_relay.rstrip('/')}/relay/{peer_id}"
    else:
        public_url = f"https://{args.host}:{args.port}"

    peer = Peer(
        peer_id=peer_id,
        room=args.room,
        name=args.name,
        public_url=public_url,
        bind_host=args.host,
        bind_port=args.port,
        cert_path=args.cert,
        key_path=args.key,
        seeds=args.peer,
        trackers=args.tracker,
        serve_tracker=args.serve_tracker,
        relay=args.relay,
        via_relay=args.via_relay,
    )
    peer.start()
    banner = (f"peer {peer.peer_id} [{peer.role}] room={peer.room} "
              f"bind=https://{args.host}:{args.port} public={peer.public_url}")
    print(banner, flush=True)

    if args.no_gui:
        try:
            while not peer.stop.wait(3600):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            peer.stop_all()
    else:
        run_gui(peer, use_tray=not args.no_tray)


if __name__ == "__main__":
    main()
