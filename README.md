# wx-send

Minimal chat workspace with a wxPython desktop client and a Python HTTPS backend.

## Layout

- `client/` desktop app: `client.py` (wxPython, current) and `client.lua` (original wxLua version).
- `backend/` HTTPS chat service.
- `peer.py` symmetric full-mesh P2P chat node (server + client + tray in one);
  see [Peer (mesh)](#peer-mesh). The client/server pair above is kept as-is.

## Backend

Generate a local development certificate:

```bash
cd backend
./scripts/generate-dev-cert.sh
```

Run the backend:

```bash
cd backend
python3 server.py --host 127.0.0.1 --port 8443
```

Optionally open a wxPython monitor/admin window alongside the server (live
transcript + message count, plus Send/Clear controls). The server runs in a
background thread while the GUI owns the main thread; closing the window stops
the server. wxPython is imported lazily, so it is only required with `--gui`:

```bash
python3 server.py --gui
```

Available endpoints:

- `GET /health`
- `GET /messages?after=<id>`
- `POST /messages`

## Client

The client is a wxPython app and uses only the Python standard library
(`urllib` + `ssl`) for HTTPS, so no extra Python packages beyond wxPython are
required.

Install wxPython (Debian/Ubuntu):

```bash
sudo apt install -y python3-wxgtk4.0
```

Run the client after the backend is up:

```bash
cd peer-send   # the repository root
python3 client/client.py
```

Optional positional arguments override the defaults:

```bash
python3 client/client.py https://127.0.0.1:8443 backend/certs/cert.pem
```

## Peer (mesh)

`peer.py` is a single program where every instance is identical and joins a
**full mesh**: each peer hosts its own in-memory store *and* gossips messages to
the peers it knows. It merges the wxPython client and the HTTPS server into one
node, adds a system-tray presence, and replicates messages peer-to-peer. It uses
only the standard library (`http.server` + `urllib` + `ssl`) plus wxPython for
the GUI; `--no-gui` peers need no GUI stack. It speaks its own JSON protocol and
does **not** interoperate with `server.py`/`client.py`.

### Roles

| Role | How it runs | Reachability |
|------|-------------|--------------|
| **Desktop peer** | wx GUI + tray + mesh; optionally `--via-relay URL` | Behind NAT; reached *through* a relay |
| **VPS super-peer** | `--no-gui --serve-tracker --relay` | Public IP/domain + cert; rendezvous + relay |

A super-peer is still a full mesh participant — it just also serves the tracker
and relay endpoints.

### Local mesh (quickstart)

Generate the dev cert once (`backend/scripts/generate-dev-cert.sh`), then run a
few peers that seed each other directly:

```bash
python3 peer.py --port 8443 --room demo --peer https://127.0.0.1:8444
python3 peer.py --port 8444 --room demo --peer https://127.0.0.1:8443
```

Each opens a chat window with a tray icon. Send in one; it appears in the others.
Closing a window **minimizes to the tray** (the peer keeps gossiping); the tray
menu has Show/Hide, Clear, and Quit. A new remote message while the window is
hidden raises a desktop toast.

### Discovery via a tracker (no explicit `--peer`)

Run one super-peer as a tracker and let peers find each other by room:

```bash
python3 peer.py --no-gui --serve-tracker --room demo --port 8443      # tracker
python3 peer.py --tracker https://127.0.0.1:8443 --room demo --port 8444
python3 peer.py --tracker https://127.0.0.1:8443 --room demo --port 8445
```

The `--room` token is both the join gate and the rendezvous key; peers in
different rooms never see each other.

### VPS super-peer + NAT'd desktops (relay)

On a public box, run a super-peer that is both tracker and relay:

```bash
python3 peer.py --no-gui --serve-tracker --relay \
  --host 0.0.0.0 --port 8443 --public-url https://your.vps:8443 --room demo
```

A desktop behind NAT reaches the mesh *through* the relay — it advertises a relay
URL instead of its own (unreachable) port and drains inbound gossip via an HTTP
long-poll:

```bash
python3 peer.py --tracker https://your.vps:8443 --via-relay https://your.vps:8443 --room demo
```

Outbound from the desktop goes directly to reachable peers; inbound arrives via
the relay. The advertised address (`--public-url`) is always separate from the
bind address (`--host`/`--port`), so you can also expose a desktop with an
external tunnel (`ssh -R`, `frp`, `cloudflared`) by pointing `--public-url` at it.

### Caveats

- **TLS trust** pins the single dev cert (`/CN=127.0.0.1`), which only fits the
  localhost demo. A real VPS super-peer needs a cert valid for its domain
  (Let's Encrypt) or explicit per-host pinning.
- **`--room`** is a shared secret, not per-peer authentication.
- The **relay** is a single point of failure / bandwidth bottleneck for the peers
  that depend on it, and their traffic transits the VPS.
- Ordering is best-effort wall-clock; **Clear is local-only** (anti-entropy pull
  may re-populate it from peers); full-state pull is O(N).

## VS Code Tasks

- `Generate Dev Certificate`
- `Run Chat Backend`
- `Validate Backend Syntax`
- `Run Peer` — a single GUI peer on `:8443`
- `Run Super-Peer` — headless tracker + relay on `0.0.0.0:8443`

## Notes

- TODO This is a development scaffold. Messages are stored in memory.
- The client polls `GET /messages?format=tsv` and parses TSV directly.
- HTTPS trust pins the provided dev certificate via `ssl` (`cafile`). Hostname
  verification is disabled because the dev cert is CN-only (`/CN=127.0.0.1`) with
  no subjectAltName, which OpenSSL rejects for IP literals.

## License

Released into the public domain under [The Unlicense](LICENSE) — do whatever you
like with it, no attribution required.
