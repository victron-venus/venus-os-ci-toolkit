# Native rootful candidate. Qualify the resulting image before any adoption.
FROM gcr.io/distroless/static-debian13@sha256:18f4cc26c9ffa7f20a359298be7deadac5c607c8e03a02b8119f1a5fc2f82c99
COPY --chmod=0755 bin/buildkitd /usr/bin/buildkitd
COPY --chmod=0755 bin/buildctl /usr/bin/buildctl
COPY --chmod=0755 bin/runc /usr/bin/buildkit-runc
COPY --chmod=0755 bin/oci-cgroup-reader /usr/bin/oci-cgroup-reader
COPY licenses/ /usr/share/licenses/oci-tools/
# Accepted only for disposable hosted qualification; see docs/OCI_TOOL_PROMOTION.md.
# trivy:ignore:DS-0002:exp:2026-12-04
USER 0:0
ENV PATH=/usr/bin
VOLUME ["/var/lib/buildkit"]
ENTRYPOINT ["/usr/bin/buildkitd", "--oci-worker=true", "--containerd-worker=false", "--oci-worker-snapshotter=native", "--oci-worker-net=host", "--oci-worker-binary=/usr/bin/buildkit-runc"]
