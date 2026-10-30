# Browser installation for ESP firmware

The shared installer turns **already built, public factory firmware** into an
HTTPS page: choose the exact board, connect USB, install, and complete the
firmware's own setup. Visitors do not need Python, PlatformIO or ESPHome on their
computer. The installer pins the module version and its Subresource Integrity (SHA384)
digest. It uses [ESP Web Tools 10.4.0](https://github.com/esphome/esp-web-tools/releases/tag/10.4.0).

The shared code does not compile firmware, change production devices, discover
private credentials, or imply hardware acceptance. Each project owns its build,
wiring, provisioning behavior and release gates. A bootstrap image must be
clearly labeled **bootstrap**, with the remaining adoption/configuration steps.

## Files supplied by the project

Upload a current-run artifact containing `web-flasher.json` and only the
explicitly referenced public binaries. Use an artifact name ending in
`-run-${{ github.run_id }}-attempt-${{ github.run_attempt }}`. This prevents a
rerun from accidentally using an earlier attempt's files.

```json
{
  "schema": 1,
  "name": "Example sensor",
  "version": "1.2.3-rc.1",
  "channel": "rc",
  "project_url": "https://github.com/example/sensor",
  "release_url": "https://github.com/example/sensor/releases/tag/v1.2.3-rc.1",
  "profiles": [
    {
      "id": "esp32-devkit",
      "label": "ESP32 DevKit — 4 MB",
      "board": "ESP32-WROOM DevKit, 4 MB flash; wiring as documented",
      "chip_family": "ESP32",
      "provisioning": "improv",
      "description": "USB Wi-Fi setup, then follow the device setup guide.",
      "setup_url": "https://github.com/example/sensor/blob/main/docs/setup.md",
      "parts": [{"path": "firmware/sensor.factory.bin", "offset": 0}]
    }
  ]
}
```

`version` and `channel` may instead be supplied as action inputs. If also present
in the catalog, they must match; relabeling another release's artifact fails.
Allowed channels are `stable`, `beta`, `rc`, and `nightly`. An optional `sha256`
on the binary part checks an expected digest before any output is created.

Each profile accepts exactly **one complete factory binary at offset zero**.
For ESP32, use the build's merged image containing its bootloader, partition
table and application. Never rename an OTA image to make it appear compatible.
The default partition-table offset is `32768` (`0x8000`); declare
`partition_offset` on the profile only when the actual build uses another
aligned offset. ESP8266 accepts its complete firmware image at offset zero.

Separate hardware profiles are required even when boards share a chip family.
Chip detection cannot identify GPIO wiring, flash/PSRAM configuration or attached
peripherals. Supported chip names match ESP Web Tools 10.4.0; support in the
installer does not mean a project supplies firmware for every chip.

## Build and review locally

Use Python 3.12 or newer, with no extra Python packages:

```sh
python3 scripts/esp_web_installer.py \
  --input-dir /path/to/public-firmware-artifact \
  --catalog web-flasher.json \
  --output /path/to/new-installer-directory
```

The output directory must not exist. The generator validates all inputs first
and writes through a temporary sibling directory, so a failed build cannot leave
a partial site masquerading as a finished installer. The output contains the
HTML/CSS/JavaScript, per-profile manifests, declared firmware copies, a catalog
with firmware digests, and `SHA256SUMS`. Unlisted files are never copied.

The checks reject path traversal, symlinks, unsupported chips, split/overlapping
parts, empty files, missing ESP boot/partition/application headers, mismatching
bootloader/application chip IDs, known private
key/token markers, and mismatching checksums. These are structural checks, not a
complete firmware verifier. They cannot prove the absence of arbitrary private
strings or guarantee a working physical device.

## Reuse the action inside a build

After checking out a reviewed toolkit commit into `_toolkit`:

```yaml
- uses: ./_toolkit/actions/esp-web-installer
  with:
    input-directory: release-firmware
    output-directory: installer-site
    version: ${{ inputs.version }}
    channel: ${{ inputs.channel }}
```

Archive the generated site alongside the firmware if desired. Keep its binary
bytes identical to the release assets. The generated catalog records SHA256
digests so the copies can be compared.

## Reuse the Pages workflow

Call `.github/workflows/esp-web-installer.yml` at a reviewed full commit SHA.
Pass the **same commit** as `toolkit-ref`. Replace `TOOLKIT_COMMIT_SHA` below
with that actual 40-character SHA; it is intentionally not a runnable moving ref.

```yaml
jobs:
  installer:
    needs: [firmware, release]
    permissions:
      contents: read
      pages: write
      id-token: write
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/esp-web-installer.yml@TOOLKIT_COMMIT_SHA
    with:
      toolkit-ref: TOOLKIT_COMMIT_SHA
      artifact-name: firmware-run-${{ github.run_id }}-attempt-${{ github.run_attempt }}
      deploy: true
```

The firmware artifact must come from this workflow run and attempt. The shared
workflow checks out only the pinned toolkit; it does not check out or execute
source contained in the artifact. `deploy: false` is the default and produces a
reviewable `esp-web-installer-site-RUN-ATTEMPT` artifact. Deployment is accepted
only for a public repository on its default branch through `push`, `schedule`
or `workflow_dispatch`. It is refused for pull requests and tag-context calls.

Enable **Settings → Pages → Source: GitHub Actions** once. The deployment job
uses the `github-pages` environment and the caller's short-lived `GITHUB_TOKEN`;
it needs `pages: write` and `id-token: write`, not a PAT. Existing Pages content
is replaced by this site, so add the installer to an existing site's build
instead of enabling this standalone deployment when a repository already hosts
a documentation website. Both jobs honor the existing `CI_RUNNER_MODE` and
`CI_RUNNER_RELEASE_LABELS` opt-in for trusted default-branch runs; GitHub-hosted
Ubuntu remains the default.

Call the installer after the project's successful publication/gates, or dispatch
an explicit default-branch workflow that downloads and verifies an existing
release into a new current-run artifact. Do not rely only on `on: release`:
releases created with `GITHUB_TOKEN` do not trigger another ordinary release
workflow. See [GitHub's event behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

This workflow publishes one explicit version. A project's stable installation
URL should continue to point to its accepted stable image; publishing a beta/RC
at that URL is a deliberate project decision, not an automatic promotion. Use a
separate preview site or include both channels in the existing site's build when
both must remain available. The page always displays the supplied channel.

## Provision without embedding secrets

ESPHome projects can use `improv_serial` with `logger` and a `wifi` configuration
without saved network credentials. Improv asks for Wi-Fi credentials over the
local USB connection. Use unique device names and a documented adoption flow.
Do not ship a shared production API encryption key, OTA password, MQTT password,
real Wi-Fi network, hardware identifier or private service URL in firmware.

If MQTT, device addresses or calibration still require project-specific setup,
say so next to the install button and link the appropriate guide. For ESPHome
bootstrap builds, retain the encrypted production configuration and explain how
to adopt/rebuild the device with its individual credentials. Do not describe
bootstrap networking as fully configured sensor functionality.

Choose `provisioning: manual` when the firmware uses its own setup access point
or another process, and `none` when network provisioning does not apply. The
installer does not invent a Wi-Fi form for firmware lacking Improv.

## User troubleshooting

- Use the HTTPS page in a desktop browser with Web Serial, such as Chrome or
  Edge. The page checks the actual browser capability. Mobile browsers, Safari
  and embedded webviews may not support it.
- A charge-only cable will power the board without exposing a serial port. Try
  a data cable and direct USB connection before installing drivers.
- Follow the board vendor's driver and BOOT/RESET instructions. On Linux, check
  serial-port group access; close any other application using that port.
- Confirm the exact model, flash size and wiring. Automatic chip detection is
  not automatic board identification.
- Back up existing device settings. A factory flash or erase may remove them.
  The installer retains ESP Web Tools' explicit erase choice rather than
  promising that every installation preserves existing data.
- If the USB installer module cannot load, the connect button stays hidden.
  Check access to the pinned `unpkg.com` module and content-blocker settings.
  When upgrading ESP Web Tools, update and verify both its version and SRI digest;
  release downloads remain usable.
- If the website loads but firmware cannot be downloaded, verify the generated
  relative URLs and `SHA256SUMS`. Files are hosted beside the page, avoiding
  cross-origin release-download restrictions. Do not open `index.html` via
  `file://` for installation.

## References and acceptance

- [ESP Web Tools](https://esphome.github.io/esp-web-tools/) — manifests, merged images, HTTPS and CORS.
- [Espressif esptool image targets](https://github.com/espressif/esptool/tree/v5.1.0/esptool/targets) — bootloader offsets and image chip IDs.
- [ESPHome Improv serial](https://esphome.io/components/improv_serial/) — local Wi-Fi setup and logger requirements.
- [ESPHome build action](https://github.com/esphome/build-action) — native firmware and complete manifest generation.
- [GitHub Pages workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages) — deployment permissions and environments.

The toolkit tests validate artifacts and page behavior. Each project should also
record at least one real cold USB installation for each hardware profile, first
network setup, reboot, and its actual sensor/application behavior. Never replace
that record with a passing manifest or simulated serial test.
