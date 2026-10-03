// Only repositories with a merged, explicit renovate.json are processed.
// Do not use autodiscovery: BOT_PAT can see repositories outside this rollout.
module.exports = {
  platform: 'github',
  onboarding: false,
  requireConfig: 'required',
  repositories: require('./renovate-repositories.json'),
  persistRepoData: false,
  ignoreScripts: true,
  allowPlugins: false,
  allowedCommands: [],
};
