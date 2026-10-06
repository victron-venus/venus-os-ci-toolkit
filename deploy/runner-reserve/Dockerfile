FROM docker.io/library/docker:29-dind@sha256:7dcdfc4a20246236f558175182ccace1eb15a41bd3eb119dd2284f393498b7c1 AS docker
FROM docker.io/library/node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS node
FROM ghcr.io/actions/actions-runner:2.337.0@sha256:e5496277be5d09bc968b3d64911b74e219ac4a3f2edce956a3ecf9271bea1ef4 AS runner
FROM docker.io/library/ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl wget git gh jq sudo unzip zip xz-utils zstd tar \
    python3 python3-pip python3-venv python3-dev build-essential cmake pkg-config \
    libssl-dev libffi-dev libicu74 libkrb5-3 libzstd1 zlib1g liblttng-ust1t64 \
    libdbus-1-dev libglib2.0-dev iptables iproute2 procps util-linux \
    openjdk-21-jre-headless postgresql-client && \
    useradd --uid 1001 --create-home --shell /bin/bash runner && \
    printf 'runner ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/runner && \
    chmod 0440 /etc/sudoers.d/runner && \
    mkdir -p /opt/hostedtoolcache /opt/actions-runner && \
    chown runner:runner /opt/hostedtoolcache
COPY --from=docker /usr/local/bin/ /usr/local/bin/
COPY --from=docker /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/ /usr/local/lib/node_modules/
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=runner /home/runner/ /opt/actions-runner/
COPY start-runner.sh /opt/victron-ci-reserve/start-runner.sh
RUN chmod 0755 /opt/victron-ci-reserve/start-runner.sh && \
    node --version && docker --version && /opt/actions-runner/bin/Runner.Listener --version
ENV HOME=/home/runner RUNNER_TOOL_CACHE=/opt/hostedtoolcache AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache ImageOS=ubuntu24
WORKDIR /home/runner
ENTRYPOINT ["/opt/victron-ci-reserve/start-runner.sh"]
