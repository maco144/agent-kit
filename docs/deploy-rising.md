# Deploying agent-kit Cloud on rising

The production-ish deployment that AIOS agents report to. Generic instructions live in
[self-hosting.md](self-hosting.md); this is the runbook for our box.

| | |
|---|---|
| Host | `rising` (45.77.104.159, also `eudaimonia.win`), user `linuxuser` |
| Path | `/opt/agentkit` (git clone of `maco144/agent-kit`) |
| Compose project | `agentkit` — services `db`, `api`, `worker` |
| API | `http://127.0.0.1:8020` (localhost only; Caddy fronts it once DNS exists) |
| Public hostname | `https://agentkit.eudaimonia.win` (live since 2026-09-16; Caddy block in `/etc/caddy/Caddyfile`) |
| Secrets | `/opt/agentkit/server/.env` (mode 600): Postgres password, `AGENTKIT_SIGNING_KEY` |
| AIOS key | `/opt/agentkit/server/aios-key.txt` (mode 600) — issued once, not recoverable |

## First deploy (done 2026-09-16)

```bash
sudo mkdir -p /opt/agentkit && sudo chown linuxuser:linuxuser /opt/agentkit
git clone https://github.com/maco144/agent-kit.git /opt/agentkit
cd /opt/agentkit/server
cp .env.example .env && chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(24))"                       # POSTGRES_PASSWORD
python3 -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"    # AGENTKIT_SIGNING_KEY
docker compose up -d --build
curl -fsS http://127.0.0.1:8020/healthz     # {"status":"ok"}
```

Org and key:

```bash
docker compose exec -T api agentkit-server create-org "Rising Sun"
docker compose exec -T api agentkit-server create-key <org-id> --name aios
```

## Pointing AIOS agents at it

Agents on the same box use localhost, so their telemetry never leaves the machine:

```bash
export AGENTKIT_BASE_URL=http://127.0.0.1:8020
export AGENTKIT_API_KEY=$(cat /opt/agentkit/server/aios-key.txt)
```

## TLS

Done on 2026-09-16 — A record to 45.77.104.159, then:

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)
printf '\nagentkit.eudaimonia.win {\n\treverse_proxy 127.0.0.1:8020\n}\n' | sudo tee -a /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
curl -fsS https://agentkit.eudaimonia.win/healthz     # {"status":"ok"}
```

If `caddy validate` fails, restore the backup and reload before investigating. Caddy issues and renews the
certificate automatically; the signing keys are public at
`https://agentkit.eudaimonia.win/.well-known/agentkit-signing-keys`.

## Operating

```bash
cd /opt/agentkit/server
docker compose ps                      # service state and health
docker compose logs -f worker          # alert/budget/retention cycles (60s)
docker compose logs --tail 50 api

git pull && docker compose up -d --build   # upgrade; the entrypoint migrates
docker compose exec db pg_dump -U agentkit agentkit > ~/agentkit-$(date +%F).sql   # backup
docker compose down                    # stop (the agentkit-db volume persists)
```

Checks worth running after any upgrade:

```bash
K=$(cat aios-key.txt)
curl -fsS http://127.0.0.1:8020/healthz
curl -fsS -H "Authorization: Bearer $K" http://127.0.0.1:8020/v1/metrics/summary
curl -fsS -H "Authorization: Bearer $K" http://127.0.0.1:8020/v1/audit/runs
```

## Notes

- The port (8020) was picked because 8000, 8001, 8010–8012, 8040 and 8101 are already used on this box.
- Exactly one `worker` container may run; the API sets `ENABLE_ALERT_WORKER=0`. More evaluators means
  duplicate alerts.
- The `agentkit-db` volume holds the audit chains that evidence bundles are built from. Back it up on the
  schedule you would want for auditor-facing records.
- Rolling back: `docker compose down`, `git checkout <previous tag>`, `docker compose up -d --build`. Database
  migrations are forward-only; take a `pg_dump` before upgrading if a downgrade might be needed.
