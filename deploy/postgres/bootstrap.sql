\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE public.sodnapraksa_documents (
    evidencna_stevilka text PRIMARY KEY,
    unid text NOT NULL,
    internal_id text NOT NULL,
    database_name text NOT NULL,
    title text NOT NULL DEFAULT '',
    url text NOT NULL DEFAULT '',
    feed_url text NOT NULL DEFAULT '',
    sodisce text NOT NULL DEFAULT '',
    oddelek text NOT NULL DEFAULT '',
    ecli text NOT NULL DEFAULT '',
    datum_odlocbe timestamptz,
    datum_nastanka timestamptz,
    datum_zadnje_spremembe timestamptz NOT NULL,
    content_hash text NOT NULL,
    status text NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    error text,
    raw_api jsonb NOT NULL,
    normalized jsonb NOT NULL,
    discovered_at timestamptz NOT NULL DEFAULT now(),
    scraped_at timestamptz NOT NULL DEFAULT now(),
    checked_at timestamptz NOT NULL DEFAULT now(),
    stored_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.sodnapraksa_documents OWNER TO :"ingest_role";

COMMIT;
