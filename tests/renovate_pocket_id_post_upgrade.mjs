// Future existing official-image CI only: no provider API, no network, no consumer scripts.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
assert.equal(process.platform, 'linux', 'Linux qualification cannot be skipped');
assert.deepEqual(process.argv.slice(2), ['/usr/local/renovate']);
assert.equal(JSON.parse(fs.readFileSync('/usr/local/renovate/package.json')).version, '44.215.0');
assert(fs.existsSync('/usr/bin/env'));
assert(fs.existsSync('/usr/bin/git'));
const load = file => import(pathToFileURL('/usr/local/renovate/dist/' + file + '.js').href);
const { GlobalConfig } = await load('config/global');
const { setCustomEnv, setUserEnv } = await load('util/env');
const { initRepo, syncGit } = await load('util/git/index');
const { default: postArtifacts } = await load('workers/repository/update/branch/execute-post-upgrade-commands');
const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pocket-id-native-')));
const file = 'deployments/pocket-id/image/Dockerfile';
const command = '/usr/bin/env -u NODE_OPTIONS -u NODE_PATH ' + process.execPath + ' --no-addons /config/sync_pocket_id_source.cjs';
const literal = '^' + command.replace(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`) + '$';
const fixtureSource = fs.readFileSync(new URL('../scripts/test_sync_pocket_id_source.cjs', import.meta.url), 'utf8');
const data = JSON.parse(/^const FIXTURE_DATA = (.+);$/m.exec(fixtureSource)[1]);
const recipe = Buffer.from(data.recipe, 'base64').toString('utf8');
const git = (...args) => { const p = spawnSync('/usr/bin/git', args, {cwd:root, encoding:'utf8', timeout:15000}); assert.equal(p.status,0,p.stderr); };
try {
  fs.mkdirSync(path.dirname(path.join(root,file)),{recursive:true});
  fs.writeFileSync(path.join(root,file),recipe,{mode:0o644});
  fs.writeFileSync(path.join(root,'unchanged.txt'),'preserve');
  git('init','-b','main');git('add','.');
  git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','commit','-m','Inert fixture');
  git('remote','add','origin',root);
  GlobalConfig.set({localDir:root,platform:'github',binarySource:'global',cacheDir:path.join(root,'cache'),executionTimeout:1,
    ignoreScripts:true,allowScripts:false,allowPlugins:false,allowedEnv:[],allowShellExecutorForPostUpgradeCommands:false,allowedCommands:[literal]});
  const poison=path.join(root,'preload.cjs'),marker=path.join(root,'untrusted-preload-executed');
  fs.writeFileSync(poison,`require('node:fs').writeFileSync(${JSON.stringify(marker)},'BAD');throw new Error('BAD');`);
  setCustomEnv({PATH:'/usr/bin:/bin',HOME:root,NODE_OPTIONS:'--require='+poison,NODE_PATH:root});setUserEnv({});
  await initRepo({url:root,defaultBranch:'main',currentBranch:'main'});await syncGit();
  const invoke = (contents,cmd=command) => postArtifacts({manager:'dockerfile',branchName:'renovate/fixture',
    upgrades:[{depName:'ghcr.io/pocket-id/pocket-id',packageFile:file}],
    postUpgradeTasks:{commands:[cmd],executionMode:'branch',fileFilters:[file]},
    updatedPackageFiles:[{type:'addition',path:file,contents}],updatedArtifacts:[],artifactErrors:[]});
  // RED proves the production helper is actually invoked, not skipped by native filtering.
  const red = await invoke('malformed fixture\n');assert.equal(red.artifactErrors.length,1);
  const contents='# Native post-upgrade invocation fixture\n'+recipe;
  const green=await invoke(contents);assert.deepEqual(green.artifactErrors,[]);
  assert(green.updatedArtifacts.some(row=>row.path===file && Buffer.from(row.contents).toString()===contents));
  assert.equal(fs.readFileSync(path.join(root,file),'utf8'),contents);
  const denied=await invoke(contents,command+' --consumer-argument');assert.equal(denied.artifactErrors.length,1);
  assert(!fs.existsSync(marker));assert.equal(fs.readFileSync(path.join(root,'unchanged.txt'),'utf8'),'preserve');
  console.log('PASS actual native branch postUpgradeTasks RED/GREEN, exact allowlist, pre-preload env clearing, networkless same-version helper');
} finally { fs.rmSync(root,{recursive:true,force:true});assert(!fs.existsSync(root)); }
