# Joining seedhunter and rpc_service

They are already one project. `rpc_service/` sits beside `seedhunter/` and is
reached through four seams, none of which require editing a seedhunter file:

| Seam | Where | Edits to seedhunter |
|---|---|---|
| Data in | `bridge.py` opens `data/hits.db` with `mode=ro` | none |
| Data out | `--mirror-hits` calls the real `Store.add()` | none |
| Alerts | `bridge.notifier()` returns the real `Notifier` | none |
| Process | `console.py` runs `python -m seedhunter.cli bot` as a child | none |

`SH_ATTACH=1` additionally makes CPython import `sitecustomize.py` at startup,
so the add-on loads inside the existing process without `cli.py` knowing.

## Two bots, two tokens

- **Command bot** — `seedhunter/bot.py`, untouched, `/start /stop /stats /hits
  /scan /family /set /show /clear /pull /restart`.
- **Mini App bot** — `rpc_service/dashboard_bot.py`, `/start /dashboard /status`,
  opens the dashboard.

They must use **different** tokens. Two pollers on one token get HTTP 409 and
both bots go deaf. `console doctor` fails on a shared token.

## Run it from the phone

Actions → **brutal** → Run workflow. That is the whole procedure.

1. `doctor` job gates everything. If it fails, nothing else starts.
2. `engine` job runs the detector on your chosen shard.
3. `dash` job starts the API, tunnels it with cloudflared, points the bot's menu
   button at the live URL, and runs the command bot.

## One-time setup

1. @BotFather → new bot → `SH_TG_DASH_BOT_TOKEN`. Keep `TELEGRAM_BOT_TOKEN` for
   the command bot.
2. Repo → Settings → Secrets → add the `SH_*` names from `rpc_service/.env.example`
   (deposits, admin token, KMS keys). Same place you already keep `RPC_*`.
3. Settings → Actions → General → Workflow permissions → read. Nothing needs
   write access.

## Shard discipline

`vulture.yml` already hunts shards 0–4 every 6h. `brutal.yml` scans the same key
space with a different cursor, so running both wastes half your RPC budget and
double-notifies.

Pick one:

- **Pause vulture** (Actions → vulture → ⋯ → Disable). Run `brutal` on shard 0,
  `shards` 1. Cleanest.
- **Split** — vulture on 0–4, `brutal` on shard 5, `shards` 8. No overlap, but
  `vulture.yml` only runs 4 shards of its 5 configured, so expect gaps.

## What the job cannot do

An Actions runner is not a server. When `dash` ends, the tunnel URL dies and the
Mini App goes dark until the next run. That is fine for live monitoring and for
testing the Mini App. For a permanently live dashboard you must deploy
`rpc_service/Dockerfile` to a host with a fixed HTTPS origin and set
`SH_PUBLIC_BASE_URL` — that is an account action, not a file edit.

## What "brutal" actually buys

Not smarter cryptography — throughput. Batching (120–250 addresses per HTTP call
instead of one), tokens only on funded addresses, derivation pipelined ahead of
checking, adaptive batch shrink on 429. Threshold divisors on `config.yaml`'s
`min_native` decide how much dust counts as a hit: `brutal` ÷4, `insane` ÷10.

Set `SH_COST_PER_MILLION` to your provider's real rate or the pre-flight prints
"unset" rather than inventing a number. Eight chains at 96 workers for six hours
will eat quota; `insane` more so.