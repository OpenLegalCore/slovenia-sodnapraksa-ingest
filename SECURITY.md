# Security policy

## Supported version

Security fixes are prepared for the current `main` branch and the latest public release tag.
Older snapshots are not maintained unless a separate support agreement says otherwise.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** control in this repository's Security tab. It creates a
private security advisory visible only to the reporter and repository maintainers.

Do not open a public issue, pull request, or discussion for a suspected vulnerability. Do not
include source credentials, DSNs, private endpoints, source records, court-document bodies,
personal data, embedding input, checkpoints, database dumps, vectors, or unredacted logs.

Include only what maintainers need to reproduce the problem safely:

- affected version or commit;
- affected command or component;
- impact and required preconditions;
- a minimal synthetic reproduction; and
- suggested remediation, if known.

Maintainers will acknowledge a complete report, validate it, coordinate a fix and disclosure, and
credit the reporter if requested. Timelines depend on severity, reproducibility, third-party
coordination, and the applicable licensing or support relationship.

## Scope

In scope are the application code, packaged artifacts, tracked deployment templates, dependency
lock, authorization gates, checkpoint/lock behavior, deterministic identities, PostgreSQL and
Qdrant integrity contracts, and accidental disclosure through this repository.

Source-portal availability or policy, embedding-provider service, operator-managed PostgreSQL or
Qdrant infrastructure, leaked operator credentials, and downstream applications are outside this
repository's direct control. Reports that demonstrate an application-level weakness at those
boundaries are still welcome through the private channel.
