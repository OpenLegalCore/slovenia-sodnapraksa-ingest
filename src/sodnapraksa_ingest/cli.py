"""Single command-line entrypoint for preflight and scheduled/manual runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from . import IngestError
from .config import ConfigError, load_settings
from .pipeline import acquire_lock, pg_preflight, qdrant_preflight, read_checkpoint, run_interval


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sodnapraksa-ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="validate configured targets without writes")
    run = commands.add_parser("run", help="run one incremental interval")
    run.add_argument("--end", metavar="UTC_TIMESTAMP", help="fixed timezone-aware interval end")
    return parser.parse_args(argv)


def _emit(value: Mapping[str, object], *, error: bool = False) -> None:
    print(json.dumps(dict(value), ensure_ascii=False, sort_keys=True), file=sys.stderr if error else sys.stdout)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = parse_args(argv)
    try:
        end = None
        if args.command == "run" and args.end:
            try:
                end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
            except ValueError:
                raise ConfigError("--end must be a valid ISO-8601 timestamp") from None
            if end.tzinfo is None:
                raise ConfigError("--end must be timezone-aware")
            end = end.astimezone(UTC)
            if end > datetime.now(UTC):
                raise ConfigError("--end must not be in the future")
        settings = load_settings(args.command, dict(os.environ if env is None else env))
        lock = acquire_lock(settings.lock_path)
        try:
            if args.command == "preflight":
                result = {
                    "postgres": pg_preflight(settings),
                    "qdrant": qdrant_preflight(settings),
                    "checkpoint_end": read_checkpoint(settings).isoformat(),
                }
            elif args.command == "run":
                result = run_interval(settings) if end is None else run_interval(settings, end)
        finally:
            lock.close()
        _emit({"status": "ok", "mode": args.command, "result": result})
        return 0
    except BlockingIOError:
        _emit({"status": "locked"}, error=True)
        return 75
    except ConfigError as exc:
        _emit({"status": "configuration_error", "error": str(exc)}, error=True)
        return 78
    except IngestError as exc:
        _emit({"status": "failed", "error": str(exc)}, error=True)
        return 1
    except Exception as exc:
        _emit({"status": "failed", "error": type(exc).__name__}, error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
