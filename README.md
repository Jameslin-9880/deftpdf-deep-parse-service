# DeftPDF Deep Parse Service

This repository contains the separate open-source service wrapper used by DeftPDF for the `Deep Parse` mode in `PDF to Markdown`.

The service boundary is intentional:

- the main DeftPDF web app calls this service over HTTP
- the parsing runtime is provisioned separately from the Laravel app
- the source for the deployed parser service is published here so the corresponding source offer can point to the exact running version

## Upstream parser

- Upstream project: [MinerU](https://github.com/opendatalab/MinerU)
- Upstream license: `GNU AGPL v3`
- Pinned package version for this service: `mineru[core]==3.0.9`

## What this service runs

This repo does not re-implement MinerU. It provides:

- a pinned Python environment for the parser runtime
- a recoverable FastAPI task wrapper around MinerU
- SQLite-backed task state that survives service restarts
- one isolated MinerU worker process with a hard per-task watchdog
- a systemd unit template for production deployment
- an environment template for the service configuration

## Runtime model

- Transport: local HTTP service
- Default bind: `127.0.0.1:18080`
- Intended caller: DeftPDF web app on the same host
- Intended backend: `pipeline` CPU mode unless you explicitly provision another backend
- Task database: `/var/lib/deftpdf-deep-parse/tasks.sqlite3`
- Default hard task timeout: `7200` seconds
- Default Linux runtime: CPU-only PyTorch `2.11.0` and torchvision `0.26.0`

`POST /tasks` persists the upload and task metadata before returning `202`.
`GET /tasks/{task_id}` and `GET /tasks/{task_id}/result` remain usable after
the API process restarts. If the service stops during parsing, the interrupted
task is returned to the queue and retried. If a task exceeds the hard timeout,
the entire MinerU worker process group is terminated before a fresh worker is
started.

## Local setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
bash scripts/start-service.sh
```

Health check:

```bash
curl http://127.0.0.1:18080/health
```

The health payload reports `persistent_tasks: true`, the worker PID, queue
counts, the task timeout, and the retention window.

## Production install outline

1. Create the service root, for example `/opt/deftpdf-deep-parse`
2. Create a dedicated service account, for example `useradd --system --create-home --home-dir /opt/deftpdf-deep-parse deftpdf`
3. Copy this repository to that path
4. `chown -R deftpdf:deftpdf /opt/deftpdf-deep-parse`
5. Create a Python virtualenv and install `requirements.txt`
6. Copy `.env.example` to `/etc/default/deftpdf-deep-parse` and adjust values
7. Create the state directories, for example `mkdir -p /var/lib/deftpdf-deep-parse/output && chown -R deftpdf:deftpdf /var/lib/deftpdf-deep-parse`
8. Install `systemd/deftpdf-deep-parse.service` to `/etc/systemd/system/`
9. `systemctl daemon-reload`
10. `systemctl enable --now deftpdf-deep-parse`
11. Confirm `curl http://127.0.0.1:18080/health` returns `200`

## Environment

See [.env.example](./.env.example).

## Tests

The control-plane tests do not load MinerU models:

```bash
python -m pytest -q
```

## Release deployment

Production releases are immutable directories below
`/opt/deftpdf-deep-parse/releases`, selected by the `current` symlink. Deploy
only a reviewed commit that is already merged to `main`:

```bash
SSH_KEY=/path/to/id_ed25519 \
  bash scripts/deploy-to-server.sh root@57.128.189.249 <merged-main-sha>
```

The script refuses a dirty checkout or a SHA mismatch, installs dependencies
into the shared virtualenv, atomically switches `current`, restarts systemd,
and rolls back automatically unless `/health` confirms persistent task mode.
Fresh Linux installs explicitly use PyTorch's CPU wheel index so a CPU parser
host never downloads CUDA runtime packages by accident.

## Notes

- Keep the service private to localhost unless you add authentication and a reverse proxy policy intentionally.
- Keep the DeftPDF web app configured with the corresponding source URL for the exact deployed revision of this repository.
