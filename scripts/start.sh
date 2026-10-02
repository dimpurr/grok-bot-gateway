#!/usr/bin/env bash
# Idempotent: (re)start the outbox server if it is not healthy and clean old files.
# The server binds 127.0.0.1 and this node's tailnet IPv4 directly (tailscale0), so
# both http://<tailnet-IP>:PORT and http://<magicdns-name>:PORT work. We do NOT use
# `tailscale serve`: it routes by Host header and 404s requests addressed to the IP.
# Any old serve mapping on PORT is removed (it would hold the port).
#   start.sh            start if needed
#   start.sh --restart  force a server restart
#   start.sh --stop     stop the server
# Environment (all optional):
#   GROKGW_HOME        state directory (default /workspace/gateway); holds outbox/ and logs/
#   GROKGW_OUTBOX_DIR  outbox directory (default $GROKGW_HOME/outbox); must match the
#                      <OUTBOX_DIR> in the routine's saved instruction
#   GROKGW_PORT        port (default 8787)
# Without Tailscale (no tailnet IPv4) it serves loopback only.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SERVER="$HERE/outbox_server.py"
GW="${GROKGW_HOME:-/workspace/gateway}"
OUTBOX_DIR="${GROKGW_OUTBOX_DIR:-$GW/outbox}"
PORT="${GROKGW_PORT:-8787}"
PIDF="$GW/logs/outbox_server.pid"
LOG="$GW/logs/outbox_server.log"
mkdir -p "$OUTBOX_DIR" "$GW/logs"; chmod 700 "$OUTBOX_DIR"

ts() { if [ "$(id -u)" = 0 ]; then tailscale "$@"; else tailscale "$@" 2>/dev/null || sudo -n tailscale "$@"; fi; }
TSIP=$(tailscale ip -4 2>/dev/null | head -1)
healthy() { # $1 = address; a valid-looking but absent id must give 404 from *our* server.
  code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' -D - "http://${1:-127.0.0.1}:$PORT/00000000-0000-4000-8000-000000000000.json" 2>/dev/null | tr -d '\r' | awk '/^[Ss]erver: outbox/{s=1} END{print s?"ok":"no"}')
  [ "$code" = ok ]
}
stop_server() {
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then kill "$(cat "$PIDF")"; sleep 0.5; fi
  pkill -f "$SERVER" 2>/dev/null || true
  rm -f "$PIDF"
}

case "${1:-}" in
  --stop) stop_server; echo "stopped"; exit 0 ;;
  --restart) stop_server ;;
esac

# one-shot cleanup (the server also cleans every 10 min)
find "$OUTBOX_DIR" -maxdepth 1 -type f \( -name '*.json' -o -name '*.part' \) -mmin +1440 -delete 2>/dev/null || true

# drop a leftover `tailscale serve` mapping on this port (it would hold the tailnet port)
if ts serve status 2>/dev/null | grep -q ":$PORT"; then ts serve --http="$PORT" off >/dev/null 2>&1 && echo "serve: removed old tailscale serve mapping on :$PORT"; fi

BIND="127.0.0.1${TSIP:+,$TSIP}"
if healthy && { [ -z "$TSIP" ] || healthy "$TSIP"; }; then
  echo "server: already healthy on 127.0.0.1:$PORT"
else
  stop_server
  GROKGW_BIND="$BIND" GROKGW_PORT="$PORT" GROKGW_OUTBOX_DIR="$OUTBOX_DIR" setsid nohup python3 "$SERVER" >>"$LOG" 2>&1 </dev/null 3>&- 4>&- 5>&- &
  echo $! >"$PIDF"
  for _ in 1 2 3 4 5 6 7 8 9 10; do healthy && break; sleep 0.3; done
  if healthy; then echo "server: started pid $(cat "$PIDF")"; else echo "server: FAILED to start; see $LOG" >&2; exit 1; fi
fi

# Defence in depth: the kernel accepts packets for the tailnet IP on any interface,
# so drop PORT unless it arrives on tailscale0 or loopback.
if [ -n "$TSIP" ] && sudo -n sh -c "command -v iptables" >/dev/null 2>&1; then
  # chain GROKGW-OUTBOX: allow tailscale0 and loopback, drop everything else
  if sudo -n iptables -N GROKGW-OUTBOX 2>/dev/null; then
    sudo -n iptables -A GROKGW-OUTBOX -i tailscale0 -j RETURN
    sudo -n iptables -A GROKGW-OUTBOX -i lo -j RETURN
    sudo -n iptables -A GROKGW-OUTBOX -j DROP
  fi
  JUMP=(INPUT -p tcp -d "$TSIP" --dport "$PORT" -j GROKGW-OUTBOX)
  if sudo -n iptables -C "${JUMP[@]}" 2>/dev/null; then echo "firewall: rule present"
  elif sudo -n iptables -I "${JUMP[@]}" 2>/dev/null; then echo "firewall: only tailscale0/lo may reach $TSIP:$PORT"
  else echo "firewall: could not add rule; relying on network isolation" >&2; fi
fi

if [ -n "$TSIP" ]; then
  if healthy "$TSIP"; then echo "tailnet: serving on http://$TSIP:$PORT"; else echo "tailnet: NOT reachable on $TSIP:$PORT" >&2; exit 2; fi
else
  echo "tailnet: no tailnet IPv4 (tailscale down?); serving loopback only" >&2
fi
