# Production deployment and nightly operation

This guide installs an immutable candidate, keeps a rollback runtime, runs the tracked hardened
systemd unit, and proves the timer path. It assumes the infrastructure and first-run contract in
[`BOOTSTRAP.md`](BOOTSTRAP.md) already pass.

The commands are examples for a systemd Linux host. Review every path, identity, artifact hash,
backup, and local hardening rule before execution.

## Layout

```text
/opt/sodnapraksa-ingest/releases/<commit>/   immutable source release
/opt/sodnapraksa-ingest/runtime/             shared CPython 3.12.3 outside user homes
/opt/sodnapraksa-ingest/.venv.<short>-opt/   immutable release environment
/opt/sodnapraksa-ingest/.venv                active symlink
/etc/sodnapraksa-ingest.env                  root-owned secrets, mode 0600
/var/lib/sodnapraksa-ingest/                 checkpoint, mode 0700
/run/sodnapraksa-ingest/                     lock, mode 0700
```

The virtual environment's Python must resolve beneath `/opt/sodnapraksa-ingest/runtime`. A symlink
into an operator's home directory is incompatible with `ProtectHome=true` and the service account.

## 1. Verify the candidate

Use a fresh clone and an exact signed-off ref. For the current public production-compatible build:

```bash
git clone https://github.com/OpenLegalCore/slovenia-sodnapraksa-ingest.git
cd slovenia-sodnapraksa-ingest
release_ref=v0.1.7
git checkout --detach "$release_ref"
release_commit="$(git rev-parse HEAD)"
git diff --exit-code
git status --porcelain

uv python install 3.12.3
uv sync --locked --extra dev
uv lock --check
uv run --extra dev ruff format --check .
uv run --extra dev ruff check .
uv run --extra dev pytest

build_output="$(mktemp -d)"
uv build --out-dir "$build_output"
sha256sum "$build_output"/*
```

Compare the commit, tree, filenames, sizes, hashes, version, BUSL metadata, LICENSE, and manifests
with the approved release record. Never deploy a stale `dist/` directory.

Version 0.1.7 changes documentation, release metadata, and the repository Python pin only. Its
ingestion logic is identical to the production-accepted v0.1.6 implementation.

## 2. Install the shared interpreter and immutable environment

Create a root-owned Python installation outside `/home`. `uv python find` returns the exact
interpreter installed in that directory.

```bash
sudo install -d -o root -g root -m 0755 \
  /opt/sodnapraksa-ingest/runtime \
  /opt/sodnapraksa-ingest/releases \
  /opt/sodnapraksa-ingest/cache

sudo env UV_PYTHON_INSTALL_DIR=/opt/sodnapraksa-ingest/runtime \
  uv python install --no-bin 3.12.3

shared_python="$(sudo env UV_PYTHON_INSTALL_DIR=/opt/sodnapraksa-ingest/runtime \
  uv python find 3.12.3)"
release_dir="/opt/sodnapraksa-ingest/releases/$release_commit"
release_short="$(git rev-parse --short=16 "$release_commit")"
candidate_venv="/opt/sodnapraksa-ingest/.venv.$release_short-opt"

sudo install -d -o root -g root -m 0755 "$release_dir"
git archive "$release_commit" | sudo tar -x -C "$release_dir"

sudo env \
  UV_PROJECT_ENVIRONMENT="$candidate_venv" \
  UV_CACHE_DIR=/opt/sodnapraksa-ingest/cache \
  UV_LINK_MODE=copy \
  uv --directory "$release_dir" sync \
    --locked --no-dev --no-editable --python "$shared_python"
```

`UV_LINK_MODE=copy` prevents the environment from depending on later cache cleanup. Validate before
activation:

```bash
readlink -f "$candidate_venv/bin/python"
sudo "$candidate_venv/bin/python" -c \
  'from importlib.metadata import version; import sodnapraksa_ingest as p; print(version("sodnapraksa-ingest"), p.__version__)'
sudo "$candidate_venv/bin/sodnapraksa-ingest" --help
```

Stop if the interpreter resolves outside `/opt/sodnapraksa-ingest/runtime`, versions differ, the
CLI exposes anything other than `preflight` and `run`, or a module differs from the approved tree.

## 3. Install the private environment and units

Create the dedicated account once, using the local non-login policy:

```bash
sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin \
  --user-group sodnapraksa-ingest
```

If it already exists, inspect it instead of recreating or modifying it blindly.

Install a reviewed private environment and the tracked units:

```bash
sudo install -o root -g root -m 0600 .env.example /etc/sodnapraksa-ingest.env
sudoedit /etc/sodnapraksa-ingest.env
sudo install -o root -g root -m 0644 \
  deploy/systemd/sodnapraksa-ingest.service \
  deploy/systemd/sodnapraksa-ingest.timer \
  /etc/systemd/system/
sudo systemd-analyze verify \
  /etc/systemd/system/sodnapraksa-ingest.service \
  /etc/systemd/system/sodnapraksa-ingest.timer
sudo systemctl daemon-reload
```

Keep the persistent limits and the two authorization flags at their reviewed values. The service
template overrides both flags after `EnvironmentFile` loading: read-only values for `ExecStartPre`,
and mutating values only for `ExecStart`.

## 4. Atomic cutover and read-only preflight

Keep the timer stopped while changing the runtime. Preserve the old symlink target as the immediate
code rollback candidate.

```bash
sudo systemctl stop sodnapraksa-ingest.timer
previous_venv="$(readlink -f /opt/sodnapraksa-ingest/.venv 2>/dev/null || true)"
sudo ln -s "$candidate_venv" /opt/sodnapraksa-ingest/.venv.next
sudo mv -Tf /opt/sodnapraksa-ingest/.venv.next /opt/sodnapraksa-ingest/.venv
```

Run the application as the service identity with the same environment and runtime directory as the
installed unit. `/usr/bin/env` comes after `EnvironmentFile`, so the no-write flags take precedence:

```bash
sudo systemd-run --wait --collect --pipe \
  --unit=sodnapraksa-ingest-preflight \
  --property=Type=oneshot \
  --property=User=sodnapraksa-ingest \
  --property=Group=sodnapraksa-ingest \
  --property=WorkingDirectory=/opt/sodnapraksa-ingest \
  --property=EnvironmentFile=/etc/sodnapraksa-ingest.env \
  --property=RuntimeDirectory=sodnapraksa-ingest \
  --property=RuntimeDirectoryMode=0700 \
  /usr/bin/env \
    SODNAPRAKSA_ALLOW_EXTERNAL_API=0 \
    SODNAPRAKSA_ALLOW_WRITES=0 \
    /opt/sodnapraksa-ingest/.venv/bin/sodnapraksa-ingest preflight
```

This preflight must exit `0` without source, embedding, PostgreSQL, Qdrant, or checkpoint writes.

## 5. Supervised service and timer acceptance

Back up PostgreSQL, Qdrant, and the checkpoint. Then run the **actual installed unit**, not a
different CLI environment:

```bash
sudo systemctl start sodnapraksa-ingest.service
sudo systemctl status sodnapraksa-ingest.service --no-pager
sudo journalctl -u sodnapraksa-ingest.service --since today --no-pager
```

Require exit `0`, checkpoint-last ordering, healthy Qdrant, PostgreSQL/Qdrant convergence, no held
lock, and no unexplained source or embedding usage.

To prove timer-driven activation immediately instead of waiting until 04:30, schedule one transient
timer that starts the unchanged installed service:

```bash
sudo systemd-run --unit=sodnapraksa-ingest-acceptance \
  --on-active=30s --timer-property=AccuracySec=1s --collect \
  /usr/bin/systemctl start sodnapraksa-ingest.service
sudo systemctl list-timers sodnapraksa-ingest-acceptance.timer
sudo journalctl -u sodnapraksa-ingest-acceptance.service \
  -u sodnapraksa-ingest.service --since today --no-pager
```

The transient timer proves a timer can start and wait for the real installed service; it does not
change the tracked daily schedule. Require the same post-run audit, then enable the real timer:

```bash
sudo systemctl enable --now sodnapraksa-ingest.timer
sudo systemctl list-timers sodnapraksa-ingest.timer
```

Because the timer uses `Persistent=true`, enabling it after a missed 04:30 trigger can immediately
start one catch-up invocation. Treat that invocation as real production work and audit it before
leaving the timer unattended.

## 6. Routine checks

```bash
systemctl is-enabled sodnapraksa-ingest.timer
systemctl is-active sodnapraksa-ingest.timer
systemctl show sodnapraksa-ingest.service -p Result -p ExecMainStatus
journalctl -u sodnapraksa-ingest.service --since today --no-pager
```

After every run verify the JSON summary, checkpoint, service result, timer's next trigger, Qdrant
health, and absence of a live lock. A later successful interval should reconstruct unchanged
overlap candidates from PostgreSQL and perform no repeated detail, embedding, or store work.

## 7. Rollback and recovery

A code rollback is an explicit symlink cutover, never an automatic data rollback:

1. stop the timer;
2. preserve the failed service journal and current checkpoint;
3. verify the prior immutable runtime and its matching configuration contract;
4. atomically point `.venv` to `previous_venv`;
5. run the read-only preflight again; and
6. resume only under a reviewed data-recovery plan.

Do not move the checkpoint backward or restore PostgreSQL alone. PostgreSQL, Qdrant, and checkpoint
recovery must use one consistent point-in-time set. Retain at least the active and immediately prior
versioned runtimes until the new scheduled path has passed.
