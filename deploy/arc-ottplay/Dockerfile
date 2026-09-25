# Build for linux/amd64. Upstream indexes resolved read-only on 2026-09-24.
# Build/push this custom image separately, then pin its resulting digest in values.
# actions-runner:2.337.0
FROM ghcr.io/actions/actions-runner@sha256:e5496277be5d09bc968b3d64911b74e219ac4a3f2edce956a3ecf9271bea1ef4 AS runner
# node:22.23.3-bookworm-slim
FROM node@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS node
# ubuntu:24.04
FROM ubuntu@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git gh jq unzip zip tar gzip xz-utils coreutils \
    python3 python3-pip python3-venv build-essential pkg-config procps \
    ffmpeg openjdk-17-jdk-headless libicu74 libssl3t64 libkrb5-3 zlib1g \
    libpulse0 libx11-6 libxcb1 libnss3 libx11-xcb1 libxi6 libxtst6 \
    libxrender1 libxkbcommon0 libvulkan1 mesa-vulkan-drivers xvfb \
 && rm -rf /var/lib/apt/lists/*
# Install only the locked build tool, without npm lifecycle scripts. Browser
# binaries follow the consuming lockfile; jobs must not run install --with-deps.
COPY buildtools/package.json buildtools/package-lock.json /opt/ottplay-buildtools/
RUN npm ci --ignore-scripts --no-audit --no-fund --prefix /opt/ottplay-buildtools \
 && node /opt/ottplay-buildtools/node_modules/playwright/cli.js install-deps chromium \
 && rm -rf /opt/ottplay-buildtools /var/lib/apt/lists/* /root/.npm \
 && groupadd --gid 1001 runner \
 && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash runner \
 && mkdir -p /opt/actions-runner /opt/hostedtoolcache /opt/android-sdk /opt/ottplay \
 && chown -R 1001:1001 /opt/hostedtoolcache /opt/android-sdk
COPY --from=runner /home/runner/ /opt/actions-runner/
COPY start-runner.sh smoke.sh /opt/ottplay/
RUN chmod 0555 /opt/ottplay/*.sh \
 && [[ "$(dpkg --print-architecture)" == amd64 ]] \
 && node --version && python3 --version && java -version \
 && /opt/actions-runner/bin/Runner.Listener --version
ENV HOME=/home/runner \
    RUNNER_MANUALLY_TRAP_SIG=1 \
    RUNNER_TOOL_CACHE=/opt/hostedtoolcache \
    AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache \
    ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
WORKDIR /home/runner
USER 1001:1001
ENTRYPOINT ["/opt/ottplay/start-runner.sh"]
