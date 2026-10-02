# Native multi-platform OCI packaging

Versioned container consumers receive `merge_oci_archives.py` and
`assemble_native_container.py` with their contract tests. Opt in through the
consumer's packaging adapter; installing the toolkit alone does not change runners.

Build `linux/amd64` on `ubuntu-24.04` and `linux/arm64` on `ubuntu-24.04-arm`.
Each job applies the same frozen plan and exports OCI bytes. Smoke-test that exact
build result on its native runner, then create a normal version receipt covering
`container-ARCH.oci.tar` and `native-build.json`. Intermediate artifact names must
not match the final `release-assets*` publication selector.

The evidence document records source SHA, plan digest, run ID/attempt, actual
runner architecture, image config digest, successful smoke, toolchain and timings.
The assembler verifies both receipts and every OCI blob, requires exactly the two
requested platforms and source/version labels, and matches the smoked config
digests to the archived images. It retains both receipts inside ordinary build
evidence covered by the final package receipt. Intermediate receipts must not be
published separately: their inventories describe the intermediate archives.

```sh
python3 scripts/assemble_native_container.py \
  --input linux/amd64=native-inputs/amd64 \
  --input linux/arm64=native-inputs/arm64 \
  --output release-dist/app.oci.tar \
  --evidence release-dist/container-build-evidence.json
```

Run inside the current Actions checkout with its applied `.release-plan.json` and
`.release-inputs.json`. Inputs from a different workflow attempt are rejected;
rerun the complete build after an attempt changes. The lower-level pure merger
can also validate local archives with explicit version and revision arguments.

No image is pushed or rebuilt during assembly. Original manifests, configs,
layers and attestations retain their bytes. A deterministic new index combines
them under a single root reference compatible with
`skopeo copy --all --preserve-digests oci-archive:...`. Existing staging,
qualification, release checks and RC-to-stable byte promotion remain required.

Reference consumer: `victron-venus/dbus-event-log`. Its PR gate invokes the same
adapter using a disposable local plan, while release builds use the allocated
plan. Cold native runs must be measured before claiming an improvement; runner
startup, transfers and assembly can outweigh emulation savings for small images.
