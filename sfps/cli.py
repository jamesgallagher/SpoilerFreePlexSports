"""sfps command-line interface.

Commands:
    sfps daemon                          watch /watch and process recordings forever
    sfps process <file> [--dry-run]      run one file through the pipeline
    sfps identify <filename>             identify a game from a filename (no file needed)
    sfps match <json> [--download DIR]   match an identifier JSON against TheSportsDB
    sfps retry                           re-attempt unknowns + upgrade missing artwork
    sfps review [--set-event ID PATH]    list unmatched recordings / force a match
    sfps health                          heartbeat freshness check (docker healthcheck)
    sfps config                          show effective configuration + problems
    sfps version                         print version
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path

from sfps import __version__
from sfps.config import Config
from sfps.identifier import identify_name
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

    p_identify = sub.add_parser(
        "identify", help="identify a game from a filename string (file need not exist)"
    )
    p_identify.add_argument("filename", help="recording filename to interpret")

    p_match = sub.add_parser(
        "match", help="match an identifier JSON (string or @file) against TheSportsDB"
    )
    p_match.add_argument("guess", help="GameGuess JSON string, or @path/to/guess.json")
    p_match.add_argument(
        "--download",
        type=Path,
        metavar="DIR",
        help="also download the matched event's artwork into DIR",
    )

    sub.add_parser("daemon", help="watch the watch dir and process new recordings forever")
    sub.add_parser("health", help="exit 0 if the daemon heartbeat is fresh (docker healthcheck)")

    sub.add_parser("retry", help="re-attempt unknown matches and upgrade missing artwork")

    p_review = sub.add_parser("review", help="list unmatched recordings, or force a match")
    p_review.add_argument(
        "--set-event",
        metavar="EVENT_ID",
        help="TheSportsDB event id to force-match the given path to",
    )
    p_review.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="game folder or media file (required with --set-event)",
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


def cmd_identify(config: Config, filename: str) -> int:
    if not config.gemini_api_key:
        log.error("GEMINI_API_KEY is not set")
        return 1
    path = Path(filename)
    mtime = None
    if path.is_file():
        from datetime import datetime

        mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    guess = identify_name(path.name, mtime, config)
    print(json.dumps(dataclasses.asdict(guess), indent=2))
    return 0 if guess.identified else 3


def cmd_match(config: Config, guess_arg: str, download_dir: Path | None) -> int:
    from sfps.matcher import download_artwork, match
    from sfps.models import GameGuess

    raw = guess_arg
    if raw.startswith("@"):
        # utf-8-sig: tolerate the BOM that Windows PowerShell redirection adds
        raw = Path(raw[1:]).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(raw)
        field_names = {f.name for f in dataclasses.fields(GameGuess)}
        guess = GameGuess(**{k: v for k, v in data.items() if k in field_names})
    except (json.JSONDecodeError, TypeError) as exc:
        log.error("invalid guess JSON: %s", exc)
        return 2

    event = match(guess, config)
    if event is None:
        print(json.dumps({"matched": False}))
        return 3

    result = {"matched": True, **dataclasses.asdict(event)}
    if download_dir:
        saved = download_artwork(event, download_dir, config)
        result["downloaded"] = {kind: str(path) for kind, path in saved.items()}
    print(json.dumps(result, indent=2))
    return 0


def cmd_retry(config: Config) -> int:
    from sfps.retry import retry_artwork, retry_unknowns

    unknowns = retry_unknowns(config)
    art = retry_artwork(config)
    print(
        f"unknowns: {unknowns['eligible']} eligible, {unknowns['matched']} matched\n"
        f"artwork:  {art['checked']} checked, {art['updated']} updated"
    )
    return 0


def cmd_review(config: Config, event_id: str | None, path: Path | None) -> int:
    from sfps.ledger import Ledger
    from sfps.retry import force_match

    if event_id:
        if path is None:
            log.error("review --set-event needs the game folder or media file path")
            return 2
        result = force_match(path, event_id, config)
        if result is None or result.status != "organized":
            return 1
        print(f"organized -> {result.target_dir}")
        return 0

    entries = Ledger(config.config_dir / "ledger.db").entries(status="unknown")
    if not entries:
        print("no unmatched recordings")
        return 0
    for entry in entries:
        print(f"{entry['processed_at']}  {Path(entry['path']).name}\n    -> {entry['target']}")
    print(f"\n{len(entries)} unmatched. Force one with:")
    print('  sfps review --set-event <ID> "<target dir>"')
    return 0


def cmd_health(config: Config) -> int:
    import time

    heartbeat = config.config_dir / "heartbeat"
    try:
        age = time.time() - heartbeat.stat().st_mtime
    except OSError:
        print("unhealthy: no heartbeat file")
        return 1
    limit = max(config.sweep_seconds, 120) * 2
    if age > limit:
        print(f"unhealthy: heartbeat is {int(age)}s old (limit {limit}s)")
        return 1
    print(f"healthy: heartbeat {int(age)}s old")
    return 0


def cmd_daemon(config: Config) -> int:
    problems = config.validate()
    if problems:
        for p in problems:
            log.log(logging.WARNING if config.dry_run else logging.ERROR, "config: %s", p)
        if not config.dry_run:
            log.error("daemon refusing to start with configuration problems")
            return 1
    from sfps.watcher import Daemon

    Daemon(config).run()
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
    if args.command == "daemon":
        return cmd_daemon(config)
    if args.command == "health":
        return cmd_health(config)
    if args.command == "retry":
        return cmd_retry(config)
    if args.command == "review":
        return cmd_review(config, args.set_event, args.path)
    if args.command == "identify":
        return cmd_identify(config, args.filename)
    if args.command == "match":
        return cmd_match(config, args.guess, args.download)
    if args.command == "process":
        return cmd_process(config, args.file, args.dry_run)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
