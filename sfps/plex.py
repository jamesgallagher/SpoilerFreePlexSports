"""Optional Plex partial rescan (design.md §3.7).

Two plain HTTP calls — list sections, then refresh the one containing the
organized folder — so no plexapi dependency. Enabled only when PLEX_URL and
PLEX_TOKEN are both set; every failure is non-fatal (Plex's scheduled scan
remains the fallback).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from sfps.config import Config

log = logging.getLogger(__name__)


def enabled(config: Config) -> bool:
    return bool(config.plex_url and config.plex_token)


def plex_path(config: Config, target_dir: Path) -> str:
    """Translate our /library path to Plex's view of the same folder."""
    text = str(target_dir).replace("\\", "/")
    if config.plex_library_path:
        lib = str(config.library_dir).replace("\\", "/").rstrip("/")
        if text == lib or text.startswith(lib + "/"):
            return config.plex_library_path.rstrip("/") + text[len(lib):]
    return text


def rescan(
    config: Config, target_dir: Path, transport: httpx.BaseTransport | None = None
) -> bool:
    """Trigger a partial scan of the section containing target_dir."""
    if not enabled(config):
        return False
    path = plex_path(config, target_dir)
    try:
        with httpx.Client(
            base_url=config.plex_url,
            headers={"X-Plex-Token": config.plex_token, "Accept": "application/xml"},
            timeout=15.0,
            transport=transport,
        ) as client:
            response = client.get("/library/sections")
            response.raise_for_status()
            root = ET.fromstring(response.text)
            for section in root.iter("Directory"):
                for location in section.iter("Location"):
                    loc = str(location.get("path") or "").rstrip("/")
                    if loc and (path == loc or path.startswith(loc + "/")):
                        key = section.get("key")
                        client.get(
                            f"/library/sections/{key}/refresh", params={"path": path}
                        ).raise_for_status()
                        log.info("plex: partial scan triggered (section %s) for %s", key, path)
                        return True
        log.warning("plex: no library section contains %s - scan not triggered", path)
    except (httpx.HTTPError, ET.ParseError) as exc:
        log.warning("plex: rescan failed (%s)", exc)
    return False
