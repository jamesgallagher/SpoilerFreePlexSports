# SpoilerFreePlexSports
# Phase 1: image runs the sfps CLI. The watcher daemon becomes the default
# command in Phase 5.
FROM python:3.12-slim

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
CMD ["config"]
