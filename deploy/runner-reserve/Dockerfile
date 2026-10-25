# Keep the distribution ABI/package identity while backporting one upstream fix.
FROM docker.io/library/ubuntu@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS patched-libbpf
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential debhelper fakeroot libelf-dev zlib1g-dev pkg-config \
        python3 ca-certificates curl patch \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 libbpf-builder \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/sh libbpf-builder \
    && install -d -o 10001 -g 10001 -m 0755 /opt/victron-libbpf-build /opt/victron-libbpf
WORKDIR /opt/victron-libbpf-inputs
COPY scripts/build_runner_libbpf.py scripts/build_runner_libbpf.py
COPY .github/containers/runner-libbpf.json .github/containers/runner-libbpf.json
COPY .github/containers/libbpf/0001-restrict-core-poisoning.patch .github/containers/libbpf/0001-restrict-core-poisoning.patch
USER 10001:10001
RUN umask 022 && TARGETARCH="$TARGETARCH" python3 scripts/build_runner_libbpf.py
FROM --platform=$BUILDPLATFORM docker.io/library/golang@sha256:672c944cfd07599b5f1bb179869270cdc94c6254e41db4d431259693cb286b82 AS patched-go-tools
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 go-builder \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/sh go-builder \
    && install -d -o 10001 -g 10001 -m 0755 \
        /home/go-builder/go/pkg/mod /home/go-builder/.cache/go-build \
        /opt/victron-go-tools-build /opt/victron-go-tools \
    && chown 10001:10001 /home/go-builder/go /home/go-builder/go/pkg \
        /home/go-builder/.cache
ENV HOME=/home/go-builder \
    GOPATH=/home/go-builder/go \
    GOMODCACHE=/home/go-builder/go/pkg/mod \
    GOCACHE=/home/go-builder/.cache/go-build
WORKDIR /opt/victron-go-tools-inputs
COPY scripts/build_runner_go_tools.py scripts/build_runner_go_tools.py
COPY .github/containers/runner-go-tools.json .github/containers/runner-go-tools.json
COPY .github/containers/runner-go-tools/containerd/go.mod .github/containers/runner-go-tools/containerd/go.mod
COPY .github/containers/runner-go-tools/containerd/go.sum .github/containers/runner-go-tools/containerd/go.sum
COPY .github/containers/runner-go-tools/containerd/inputs.json .github/containers/runner-go-tools/containerd/inputs.json
COPY .github/containers/runner-go-tools/buildx/go.mod .github/containers/runner-go-tools/buildx/go.mod
COPY .github/containers/runner-go-tools/buildx/go.sum .github/containers/runner-go-tools/buildx/go.sum
COPY .github/containers/runner-go-tools/buildx/inputs.json .github/containers/runner-go-tools/buildx/inputs.json
COPY .github/containers/runner-go-tools/compose/go.mod .github/containers/runner-go-tools/compose/go.mod
COPY .github/containers/runner-go-tools/compose/go.sum .github/containers/runner-go-tools/compose/go.sum
COPY .github/containers/runner-go-tools/compose/inputs.json .github/containers/runner-go-tools/compose/inputs.json
USER 10001:10001
# Check the actual image-stage identity and permissions before any source build.
# Upstream root-only tests select their own skip paths; unit packages stay intact.
RUN test "$(id -u)" = 10001 && test "$(id -u -r)" = 10001 \
    && test "$(id -g)" = 10001 && test "$(id -g -r)" = 10001 \
    && for path in "$HOME" "$GOPATH" "$GOMODCACHE" "$GOCACHE" \
        /opt/victron-go-tools-build /opt/victron-go-tools; do \
        test -d "$path" && test ! -L "$path" && test -w "$path" && test -x "$path" \
        && test "$(stat -c '%u:%g' "$path")" = 10001:10001 || exit 1; \
    done \
    && test -w /tmp && test -x /tmp \
    && find /opt/victron-go-tools-inputs -exec sh -ec '\
        for path do \
            test ! -L "$path" && test -r "$path" && test ! -w "$path" \
                && test "$(stat -c "%u:%g" "$path")" = 0:0 || exit 1; \
            if test -d "$path"; then test -x "$path"; else test -f "$path"; fi || exit 1; \
        done' sh {} + \
    && printf 'Go source builder uid=%s gid=%s; workspace/cache ownership and readonly inputs verified\n' \
        "$(id -u)" "$(id -g)"
RUN umask 022 && python3 scripts/build_runner_go_tools.py containerd
RUN umask 022 && python3 scripts/build_runner_go_tools.py buildx
RUN umask 022 && python3 scripts/build_runner_go_tools.py compose
FROM docker.io/library/docker@sha256:9e79b879b177fbd72c34376a3dd3d91747da5db49e1f560f76aea59561471977 AS docker
FROM docker.io/library/node@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS node
FROM ghcr.io/actions/actions-runner@sha256:e275409999c37162649aa050ca573f03eab2eabcb3a1e7fcc47548de6b4f9690 AS runner
FROM docker.io/library/ubuntu@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
# This disposable build layer is exported only after package installation succeeds.
RUN apt-get update && apt-get -o Dpkg::Options::=--force-unsafe-io install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    git \
    iproute2 \
    iptables \
    jq \
    libdbus-1-dev \
    libffi-dev \
    libglib2.0-dev \
    libicu74 \
    libkrb5-3 \
    liblttng-ust1t64 \
    libssl-dev \
    libzstd1 \
    openjdk-21-jre-headless \
    pkg-config \
    postgresql-client \
    procps \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    sudo \
    tar \
    unzip \
    util-linux \
    wget \
    xz-utils \
    zip \
    zlib1g \
    zstd \
    && \
    useradd --uid 1001 --create-home --shell /bin/bash runner && \
    printf 'runner ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/runner && \
    chmod 0440 /etc/sudoers.d/runner && \
    mkdir -p /opt/hostedtoolcache && \
    chown runner:runner /opt/hostedtoolcache
# Install the rebuilt distro package, retaining dpkg version and copyright metadata.
COPY --from=patched-libbpf /opt/victron-libbpf/libbpf1.deb /opt/victron-libbpf/installed.sha256 /tmp/victron-libbpf/
COPY --from=patched-libbpf /opt/victron-libbpf/notices/ /usr/local/share/victron-libbpf/
RUN dpkg -i /tmp/victron-libbpf/libbpf1.deb \
    && test "$(dpkg-query -W -f='${Version}' libbpf1)" = '1:1.3.0-2build2+victron1' \
    && sha256sum --check /tmp/victron-libbpf/installed.sha256 \
    && rm -rf /tmp/victron-libbpf
# Shared automation requires gh api --slurp, absent from Ubuntu's gh 2.45.
# Verify the existing release manifest's archive, binary and version pins.
COPY .github/gh/release.json /tmp/reserve-gh/.github/gh/release.json
COPY scripts/install_release_gh.py /tmp/reserve-gh/scripts/install_release_gh.py
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64|arm64) ;; *) exit 1 ;; esac \
    && curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 180 --max-filesize 33554432 \
        "https://github.com/cli/cli/releases/download/v2.103.0/gh_2.103.0_linux_${arch}.tar.gz" \
        --output /tmp/reserve-gh/archive.tar.gz \
    && GODEBUG=fips140=on python3 /tmp/reserve-gh/scripts/install_release_gh.py \
        --archive /tmp/reserve-gh/archive.tar.gz --output /usr/local/bin/gh \
    && test "$(command -v gh)" = /usr/local/bin/gh \
    && gh api --paginate --slurp --help >/dev/null \
    && gh api --help | grep -F -- '--slurp' >/dev/null \
    && rm -rf /tmp/reserve-gh
COPY --from=docker /usr/local/bin/ /usr/local/bin/
COPY --from=docker /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
# Replace the affected executables, including the separately supervised daemon.
# Official runc is retained; its affected-package applicability is documented.
COPY --from=patched-go-tools /opt/victron-go-tools/bin/ /usr/local/bin/
COPY --from=patched-go-tools /opt/victron-go-tools/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
COPY --from=patched-go-tools /opt/victron-go-tools/notices/ /usr/local/share/licenses/victron-go-tools/
COPY --from=node /usr/local/bin/node /usr/local/bin/node
# Keep npm 11's CLI contract; replace only its two selected bundled dependencies with the
# reviewed upstream security release. No lifecycle hooks or package resolution.
COPY .github/containers/runner-npm.json /opt/victron-image-build/npm/.github/containers/runner-npm.json
COPY scripts/install_runner_npm.py /opt/victron-image-build/npm/scripts/install_runner_npm.py
COPY deploy/runner-reserve/qualify-npm.cjs /opt/victron-image-build/npm/qualify-npm.cjs
RUN mkdir -p /usr/local/lib/node_modules \
    && curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 180 --max-filesize 20971520 \
        https://registry.npmjs.org/npm/-/npm-11.21.1.tgz --output /opt/victron-image-build/npm/npm.tgz \
    && curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 180 --max-filesize 20971520 \
        https://registry.npmjs.org/undici/-/undici-6.29.1.tgz --output /opt/victron-image-build/npm/undici.tgz \
    && curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 180 --max-filesize 20971520 \
        https://registry.npmjs.org/ip-address/-/ip-address-10.7.4.tgz --output /opt/victron-image-build/npm/ip-address.tgz \
    && python3 /opt/victron-image-build/npm/scripts/install_runner_npm.py \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node /opt/victron-image-build/npm/qualify-npm.cjs \
    && rm -rf /opt/victron-image-build/npm
COPY --from=runner --chown=1001:1001 /home/runner/ /home/runner/
COPY scripts/prune_runner_npm.py /tmp/prune_runner_npm.py
RUN python3 /tmp/prune_runner_npm.py /home/runner \
    && rm /tmp/prune_runner_npm.py \
    && /home/runner/externals/node20/bin/node \
        -e 'require("node:crypto"); require("node:fs"); console.log(process.version)' \
    && /home/runner/externals/node24/bin/node \
        -e 'require("node:crypto"); require("node:fs"); console.log(process.version)'
COPY deploy/runner-reserve/start-runner.sh deploy/runner-reserve/sandbox-docker.sh \
    deploy/runner-reserve/qualify.sh deploy/runner-reserve/qualify-npm.cjs \
    deploy/runner-reserve/guard-ca.crt /opt/victron-ci-reserve/
RUN chmod 0755 /opt/victron-ci-reserve/*.sh && \
    node --version && docker --version && /home/runner/bin/Runner.Listener --version
ENV HOME=/home/runner RUNNER_TOOL_CACHE=/opt/hostedtoolcache AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache ImageOS=ubuntu24
WORKDIR /home/runner
USER 1001:1001
ENTRYPOINT ["/opt/victron-ci-reserve/start-runner.sh"]
