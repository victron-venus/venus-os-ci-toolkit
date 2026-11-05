// Exact reviewed generators only; never execute repository scripts or a shell.
const postUpgradeCommands = [
  "uv export --locked --offline --no-build --extra dev --group build --group security --no-emit-project --output-file .github/requirements-ci.txt",
  "uv export --locked --offline --no-build --no-default-groups --no-emit-project --output-file .github/requirements-runtime.txt",
  "uv export --locked --offline --no-build --no-dev --extra monitor --no-emit-project --format requirements-txt --output-file requirements-docker.txt",
  "uv export --locked --offline --no-build --no-dev --no-emit-project --format requirements-txt --output-file .renovate-constraints.txt",
  "uv export --locked --offline --no-build --no-dev --no-emit-project --format requirements-txt --output-file .renovate-constraints.txt --no-hashes",
  "uv export --locked --offline --no-build --only-group build --no-emit-project --output-file .github/requirements-release-build.txt",
  "uv export --locked --offline --no-build --only-group security --no-emit-project --output-file .github/requirements-release-security.txt",
  "uv pip compile --python-version=3.12 --python-platform=x86_64-manylinux_2_28 --generate-hashes --no-build .github/requirements-fuzz.in --output-file=.github/requirements-fuzz.txt",
  "uv pip compile .github/requirements-browser.in --constraint .renovate-constraints.txt --python-version 3.12 --universal --no-build --generate-hashes --no-header --no-annotate --output-file .github/requirements-browser.txt",
  "uv pip compile .github/requirements-fuzz.in --constraint .renovate-constraints.txt --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --no-build --generate-hashes --no-header --no-annotate --output-file .github/requirements-fuzz.txt"
];
const literalCommand = (command) => '^' + command.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$';

// Only repositories with a merged, explicit renovate.json are processed.
// Do not use autodiscovery: BOT_PAT can see repositories outside this rollout.
module.exports = {
  platform: 'github',
  // Dedicated signing key registered to this account, never a personal login key.
  gitAuthor: 'Renovate CI <alvit.work+4alvit@gmail.com>',
  onboarding: false,
  requireConfig: 'required',
  repositories: require('./renovate-repositories.json'),
  persistRepoData: false,
  ignoreScripts: true,
  allowPlugins: false,
  allowShellExecutorForPostUpgradeCommands: false,
  allowedCommands: postUpgradeCommands.map(literalCommand),
};
