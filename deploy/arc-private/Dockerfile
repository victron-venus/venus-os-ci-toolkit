# Build with the repository root as context and a digest-pinned sandbox base.
# Keep its guard CA and tools, but always bake the checked-in bootstrap.
ARG BASE_IMAGE
# Stage the same six payload files without copying the populated public base.
FROM scratch AS private-inputs
COPY .github/gh/release.json /tmp/private-gh/.github/gh/release.json
COPY scripts/install_release_gh.py /tmp/private-gh/scripts/install_release_gh.py
COPY deploy/runner-reserve/start-runner.sh deploy/runner-reserve/sandbox-docker.sh deploy/runner-reserve/qualify.sh /opt/victron-ci-reserve/
COPY deploy/arc-private/sandbox-docker.sh /tmp/private-wrapper/sandbox-docker.sh
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
# One assembly layer preserves the original install and wrapper replacement order.
# Stage inputs are read-only; the public base's CA and runtime metadata are retained.
RUN --mount=type=bind,from=private-inputs,target=/tmp/private-inputs,ro \
    mkdir -p /tmp/private-gh/.github/gh /tmp/private-gh/scripts \
    && cp -a --remove-destination /tmp/private-inputs/tmp/private-gh/.github/gh/release.json /tmp/private-gh/.github/gh/release.json \
    && cp -a --remove-destination /tmp/private-inputs/tmp/private-gh/scripts/install_release_gh.py /tmp/private-gh/scripts/install_release_gh.py \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64|arm64) ;; *) exit 1 ;; esac \
    && curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 180 --max-filesize 33554432 \
        "https://github.com/cli/cli/releases/download/v2.103.0/gh_2.103.0_linux_${arch}.tar.gz" \
        --output /tmp/private-gh/archive.tar.gz \
    && GODEBUG=fips140=on python3 /tmp/private-gh/scripts/install_release_gh.py \
        --archive /tmp/private-gh/archive.tar.gz --output /usr/local/bin/gh \
    && test "$(command -v gh)" = /usr/local/bin/gh \
    && gh api --paginate --slurp --help >/dev/null \
    && gh api --help | grep -F -- '--slurp' >/dev/null \
    && rm -rf /tmp/private-gh \
    && if [ -d /opt/actions-runner ]; then \
      cp -a /opt/actions-runner/. /home/runner/; \
    fi \
    && test -x /home/runner/bin/Runner.Listener \
    && test ! -e /home/runner/.runner \
    && chown -R 1001:1001 /home/runner \
    && cp -a --remove-destination /tmp/private-inputs/opt/victron-ci-reserve/start-runner.sh \
        /tmp/private-inputs/opt/victron-ci-reserve/sandbox-docker.sh \
        /tmp/private-inputs/opt/victron-ci-reserve/qualify.sh /opt/victron-ci-reserve/ \
    && mv /opt/victron-ci-reserve/sandbox-docker.sh /opt/victron-ci-reserve/sandbox-docker-base.sh \
    && cp -a --remove-destination /tmp/private-inputs/tmp/private-wrapper/sandbox-docker.sh /opt/victron-ci-reserve/sandbox-docker.sh \
    && chmod 0755 /opt/victron-ci-reserve/start-runner.sh /opt/victron-ci-reserve/sandbox-docker.sh /opt/victron-ci-reserve/qualify.sh \
    && chmod 0755 /opt/victron-ci-reserve/sandbox-docker-base.sh
USER 1001:1001
