# SpoilerFreePlexSports
FROM python:3.12-slim

# Links the GHCR package to this repo
LABEL org.opencontainers.image.source="https://github.com/jamesgallagher/SpoilerFreePlexSports"
LABEL org.opencontainers.image.description="Spoiler-free sports organizer for Plex"

# Non-root user; PUID/PGID remapping arrives with the Phase 6 entrypoint.
RUN groupadd -g 1000 sfps && useradd -u 1000 -g sfps -m sfps

WORKDIR /app
COPY pyproject.toml README.md ./
COPY sfps/ ./sfps/
RUN pip install --no-cache-dir .

# Docker volume mount points (see docker-compose in design.md §4)
RUN mkdir -p /watch /library /config && chown sfps:sfps /watch /library /config
VOLUME ["/watch", "/library", "/config"]

USER sfps

ENTRYPOINT ["sfps"]
CMD ["daemon"]
