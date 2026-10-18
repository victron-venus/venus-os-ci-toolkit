# Supply the already qualified, digest-pinned sandbox base. This derivative
# keeps its guard CA, bootstrap, Docker and Actions runner unchanged.
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
USER 1001:1001
