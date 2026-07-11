"""sfps command-line interface.

Phase 1 commands:
    sfps process <file> [--dry-run]   run one file through the pipeline
    sfps config                       show effective configuration + problems
    sfps version                      print version
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

from sfps import __version__
from sfps.config import Config
from sfps.log import setup_logging
from sfps.pipeline import process_file

log = logging.getLogger(__name__)

_SECRET_FIELDS = {"gemini_api_key", "thesportsdb_api_key", "plex_token"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sfps",
        description="SpoilerFreePlexSports: spoiler-free sports organizer for Plex.",
    )
    sub = parser.add_subparsers(dest="command")

    p_process = sub.add_parser("process", help="run one media file through the pipeline")
    p_process.add_argument("file", type=Path, help="path to the media file")
    p_process.add_argument(
        "--dry-run",
        action="store_true",
        help="log planned actions without moving or writing anything",
    )

    sub.add_parser("config", help="show effective configuration and any problems")
    sub.add_parser("version", help="print version and exit")
    return parser


def _redact(name: str, value: object) -> object:
    if name in _SECRET_FIELDS and value:
        return "***set***"
    return value


def cmd_config(config: Config) -> int:
    for f in dataclasses.fields(config):
        print(f"{f.name:20} = {_redact(f.name, getattr(config, f.name))}")
    problems = config.validate()
    if problems:
        print("\nproblems:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nconfiguration OK")
    return 0


def cmd_process(config: Config, file: Path, dry_run: bool) -> int:
    dry_run = dry_run or config.dry_run
    problems = config.validate()
    if problems:
        level = logging.WARNING if dry_run else logging.ERROR
        for p in problems:
            log.log(level, "config: %s", p)
        if not dry_run:
            log.error("refusing live run with configuration problems (use --dry-run to preview)")
            return 1
    try:
        result = process_file(file, config, dry_run=dry_run)
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 2
    return 0 if result.status in ("organized", "unknown", "planned") else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config = Config.from_env()
    setup_logging(config.log_level)

    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "config":
        return cmd_config(config)
    if args.command == "process":
        return cmd_process(config, args.file, args.dry_run)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
