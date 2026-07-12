# SpoilerFreePlexSports
FROM python:3.12-slim

# Links the GHCR package to this repo
LABEL org.opencontainers.image.source="https://github.com/jamesgallagher/SpoilerFreePlexSports"
LABEL org.opencontainers.image.description="Spoiler-free sports organizer for Plex"

# gosu: drop root to PUID:PGID in the entrypoint (unRAID file ownership)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY sfps/ ./sfps/
RUN pip install --no-cache-dir .

# Docker volume mount points (see README / design.md §4)
RUN mkdir -p /watch /library /config
VOLUME ["/watch", "/library", "/config"]

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Daemon touches /config/heartbeat; sfps health checks its age
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["sfps", "health"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["daemon"]
