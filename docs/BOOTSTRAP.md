# Bootstrap from an empty environment

This guide provisions the application-owned PostgreSQL table and Qdrant collection required by
`sodnapraksa-ingest`. It is for a new deployment with no case-law state. It does not create source
or embedding-provider accounts and it does not perform an unbounded historical import.

The validated production combination for v0.1.6 is CPython 3.12.3, PostgreSQL 18.3, and Qdrant
1.17.1. Other maintained PostgreSQL and Qdrant releases may work, but must pass the same preflight
and controlled-run acceptance before unattended use.

## 1. Choose the starting mode

Choose one before creating a checkpoint:

- **Forward-only empty start:** select a reviewed recent UTC value for
  `SODNAPRAKSA_INITIAL_SINCE`. The first run ingests source modifications at or after that value.
- **Compatible restore:** restore PostgreSQL, Qdrant, and the checkpoint from one matching,
  operator-verified snapshot set.
- **Historical import:** plan a sequence of fixed UTC ends that remains within all discovery,
  detail, and embedding-byte limits. This repository intentionally has no unbounded backfill mode.

Do not set an arbitrarily old initial boundary and hope that safety limits will truncate the
result. Every limit fails closed; discovery is never silently truncated.

## Optional local infrastructure

For development and evaluation only, the tracked Compose file starts the exact PostgreSQL and
Qdrant versions used by the bootstrap CI. Both ports bind to loopback, the PostgreSQL password is
required explicitly, and named volumes preserve state:

```bash
export SODNAPRAKSA_DEV_POSTGRES_PASSWORD='<private-development-password>'
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml ps
```

Do not use this convenience stack as an unreviewed production architecture. Production requires
independent access control, TLS or private networking, backups, monitoring, capacity planning, and
restore tests.

## 2. PostgreSQL

Create a dedicated login and database. The following example uses local PostgreSQL administration;
adapt the authentication method for a managed service. `--pwprompt` avoids placing the password in
shell history or a process argument.

```bash
ingest_role=sodnapraksa_ingest
ingest_database=sodnapraksa

sudo -u postgres createuser --pwprompt "$ingest_role"
sudo -u postgres createdb --owner="$ingest_role" "$ingest_database"
sudo -u postgres psql \
  --dbname="$ingest_database" \
  --set=ON_ERROR_STOP=1 \
  --set=ingest_role="$ingest_role" \
  --file=deploy/postgres/bootstrap.sql
```

The bootstrap is intentionally one table, not a migration framework. It creates
`public.sodnapraksa_documents`, makes the dedicated login its owner, and stops on the first SQL
error. Run it only against an empty target; an existing target must be audited rather than
overwritten or patched implicitly.

Verify the resulting owner, primary key, columns, and types:

```bash
sudo -u postgres psql --dbname="$ingest_database" \
  --command='\d+ public.sodnapraksa_documents'
```

For the optional Compose stack, the login and database already exist. Apply the same schema through
the container instead:

```bash
docker compose -f deploy/compose.yaml exec -T postgres \
  psql --username=sodnapraksa_ingest --dbname=sodnapraksa \
    --set=ON_ERROR_STOP=1 --set=ingest_role=sodnapraksa_ingest \
  < deploy/postgres/bootstrap.sql
```

Use a PostgreSQL DSN for that login and database in the private application environment. The DSN
database must exactly match `SODNAPRAKSA_EXPECTED_DATABASE`; the bootstrap schema uses `public`.

PostgreSQL is authoritative. Back it up before every deployment or controlled recovery and test
restoration together with the matching checkpoint and Qdrant state.

## 3. Qdrant

Create one empty collection with the established vector contract. The examples use a loopback
endpoint. For an authenticated target, point `qdrant_curl_config` to a root-owned mode-0600 curl
configuration containing the required `header = "api-key: ..."`; this keeps the key out of the
command line. Leave it as `/dev/null` only when the target requires no key.

```bash
qdrant_url=http://127.0.0.1:6333
qdrant_collection=sodnapraksa_current
qdrant_curl_config=/dev/null

curl --config "$qdrant_curl_config" --fail-with-body --silent --show-error \
  --request PUT "$qdrant_url/collections/$qdrant_collection" \
  --header 'content-type: application/json' \
  --data-binary '{"vectors":{"size":3072,"distance":"Cosine"}}'
```

Create the four payload indexes required by preflight and grouped reconciliation:

```bash
for payload_field in evidencna_stevilka chunk_id chunk_hash; do
  curl --config "$qdrant_curl_config" --fail-with-body --silent --show-error \
    --request PUT "$qdrant_url/collections/$qdrant_collection/index?wait=true" \
    --header 'content-type: application/json' \
    --data-binary "{\"field_name\":\"$payload_field\",\"field_schema\":\"keyword\"}"
done

curl --config "$qdrant_curl_config" --fail-with-body --silent --show-error \
  --request PUT "$qdrant_url/collections/$qdrant_collection/index?wait=true" \
  --header 'content-type: application/json' \
  --data-binary '{"field_name":"chunk_index","field_schema":"integer"}'
```

Verify health, distance, dimension, and payload-index types without retrieving vectors or source
content:

```bash
curl --config "$qdrant_curl_config" --fail-with-body --silent --show-error \
  "$qdrant_url/collections/$qdrant_collection" |
  jq '.result | {status, optimizer_status, vectors: .config.params.vectors,
      payload_schema: (.payload_schema | with_entries(.value = .value.data_type))}'
```

Expected values are `green`, optimizer `ok`, vector size `3072`, distance `Cosine`, three keyword
indexes, and one integer index. Qdrant is derived state; do not treat it as the document authority.

## 4. Application configuration

Copy [`.env.example`](../.env.example) to a private file and replace the remaining placeholders:

```bash
install -m 600 .env.example .env.local
${EDITOR:-vi} .env.local
set -a
. ./.env.local
set +a
```

For a local preflight, override the production state paths with an ignored writable directory:

```bash
mkdir -p state
export SODNAPRAKSA_CHECKPOINT_PATH="$PWD/state/checkpoint.json"
export SODNAPRAKSA_LOCK_PATH="$PWD/state/ingest.lock"
```

Keep both authorization flags at `0`, then run:

```bash
uv sync --locked --no-dev
uv run sodnapraksa-ingest preflight
```

Preflight must report the intended database/schema and `sodnapraksa_current` with 3,072-dimensional
Cosine vectors. It does not contact the source or embedding provider and performs no data write.

## 5. First bounded run

Record the exact commit, configuration fingerprint with secrets redacted, initial boundary, target
end, and baseline counts. Set the two authorization flags to `1` only for the reviewed mutating
invocation:

```bash
run_end_utc=2026-08-18T00:00:00Z  # replace with a reviewed end later than the checkpoint
SODNAPRAKSA_ALLOW_EXTERNAL_API=1 \
SODNAPRAKSA_ALLOW_WRITES=1 \
uv run sodnapraksa-ingest run --end "$run_end_utc"
```

The example date is notation, not a recommended starting point. `--end` must be timezone-aware,
not in the future, and strictly newer than the checkpoint. If a discovery, detail, or embedding
budget is exceeded, the interval fails before its prohibited downstream side effects. Choose an
earlier fixed end or prepare a separately reviewed recovery plan; do not raise persistent limits
blindly.

After success:

1. rerun read-only preflight;
2. verify PostgreSQL has only complete `STORED`, non-deleted rows;
3. verify Qdrant is green and its point/payload contract matches PostgreSQL-derived chunks;
4. verify the checkpoint advanced to the exact selected end and no temporary sibling remains;
5. run a **strictly newer** small interval so the configured overlap rechecks the prior work; and
6. require zero repeated detail, embedding, PostgreSQL, or Qdrant work for unchanged overlap rows.

The CLI deliberately rejects `end <= checkpoint`; an exact same-end command is not an idempotency
test. Idempotency is demonstrated by a later interval replaying the overlap.

## 6. Move to unattended operation

Continue with [`DEPLOYMENT.md`](DEPLOYMENT.md). Do not enable the timer until the versioned runtime,
read-only preflight, supervised service run, post-run consistency check, and timer-driven acceptance
all pass.
