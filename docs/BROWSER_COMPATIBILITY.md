# Browser and operating-system acceptance

The existing `typescript-ci.yml` reusable workflow accepts an **opt-in** browser
inventory. Existing callers retain their current behavior. A successful ordinary
Node test job does not imply that a browser or another operating system was tested.

Use this for applications with a web interface and browser-dependent libraries.
Backend-only services and embedded firmware need their own runtime/platform tests,
not empty browser jobs. Keep native application, installer and hardware checks in
their current adapters.

## Prepare a consumer

From a reviewed toolkit checkout, prepare the separate browser toolchain locks:

```bash
python3 scripts/prepare_browser_compatibility.py /path/to/consumer
python3 scripts/prepare_browser_compatibility.py /path/to/consumer --check
```

This writes three standalone npm lock directories and
`.github/browser-compatibility.json`; it does not edit application dependencies,
workflows, runner settings or release policy. Review the generated inventory before
committing. Re-running the generator replaces these owned files; retain intentional
customization separately instead of overwriting a customized inventory.

Historical toolchains must remain separate generations. The shared `renovate-ci`
preset manages CI references, not npm packages. If a consumer enables Renovate's
npm manager, disable updates specifically for `tools/browser-compatibility/**`
and advance this reviewed compatibility window together; ordinary application
dependencies should continue to receive their normal updates.

The starter selects five required targets: current Chromium, Firefox and WebKit on
Linux, Chromium on Windows, and WebKit on macOS. Its extended selection runs all
three pinned Playwright generations across all three operating systems, Chrome and
Edge Stable on each system, and Chrome/Edge Beta on Linux: **35 total targets**.
The locks include npm integrity hashes and the expected bundled browser versions;
the workflow rejects changed versions before running application tests. Browser
binary installation follows Playwright's pinned revision catalog. Branded Stable
and Beta channels follow the vendor's installed current release and record its
actual version; they are not advertised as historically pinned Chrome/Edge builds.

Add these inputs to the existing TypeScript reusable call, pinned to the reviewed
toolkit commit as usual:

```yaml
with:
  browser-matrix-file: .github/browser-compatibility.json
  browser-test-command: node scripts/browser-acceptance.cjs
  browser-prepare-command: npm ci --ignore-scripts && npm run build
  browser-extended: ${{ github.event_name == 'schedule' || github.event_name == 'workflow_dispatch' }}
```

When a consumer already has its ordinary Node checks in another required job,
set `run-node-ci: false` for this browser-only call to avoid a redundant install
and lint/build/test job. The default is `true`, preserving existing callers.

Alternatively pass a JSON target array with `browser-matrix`; do not supply both.
The matrix file is relative to the checkout root. Toolchain paths and test commands
are relative to `working-directory`. A monorepo should prepare the lock directories
inside its selected application directory and keep the inventory accessible from
the checkout root.

The caller owns when the extended tier is required. Pass `browser-extended: true`
from the existing RC qualification path as well as the scheduled/manual path.
If the release adapter reuses ordinary CI without passing a channel, explicitly
thread an `extended` boolean through that existing adapter. Merely having an
extended inventory does not make a release execute it. Keep the reusable call in
the required Quality gate so a failed selected target blocks that gate. Scheduled
failures must be fixed before promoting a candidate; no target uses
`continue-on-error` and an existing red check is not waived by a smaller rerun.

## Application acceptance contract

The consumer must adapt real user-flow tests to the provided toolchain rather than
loading the application's original Playwright in every purported version target:

```javascript
const fs = require('node:fs');
const path = require('node:path');
const playwright = require(process.env.CI_PLAYWRIGHT_MODULE);
const engine = process.env.CI_BROWSER_ENGINE;
const channel = process.env.CI_BROWSER_CHANNEL;
const browser = await playwright[engine].launch({
  headless: true, ...(channel ? {channel} : {}),
});
try {
  // Run existing assertions against synthetic local fixtures. These names must
  // be added only after each actual scenario succeeds; never count skipped tests.
  const scenarios = await runApplicationScenarios(browser);
  const runtime = JSON.parse(fs.readFileSync(
    path.join(process.env.CI_BROWSER_DIAGNOSTICS, 'runtime.json'), 'utf8'));
  fs.writeFileSync(process.env.CI_BROWSER_EVIDENCE, JSON.stringify({
    ...runtime, engine: browser.browserType().name(),
    browserVersion: browser.version(), testsPassed: scenarios.length,
    testsFailed: 0, scenarios,
  }));
} finally {
  await browser.close();
}
```

This excerpt belongs inside an async function. A page-load smoke alone is not a
replacement for application acceptance: exercise navigation, form input and saved
state, reconnect/error paths, or the product's relevant rendering/playback flows.
Keep existing assertions and performance thresholds. Tests may add headed/Xvfb
launching where the application requires it; the default identity probe is
headless and does not itself count as a passing application scenario.

Evidence must match the actual runtime probe, source SHA, run ID and run attempt.
Missing evidence, zero tests, duplicated scenario names, stale attempts, mismatched
browser versions and failed tests fail the job. The adapter clears old result
evidence before invoking the application command. Write screenshots, traces and
logs under `CI_BROWSER_DIAGNOSTICS`; the workflow uploads that target's directory
even on failure. Use synthetic data: artifacts must not include personal accounts,
access tokens, production pages or private fixture contents.

## Coverage boundaries and runners

Playwright Firefox is its patched engine build. Native Firefox Stable/ESR and
Waterfox need separate product-specific native adapters. Playwright WebKit is not
Safari: test actual Safari with SafariDriver on macOS where Safari is supported.
Mobile viewport/touch emulation is not testing iOS/Android devices. Media codecs,
DRM, GPU drivers, assistive technology and embedded webviews also require their
native acceptance paths. Preserve such checks when adopting the shared matrix.

GitHub-hosted systems remain the default. Only `ubuntu-latest` targets may select
the existing, opt-in, isolated Linux reserve via `CI_RUNNER_MODE`. The planner
uses that same reserve policy. Forks and untrusted events stay hosted. Fixed
Ubuntu, macOS and Windows targets retain their explicit hosted systems; they do
not collapse onto a Linux worker during an outage. Dedicated non-Linux reserves
need their own admission evidence before routing can be extended. No switch or
runner registration is changed by this feature.

References: [Playwright browser and branded-channel differences](https://playwright.dev/docs/browsers),
[CI platform setup](https://playwright.dev/docs/ci), and
[Safari WebDriver](https://developer.apple.com/documentation/webkit/testing-with-webdriver-in-safari).
