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
# Metadata workflows require gh api --slurp (absent from Ubuntu's gh 2.45).
# Reuse the reviewed release CLI pins and installer without adding a Go compiler.
COPY .github/gh/release.json /tmp/private-gh/.github/gh/release.json
COPY scripts/install_release_gh.py /tmp/private-gh/scripts/install_release_gh.py
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64|arm64) ;; *) exit 1 ;; esac \
    && curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 180 --max-filesize 33554432 \
        "https://github.com/cli/cli/releases/download/v2.102.0/gh_2.102.0_linux_${arch}.tar.gz" \
        --output /tmp/private-gh/archive.tar.gz \
    && GODEBUG=fips140=on python3 /tmp/private-gh/scripts/install_release_gh.py \
        --archive /tmp/private-gh/archive.tar.gz --output /usr/local/bin/gh \
    && test "$(command -v gh)" = /usr/local/bin/gh \
    && gh api --paginate --slurp --help >/dev/null \
    && gh api --help | grep -F -- '--slurp' >/dev/null \
    && rm -rf /tmp/private-gh
# Older bases stored a pristine runner under /opt and copied it during startup.
# Populate the image layer once so every ephemeral worker starts without that I/O.
RUN if [ -d /opt/actions-runner ]; then \
      cp -a /opt/actions-runner/. /home/runner/; \
    fi \
    && test -x /home/runner/bin/Runner.Listener \
    && test ! -e /home/runner/.runner \
    && chown -R 1001:1001 /home/runner
COPY deploy/runner-reserve/start-runner.sh deploy/runner-reserve/sandbox-docker.sh deploy/runner-reserve/qualify.sh /opt/victron-ci-reserve/
RUN mv /opt/victron-ci-reserve/sandbox-docker.sh /opt/victron-ci-reserve/sandbox-docker-base.sh
COPY deploy/arc-private/sandbox-docker.sh /opt/victron-ci-reserve/sandbox-docker.sh
RUN chmod 0755 /opt/victron-ci-reserve/start-runner.sh /opt/victron-ci-reserve/sandbox-docker.sh /opt/victron-ci-reserve/qualify.sh
RUN chmod 0755 /opt/victron-ci-reserve/sandbox-docker-base.sh
USER 1001:1001
