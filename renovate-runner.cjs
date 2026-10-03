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
  allowedCommands: [],
};
