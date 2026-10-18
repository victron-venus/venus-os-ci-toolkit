# Build with the repository root as context and a digest-pinned sandbox base.
# Keep its guard CA and tools, but always bake the checked-in bootstrap.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER 0:0
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    libreoffice-calc \
    libreoffice-impress \
    libreoffice-writer \
    poppler-utils \
    shellcheck \
    tmux \
    && rm -rf /var/lib/apt/lists/*
# Older bases stored a pristine runner under /opt and copied it during startup.
# Populate the image layer once so every ephemeral worker starts without that I/O.
RUN if [ -d /opt/actions-runner ]; then \
      cp -a /opt/actions-runner/. /home/runner/; \
    fi \
    && test -x /home/runner/bin/Runner.Listener \
    && test ! -e /home/runner/.runner \
    && chown -R 1001:1001 /home/runner
COPY deploy/runner-reserve/start-runner.sh deploy/runner-reserve/sandbox-docker.sh deploy/runner-reserve/qualify.sh /opt/victron-ci-reserve/
RUN chmod 0755 /opt/victron-ci-reserve/start-runner.sh /opt/victron-ci-reserve/sandbox-docker.sh /opt/victron-ci-reserve/qualify.sh
USER 1001:1001
