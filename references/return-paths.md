# Return paths

How results get back from the gateway Bot to the caller is a **return path**.
It is chosen on the caller with `GROKGW_RETURN` and sent to the host in each
request as a `return` descriptor. The host validates that descriptor against
its own allowlist (`host.json`) before it writes or sends anything, then hands
results back only through `scripts/gateway_deliver.py`.

**The default is `none`.** No mode is recommended here; pick whatever fits
your trust and network situation. Modes:

| Mode | Caller needs | Host needs | Exposure | Can list/read/ask | Main risk |
|---|---|---|---|---|---|
| `none` | nothing | nothing | none | no | You never see any result. |
| `tailnet` | join the same tailnet | join the same tailnet; run `outbox_server.py` | tailnet only | yes | Any device on the tailnet can reach the outbox port. |
| `tunnel` | the public URL + bearer token | run `outbox_server.py` plus a public tunnel; set a token | public internet | yes | The port is public; request ids + token are the only protection. |
| `github` | read-only token for one repo | a repo allowlist and a contents-write token | repo ACLs | yes | Results (incl. transcripts) become permanent git history. |
| `callback` | a public HTTPS receiver | a callback host allowlist | caller endpoint is public | yes | The caller's endpoint is exposed; each request is HMAC-signed. |

Only `ping` and `send` (without `--wait-reply`) work in `none` mode. `list`,
`read`, `ask`, `fetch` and `send --wait-reply` exit 7 asking for a return path.

The request body carries the descriptor, for example:

```json
{"v":1,"request_id":"<uuid4>","op":"ping","return":{"mode":"tailnet"}}
```

An absent `return` means an old v0.2 client; the host decides with
`legacy_no_return` whether to accept it and as what.

---

## `none` — fire-and-forget (default)

- **Caller:** nothing. `GROKGW_RETURN=none` (or unset). `grokgw ping` prints
  `{"request_id":...,"status":"started","return":"none"}` and never waits.
- **Host:** nothing; the descriptor is accepted (it is in `allowed_returns` by
  default) but no result is delivered. `gateway_deliver.py result` prints
  `{"delivered":false,"mode":"none"}`.
- **No** `host.json` section is needed.
- **Security:** nothing is exposed. The trade-off is that you cannot read
  anything back through the gateway; set one of the other modes, or read the
  gateway Bot's chat yourself.

## `tailnet` — shared Tailscale tailnet

- **Caller:** join the same tailnet as the gateway computer. Set
  `GROKGW_RETURN=tailnet` and `GROKGW_OUTBOX=http://<gateway-host>:8787`.
  Prefer the gateway computer's MagicDNS name (`<name>.<tailnet>.ts.net`) over
  its tailnet IP: the IP changes whenever the host re-registers. Optional `GROKGW_OUTBOX_HOST` if a proxy in
  front routes by host name.
- **Host:** join the same tailnet and run `scripts/outbox_server.py` (via
  `scripts/start.sh`), which serves the outbox directory read-only.
  `gateway_deliver.py` writes the result files into that directory.
- **Keep the host's tailnet identity across resets.** A Bot's computer can be
  restarted, moved or reset; programs stop and only some directories survive
  (on Grok Bot, the home directory and `/workspace` survived a reset that wiped
  `/var/lib`). Tailscale's device identity lives in its state file, by default
  under `/var/lib/tailscale`. If that file is lost the computer joins as a new
  device with a new IP, and if the old offline device still holds the name the
  new one gets a `-1` suffix. To avoid this:
  - start `tailscaled` with `--statedir`/`--state` pointing into a directory
    that survives (readable by root only);
  - give the device a name you control (`tailscale set --hostname=<name>`);
  - optionally disable key expiry for that device in the Tailscale admin
    console;
  - re-run `scripts/start.sh` (and `tailscaled`) after every restart, since
    nothing on the Bot's computer autostarts. A scheduled routine that checks
    the port and restarts it works well.
- **host.json:**
  ```json
  {"allowed_returns": ["none", "tailnet"],
   "outbox_dir": "/workspace/gateway/outbox",
   "legacy_no_return": "tailnet"}
  ```
- Sends `{"mode":"tailnet"}`; the host writes
  `<outbox_dir>/<rid>.json` and `<rid>.reply.json`.
- **Security:** results never leave the tailnet. A tailnet can include devices
  shared from other users, and the outbox has no listing and unguessable ids,
  but check your tailnet ACLs if you want only your devices to reach the port.
  No public exposure.

## `tunnel` — outbox behind a public tunnel

- **Caller:** set `GROKGW_RETURN=tunnel`, `GROKGW_OUTBOX=https://<public-host>`
  (must be `https://`) and a bearer token in `GROKGW_OUTBOX_TOKEN` or
  `GROKGW_OUTBOX_TOKEN_CMD`. The token is passed as
  `Authorization: Bearer` on every outbox GET (via stdin, never in `ps`).
- **Host:** run `outbox_server.py` with `GROKGW_OUTBOX_TOKEN` (or
  `GROKGW_OUTBOX_TOKEN_FILE`) set, and put a tunnel in front of the loopback
  port yourself, e.g. `cloudflared tunnel --url http://127.0.0.1:8787`.
- **host.json:**
  ```json
  {"allowed_returns": ["none", "tunnel"],
   "outbox_dir": "/workspace/gateway/outbox"}
  ```
- Sends `{"mode":"tunnel"}`; the host writes the outbox files exactly as for
  `tailnet`.
- **Security:** the outbox is on the public internet. Request ids and the
  bearer token are the only protection; rotate the token and keep it out of
  logs. Every request is rejected with 401 when a token is configured and it
  is missing or wrong.

## `github` — results committed to a repo

- **Caller:** set `GROKGW_RETURN=github` and `GROKGW_GITHUB_REPO=owner/name`,
  plus a **read-only** token in `GROKGW_GITHUB_TOKEN` /
  `GROKGW_GITHUB_TOKEN_CMD`. Optional `GROKGW_GITHUB_PATH` (default `outbox`),
  `GROKGW_GITHUB_REF` (default `main`), `GROKGW_GITHUB_API` (default
  `https://api.github.com`). The client polls
  `GET /repos/<repo>/contents/<path>/<file>?ref=<ref>` with
  `Accept: application/vnd.github.raw`; 404 means "not ready yet".
- **Host:** allowlist the repo and provide a fine-grained token file:
  ```json
  {"allowed_returns": ["none", "github"],
   "github": {
     "repos": {"owner/gateway-results": {"path": "outbox", "branch": "main"}},
     "token_file": "/workspace/gateway/github-token"}}
  ```
  The host commits with a PUT to the Contents API; the token is read from the
  file and never printed.
- Sends `{"mode":"github","repo":"owner/name"}`.
- **Security:** use a **dedicated private repo**: results, including
  transcripts, become permanent git history, and removing them later means
  rewriting history (or deleting and recreating the repo). Give the host a
  fine-grained token scoped to that ONE repo with Contents read/write, and
  give callers a read-only token for the same repo. The gateway Bot can read
  the token file on its own computer, so the token's scope is the real limit of
  what it could touch.

## `callback` — the caller runs a receiver

- **Caller:** set `GROKGW_RETURN=callback` and `GROKGW_CALLBACK_URL` to the
  public `https://` URL of your receiver. Run
  `scripts/callback_receiver.py` behind your own HTTPS reverse proxy/tunnel,
  on the same machine as the client (they share `GROKGW_CALLBACK_DIR`).
  For each request the client writes a fresh 32-byte secret to
  `$GROKGW_CALLBACK_DIR/<rid>.secret` (default under `GROKGW_STATE_DIR`); the
  receiver verifies signatures with it and writes `<rid>.json` /
  `<rid>.reply.json`, which the client polls.
- **Host:** allowlist the callback hostname:
  ```json
  {"allowed_returns": ["none", "callback"],
   "callback": {"hosts": ["hooks.example.com"]}}
  ```
- Sends `{"mode":"callback","url":"https://...","secret":"<64 hex>"}`. The
  host POSTs to `<url>/<rid>/result` (or `/reply`) with
  `X-Grokgw-Request-Id`, `X-Grokgw-Kind`, `X-Grokgw-Timestamp` and
  `X-Grokgw-Signature: sha256=HMAC-SHA256(secret, timestamp + "." + body)`.
  No redirects are followed; the timeout is 15 s.
- **Security:** the caller must have a public HTTPS endpoint. The host only
  talks to hostnames on its allowlist (exact match, no wildcards, no IP
  literals, port 443 only). Each request carries a per-request HMAC secret, so
  a captured body cannot be replayed into a different request. The receiver is
  first-write-wins (a replay returns 409), rejects bad/missing signatures with
  401 and unknown request ids with 404. The secret travels inside the request
  body, so it is visible in the gateway Bot's run (and its transcript); it only
  protects against third parties forging results, not against the host.

---

## Why the allowlist matters

A caller supplies the `return` descriptor, but a caller must not be able to
make the gateway Bot send transcripts to an arbitrary URL or repo. The host's
`allowed_returns`, `github.repos` and `callback.hosts` are an **allowlist**: a
descriptor outside it is rejected (`return_not_allowed`) before the Bot does
anything. The Bot is instructed (in its persona and routine) to hand results
back **only** by calling `scripts/gateway_deliver.py`, and to never contact a
URL or repo itself. The allowlist check is code; whether the Bot routes every
delivery through that code is prompt-enforced. Keep the allowlists as small as
possible and scope tokens so that a Bot ignoring its instructions still has
little it can reach.
