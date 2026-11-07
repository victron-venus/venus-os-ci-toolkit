// Offline regression tests execute exact upstream 44.215.0 functions in a VM.
// Git/platform/manager I/O is stubbed; no producer, credentials or network.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const crypto = require('node:crypto');
const { pathToFileURL } = require('node:url');
const { test } = require('node:test');
const fixture = path.join(__dirname, 'fixtures/renovate-completion-44.215.0');
const compatPath = process.env.COMPLETION_COMPAT_FILE || path.join(__dirname, 'renovate-completion-compat.mjs');
const verifierPath = process.env.COMPLETION_VERIFIER_FILE || path.join(__dirname, 'verify-renovate-completion.cjs');
const { validateRecords } = require(verifierPath);
const manifest = JSON.parse(fs.readFileSync(path.join(fixture, 'manifest.json'), 'utf8'));
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const read = relative => fs.readFileSync(path.join(fixture, relative), 'utf8');
const strip = source => source.replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
const compatibility = import(pathToFileURL(compatPath).href);
const branchPath = 'dist/workers/repository/update/branch/index.js';
const loggerPath = 'dist/logger/renovate-logger.js';
const repo = 'fixture/repository';
const start = { name:'renovate',v:0,level:30,time:'2026-11-07T18:00:00.000Z',repository:repo,msg:'Repository started',renovateVersion:'44.215.0' };
const finish = { ...start,msg:'Repository finished',result:'done',status:'activated',enabled:true,onboarded:true,exitCode:0 };

async function branchReplay(transform, failedArtifact) {
  const logs = [];
  const noop = async () => {};
  const logger = Object.fromEntries(['trace','debug','info','warn','error','fatal'].map(level => [level, (...args) => logs.push({level,args:JSON.parse(JSON.stringify(args))})]));
  const context = { logger, coerceArray:x=>x==null?[]:Array.isArray(x)?x:[x], isNonEmptyArray:x=>Array.isArray(x)&&x.length>0,
    scm:{branchExists:async()=>false,checkoutBranch:noop,isBranchConflicted:async()=>false,commitAndPush:()=>{throw Error('Unexpected mutation');}},
    platform:{getBranchPr:async()=>null},emojify:s=>s,prAlreadyExisted:async()=>null,getCount:()=>0,isLimitReached:()=>false,
    isScheduledNow:()=>true,computeInternalChecksStatus:async()=>null,decideBranchReuse:()=>({action:'no-reuse',userRebaseRequested:false}),
    getAdditionalFiles:async()=>({artifactErrors:[],updatedArtifacts:[],artifactNotices:[]}),
    updateActionsLockfile:async()=>({artifactErrors:[],updatedArtifacts:[]}),executePostUpgradeCommands:async()=>null,
    embedChangelogs:noop,bumpVersions:noop,removeMeta:()=>{},compileCommitBody:()=> 'Update lockfile',compileCommitTrailers:()=>[],
    handleBranchError:error=>{throw error;},GlobalConfig:{get:()=>undefined},ensurePr:()=>{throw Error('Unexpected PR creation');},failedArtifact
  };
  vm.createContext(context);
  vm.runInContext(strip(read('dist/workers/repository/update/branch/get-updated.js')),context);
  vm.runInContext(`getUpdatedPackageFiles = async () => {
    const updatedPackageFiles = failedArtifact ? [{path:'fixture.lock'}] : [];
    const removed = removeUnrefreshedLockfileOnlyChanges(updatedPackageFiles,[{isLockfileOnly:true,lockFile:'fixture.lock'}],[]);
    const artifactErrors=[];reportUnrefreshedLockfileOnlyChanges(removed,[],artifactErrors);
    return {updatedPackageFiles,updatedArtifacts:[],artifactErrors,artifactNotices:[]};
  };`,context);
  vm.runInContext(strip(read('dist/workers/repository/update/branch/commit.js')),context);
  vm.runInContext(strip(transform ? transform(read(branchPath),branchPath) : read(branchPath)),context);
  const result=await context.processBranch({branchName:'renovate/lock-file-maintenance',baseBranch:'main',branchPrefix:'renovate/',branchPrefixOld:'renovate/',
    upgrades:[{}],packageFiles:{},errors:[],warnings:[],updatedPackageFiles:[],updatedArtifacts:[],artifactErrors:[],automerge:false});
  return {result,logs};
}

function loggerReplay(transform, remap, events) {
  const records=[];
  const levels={trace:10,debug:20,info:30,warn:40,error:50,fatal:60};
  const bunyan=Object.fromEntries(Object.entries(levels).map(([level,value])=>[level,(meta,msg)=>{
    // Match the actual installed JSON file stream threshold (proved separately).
    if(value>=30) records.push({name:'renovate',v:0,time:'2026-11-07T18:00:00.000Z',level:value,...JSON.parse(JSON.stringify(meta)),msg});
  }]));
  const once=fn=>fn();
  const context={getRemappedLevel:()=>remap,isString:x=>typeof x==='string',once,reset:()=>{},
    getMessage:(p1,p2)=>typeof p1==='string'?p1:p2,toMeta:p1=>typeof p1==='object'?p1:{},LOGGER_NOT_INITIALIZED:'not-initialized'};
  vm.createContext(context);
  vm.runInContext(strip(transform ? transform(read(loggerPath),loggerPath) : read(loggerPath)),context);
  const logger=new context.RenovateLogger('fixture-context',{repository:repo},bunyan);
  for(const [level,meta,msg] of events) logger[level](meta,msg);
  return records;
}

 test('all upstream fixture bytes/version/license retain exact provenance',()=>{
  assert.equal(manifest.version,'44.215.0');assert.equal(manifest.license,'AGPL-3.0-only');
  for(const file of manifest.files){const bytes=fs.readFileSync(path.join(fixture,file.path));assert.equal(digest(bytes),file.sha256);assert.equal(bytes.length,file.bytes);}
  assert.equal(JSON.parse(read('package.json.fixture')).version,'44.215.0');assert.match(read('license'),/GNU AFFERO GENERAL PUBLIC LICENSE/);
 });
 test('actual upstream lockfile-only failure is silently no-work before hook',async()=>{
  const {result,logs}=await branchReplay(null,true);assert.equal(result.result,'no-work');
  assert.equal(logs.filter(x=>['info','warn','error','fatal'].includes(x.level)).length,0);
  assert.ok(!logs.some(x=>x.level==='debug'&&JSON.stringify(x.args).includes('artifactErrors')));
 });
 test('actual transform exposes no-PR artifact failure as one count-only record',async()=>{
  const {transformCompletionModule}=await compatibility;const {result,logs}=await branchReplay(transformCompletionModule,true);
  assert.equal(result.result,'no-work');const warnings=logs.filter(x=>x.level==='warn');assert.equal(warnings.length,1);
  assert.deepEqual(warnings[0].args,[{artifactErrorCount:1},'Artifact generation failed before PR decision']);
  const rec={...start,level:40,msg:warnings[0].args[1],...warnings[0].args[0]};
  assert.throws(()=>validateRecords([start,rec,finish],[repo]),{code:'ARTIFACT_ERRORS'});
 });
 test('actual no-change no-work remains unchanged and has no artificial error',async()=>{
  const {transformCompletionModule}=await compatibility;
  for(const transform of [null,transformCompletionModule]){const {result,logs}=await branchReplay(transform,false);assert.equal(result.result,'no-work');assert.equal(logs.filter(x=>['warn','error','fatal'].includes(x.level)).length,0);}
 });
 test('original logger really hides remapped error and lifecycle info at file info threshold',()=>{
  assert.deepEqual(loggerReplay(null,'debug',[['error',{},'fixture error'],['info',{renovateVersion:'44.215.0'},'Repository started'],['info',{},'Repository finished']]),[]);
 });
 test('patched logger preserves error/fatal downgraded to debug or trace',async()=>{
  const {transformCompletionModule}=await compatibility;
  for(const level of ['error','fatal'])for(const remap of ['debug','trace']){
   const records=loggerReplay(transformCompletionModule,remap,[[level,{},'fixture error']]);assert.equal(records.length,1);assert.ok(records[0].level>=50);
   assert.throws(()=>validateRecords([start,...records,finish],[repo]),{code:'ERROR_OR_FATAL'});
  }
 });
 test('error/fatal remapped to info retains oldLevel and still rejects',async()=>{
  const {transformCompletionModule}=await compatibility;
  for(const level of ['error','fatal']){const records=loggerReplay(transformCompletionModule,'info',[[level,{},'fixture error']]);assert.equal(records[0].level,30);assert.equal(records[0].oldLevel,level);assert.throws(()=>validateRecords([start,...records,finish],[repo]),{code:'ERROR_OR_FATAL'});}
 });
 test('artifact warning cannot be hidden below info by repository remap',async()=>{
  const {transformCompletionModule}=await compatibility;
  for(const remap of ['trace','debug','info']){const records=loggerReplay(transformCompletionModule,remap,[['warn',{artifactErrorCount:1},'Artifact generation failed before PR decision']]);assert.equal(records.length,1);assert.throws(()=>validateRecords([start,...records,finish],[repo]),{code:'ARTIFACT_ERRORS'});}
 });
 test('starts/finishes remapped below info remain visible and complete exact roster',async()=>{
  const {transformCompletionModule}=await compatibility;
  for(const remap of ['trace','debug']){const records=loggerReplay(transformCompletionModule,remap,[['info',{renovateVersion:'44.215.0'},'Repository started'],['info',{result:'done',status:'activated',enabled:true,onboarded:true,exitCode:0},'Repository finished']]);assert.equal(validateRecords(records,[repo]).finishedCount,1);}
 });
 test('ordinary debug/info remaps remain unchanged',async()=>{
  const {transformCompletionModule}=await compatibility;
  assert.deepEqual(loggerReplay(transformCompletionModule,'debug',[['info',{},'ordinary notice']]),[]);
 });
 test('wrong hash, already patched bytes and unapproved module fail closed',async()=>{
  const {transformCompletionModule}=await compatibility;
  for(const relative of [branchPath,loggerPath]){const original=read(relative);assert.throws(()=>transformCompletionModule(original+'\n',relative));const transformed=transformCompletionModule(original,relative);assert.throws(()=>transformCompletionModule(transformed,relative));}
  assert.throws(()=>transformCompletionModule(read(loggerPath),'other.js'));
 });
 test('registration does not preload target modules or falsely claim applied',async()=>{
  const {installCompletionCompat,activeCompletionCompat}=await compatibility;assert.equal(activeCompletionCompat,null);
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'completion-registration-'));
  try{fs.cpSync(fixture,dir,{recursive:true});fs.copyFileSync(path.join(fixture,'package.json.fixture'),path.join(dir,'package.json'));const hook=installCompletionCompat(dir);try{assert.equal(hook.wasApplied(),false);}finally{hook.deregister();}}finally{fs.rmSync(dir,{recursive:true});}
 });
 test('unapproved version, omitted target and tampered target reject at registration',async()=>{
  const {installCompletionCompat}=await compatibility;const dir=fs.mkdtempSync(path.join(os.tmpdir(),'completion-native-'));
  try{
   fs.cpSync(fixture,dir,{recursive:true});fs.copyFileSync(path.join(fixture,'package.json.fixture'),path.join(dir,'package.json'));const metadata=JSON.parse(fs.readFileSync(path.join(dir,'package.json'),'utf8'));metadata.version='44.216.0';fs.writeFileSync(path.join(dir,'package.json'),JSON.stringify(metadata));assert.throws(()=>installCompletionCompat(dir));
   fs.copyFileSync(path.join(fixture,'package.json.fixture'),path.join(dir,'package.json'));fs.appendFileSync(path.join(dir,loggerPath),'\n');assert.throws(()=>installCompletionCompat(dir));
   fs.copyFileSync(path.join(fixture,loggerPath),path.join(dir,loggerPath));fs.unlinkSync(path.join(dir,branchPath));assert.throws(()=>installCompletionCompat(dir));
  }finally{fs.rmSync(dir,{recursive:true});}
 });
 test('production loader order registers before other preloads and checks applied after guards',()=>{
  const scripts=path.dirname(compatPath);const runner=fs.readFileSync(path.join(scripts,'renovate-security-runner.sh'),'utf8');
  const imports=[...runner.matchAll(/--import "\$\{compat_dir%\/\}\/([^\"]+)"/g)].map(x=>x[1]);
  assert.deepEqual(imports,['renovate-completion-compat.mjs','renovate-security-compat.mjs']);
  const completion=fs.readFileSync(compatPath,'utf8');const activation=completion.slice(completion.lastIndexOf("if (process.argv[1]"));assert.ok(!activation.includes('await '));assert.ok(activation.includes('installCompletionCompat('));
  const security=fs.readFileSync(path.join(scripts,'renovate-security-compat.mjs'),'utf8');const startIndex=security.lastIndexOf("if (process.argv[1]");const body=security.slice(startIndex);
  assert.ok(body.indexOf('assert.ok(activeCompletionCompat,')<body.indexOf('installSecurityCompat('));
  assert.ok(body.indexOf('installNpmCompat(')<body.indexOf('await activeCompletionCompat.preload()'));
  assert.ok(body.indexOf('await activeCompletionCompat.preload()')<body.indexOf('assert.ok(activeCompletionCompat.wasApplied()'));
 });
