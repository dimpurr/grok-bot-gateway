# Grok Bot Gateway

Grok Bot's official external channel gateway, with a return path you choose.

## What it does

A caller can list all Bots, read any Bot's conversation with paging, message a Bot, and ping the gateway. The `ask` command sends a message and waits for a reply. Calls use one gateway Bot and its official webhook routine.

Claude Code, Codex, or scripts can call the gateway through `scripts/grokgw`. The caller needs bash and curl; return-path modes may have additional requirements. See [SKILL.md](SKILL.md) for setup and use.

## Why this gateway

- It uses only the official Grok Bot webhook; it does not use internal tokens or a desktop-app session.
- It exposes fixed operations, with JSON Schema contracts, host-side request validation and deduplication, and prompt-injection guards in the gateway Bot instructions.
- The client has a kill switch and a configurable hourly cost cap.
- There is no return path by default. `send` and `ping` are fire-and-forget; getting results back is optional.

## Return paths

The default is `none`, which does not return results. Choose a return path only if you need to read results. The four result-return options have these trade-offs:

| Mode | Caller and host requirements | Exposure and trade-off |
|---|---|---|
| `github` | Caller needs a read-only token for one repo. Host needs a repo allowlist and contents-write token. | Repo access controls visibility, but results, including transcripts, become permanent git history. |
| `callback` | Caller runs a public HTTPS receiver; host allowlists its hostname. | The caller endpoint is public. Each request is HMAC-signed. |
| `tunnel` | Host runs the outbox server behind a public tunnel and configures a bearer token; caller needs the URL and token. | The outbox is on the public internet; request IDs and the bearer token are its protection. |
| `tailnet` | Caller and host join the same tailnet; host runs the outbox server. | No public exposure, but any device on the tailnet can reach the outbox port. |

Details and configuration are in [references/return-paths.md](references/return-paths.md).

## Install

Install the skill and its client files with:

```sh
npx skills add dimpurr/grok-bot-gateway
```

Claude Code and Cursor plugin manifests are present in this repository. Plugin installation has not been verified here.

## Quick start

### Caller

Install `scripts/grokgw` on your `PATH`, configure `GROKGW_WEBHOOK_URL` and either `GROKGW_WEBHOOK_KEY` or `GROKGW_WEBHOOK_KEY_CMD`, then run:

```sh
grokgw doctor
grokgw ping
grokgw list
grokgw read <agent-id> --limit 20
grokgw send <agent-id> "message"
grokgw ask <agent-id> "question"
```

`list`, `read`, and `ask` need a return path. `none` supports fire-and-forget `ping` and `send`. See the Caller path in [SKILL.md](SKILL.md) for credentials, configuration, paging, and return-path setup.

### Host

Create a gateway Bot and a webhook routine, copy the host scripts to its computer, and configure `host.json`. For `tailnet` or `tunnel`, also run `scripts/start.sh`. See the Host path in [SKILL.md](SKILL.md) for the routine instructions, allowlists, and deployment steps.

## Limitations

- Each request runs the Bot and takes tens of seconds to minutes; every call spends the account's Grok Bot usage.
- `read` is handed from the routine run to the gateway Bot itself, so it is the slowest operation (minutes) and needs that Bot to be reachable.
- `ask` and `send --wait-reply` relay with priority so the target Bot wakes now; plain `send` is read on the target's next turn.
- The hourly cap applies only on each client machine. One webhook key grants access to every Bot and has no per-caller scopes.
- Host-side operation and prompt-injection checks depend on the gateway Bot following its persona and routine instructions. The delivery script enforces the return allowlist when used, but a misbehaving Bot could try to contact a destination itself.
- Setup requires creating the Bot and routine and configuring the host; `tailnet` and `tunnel` also require an outbox server.
- The Bot's computer can restart or be reset, so the outbox server must be restarted afterwards; for `tailnet`, use a MagicDNS name and keep Tailscale's state in a directory that survives (see [return paths](references/return-paths.md)).
- Relayed replies depend on the target Bot replying and the gateway writing the reply file; verified end to end once, and waits can still time out.
- Transcript entry fields are passed through and may change.
- Use through Cursor must follow Cursor's terms.

## License

[Apache-2.0](LICENSE). Copyright 2026 dimpurr <dimpurr@live.com>.
