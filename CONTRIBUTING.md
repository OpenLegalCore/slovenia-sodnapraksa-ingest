# Contributing

Thank you for your interest in `sodnapraksa-ingest`.

The project uses a source-available commercial licensing model that requires the
Licensor to retain the rights needed for consistent commercial licensing. For now,
external code, documentation, and other copyrightable contributions are not accepted
unless the contributor has first signed a contributor license agreement (CLA) approved
by the Licensor. Unsolicited pull requests may therefore be closed without review.

Opening an issue to report a reproducible defect or discuss a proposal does not transfer
copyright and does not guarantee that a contribution will be accepted. Do not include
secrets, source credentials, personal data, court-document content, or third-party
material that you are not authorized to submit.

## Before opening an issue

- Search existing issues and the troubleshooting section in `README.md`.
- Report the package version, operating system, command, exit code, and a minimal synthetic
  reproduction.
- Redact DSNs, API keys, private endpoints, source identities, source bodies, embedding input,
  checkpoints, and operational logs.
- Report suspected vulnerabilities through the private process in `SECURITY.md`, not a public
  issue.

## Approved development work

After the Licensor has approved the contribution and CLA, create a focused branch and keep the
change as small as the contract allows. Do not add production defaults, schema migrations,
dependencies, abstractions, or compatibility behavior without explicit review.

Run the complete local gate before requesting review:

```bash
uv sync --locked --extra dev
uv lock --check
uv run --extra dev ruff format --check .
uv run --extra dev ruff check .
uv run --extra dev pytest
build_output="$(mktemp -d)"
uv build --out-dir "$build_output"
```

Do not commit local environments, build output, source responses, database material, vectors,
logs, or credentials. A behavioral change also requires focused regression tests and a clear
description of its failure and checkpoint semantics.

For commercial licensing inquiries, contact sales@openlegalcore.org.
