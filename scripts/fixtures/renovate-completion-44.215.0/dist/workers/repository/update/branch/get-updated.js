import { WORKER_FILE_UPDATE_FAILED } from "../../../../constants/error-messages.js";
import { GlobalConfig } from "../../../../config/global.js";
import { coerceArray } from "../../../../util/array.js";
import { coerceString } from "../../../../util/string.js";
import { logger } from "../../../../logger/index.js";
import { getFile } from "../../../../util/git/index.js";
import { extractPackageFile, get } from "../../../../modules/manager/index.js";
import { normalizeDepNames } from "../../extract/manager-files.js";
import { doAutoReplace } from "./auto-replace.js";
import { isNonEmptyArray, isObject } from "@sindresorhus/is";
// lib/workers/repository/update/branch/get-updated.ts
async function getFileContent(updatedFileContents, filePath, config) {
	let fileContent = updatedFileContents[filePath];
	if (!fileContent) fileContent = await getFile(filePath, config.reuseExistingBranch ? config.branchName : config.baseBranch);
	return fileContent;
}
function sortPackageFiles(config, manager, packageFiles) {
	const managerPackageFiles = config.packageFiles?.[manager];
	if (!managerPackageFiles) return;
	packageFiles.sort((lhs, rhs) => {
		return managerPackageFiles.findIndex((entry) => entry.packageFile === lhs.path) - managerPackageFiles.findIndex((entry) => entry.packageFile === rhs.path);
	});
}
function hasAny(set, targets) {
	for (const target of targets) if (set.has(target)) return true;
	return false;
}
function getUpdatedLockFileContent(updatedDeps, updatedFileContents) {
	for (const upgrade of updatedDeps) {
		const lockFiles = [upgrade.lockFile, ...coerceArray(upgrade.lockFiles)];
		for (const lockFile of lockFiles) if (lockFile && updatedFileContents[lockFile] !== void 0) return updatedFileContents[lockFile];
	}
}
function removeUnrefreshedLockfileOnlyChanges(packageFiles, updatedDeps, results) {
	const removedLockFiles = [];
	const refreshedFiles = new Set(coerceArray(results).flatMap((result) => result.file ? [result.file.path] : []));
	const lockFiles = new Set(updatedDeps.filter((upgrade) => upgrade.isLockfileOnly).flatMap((upgrade) => [upgrade.lockFile, ...coerceArray(upgrade.lockFiles)]));
	for (let index = packageFiles.length - 1; index >= 0; index -= 1) {
		const path = packageFiles[index].path;
		if (lockFiles.has(path) && !refreshedFiles.has(path)) {
			packageFiles.splice(index, 1);
			removedLockFiles.push(path);
		}
	}
	return removedLockFiles;
}
function reportUnrefreshedLockfileOnlyChanges(removedLockFiles, results, artifactErrors) {
	const artifactErrorFiles = new Set(coerceArray(results).flatMap((result) => result.artifactError ? [result.artifactError.fileName] : []));
	for (const lockFile of removedLockFiles) if (!artifactErrorFiles.has(lockFile)) artifactErrors.push({
		fileName: lockFile,
		stderr: "Lockfile-only update could not be refreshed"
	});
}
function removeSupersededLockFileChanges(packageFiles, updatedDeps, results) {
	const artifactFiles = new Set(coerceArray(results).flatMap((result) => result.file ? [result.file.path] : []));
	const lockFiles = new Set(updatedDeps.filter((upgrade) => upgrade.isLockfileOnly).flatMap((upgrade) => [upgrade.lockFile, ...coerceArray(upgrade.lockFiles)]));
	for (let index = packageFiles.length - 1; index >= 0; index -= 1) {
		const path = packageFiles[index].path;
		if (lockFiles.has(path) && artifactFiles.has(path)) packageFiles.splice(index, 1);
	}
}
function getManagersForPackageFiles(packageFiles, managerPackageFiles) {
	const packageFileNames = packageFiles.map((packageFile) => packageFile.path);
	return new Set(Object.keys(managerPackageFiles).filter((manager) => hasAny(managerPackageFiles[manager], packageFileNames)));
}
function getPackageFilesForManager(packageFiles, managerPackageFiles) {
	return packageFiles.filter((packageFile) => managerPackageFiles.has(packageFile.path));
}
async function getUpdatedPackageFiles(config) {
	logger.trace({ config });
	const reuseExistingBranch = config.reuseExistingBranch;
	logger.debug(`manager.getUpdatedPackageFiles() reuseExistingBranch=${reuseExistingBranch}`);
	let updatedFileContents = {};
	const nonUpdatedFileContents = {};
	const managerPackageFiles = {};
	const packageFileUpdatedDeps = {};
	const lockFileMaintenanceFiles = [];
	let firstUpdate = true;
	for (const upgrade of config.upgrades) {
		const manager = upgrade.manager;
		const packageFile = upgrade.packageFile;
		const depName = upgrade.depName;
		const newVersion = upgrade.newVersion;
		const currentVersion = upgrade.currentVersion;
		const updateLockedDependency = get(manager, "updateLockedDependency");
		managerPackageFiles[manager] ??= /* @__PURE__ */ new Set();
		managerPackageFiles[manager].add(packageFile);
		packageFileUpdatedDeps[packageFile] ??= [];
		packageFileUpdatedDeps[packageFile].push({ ...upgrade });
		const packageFileContent = await getFileContent(updatedFileContents, packageFile, config);
		let lockFileContent = null;
		const lockFile = upgrade.lockFile ?? upgrade.lockFiles?.[0] ?? "";
		if (lockFile) lockFileContent = await getFileContent(updatedFileContents, lockFile, config);
		if (reuseExistingBranch && (!packageFileContent || !!lockFile && !lockFileContent)) {
			logger.debug({
				packageFile,
				depName
			}, "Rebasing branch after file not found");
			return getUpdatedPackageFiles({
				...config,
				reuseExistingBranch: false
			});
		}
		if (upgrade.updateType === "lockFileMaintenance") lockFileMaintenanceFiles.push(packageFile);
		else if (upgrade.isRemediation) {
			const { status, files } = await updateLockedDependency({
				...upgrade,
				depName,
				newVersion,
				currentVersion,
				packageFile,
				packageFileContent,
				lockFile,
				lockFileContent,
				allowParentUpdates: true,
				allowHigherOrRemoved: true
			});
			if (reuseExistingBranch && status !== "already-updated") {
				logger.debug({
					lockFile,
					depName,
					status
				}, "Need to retry branch as it is not already up-to-date");
				return getUpdatedPackageFiles({
					...config,
					reuseExistingBranch: false
				});
			}
			if (files) {
				updatedFileContents = {
					...updatedFileContents,
					...files
				};
				Object.keys(files).forEach((file) => delete nonUpdatedFileContents[file]);
			}
			if (status === "update-failed" || status === "unsupported") upgrade.remediationNotPossible = true;
		} else if (upgrade.isLockfileUpdate) {
			if (updateLockedDependency) {
				const { status, files } = await updateLockedDependency({
					...upgrade,
					depName,
					newVersion,
					currentVersion,
					packageFile,
					packageFileContent,
					lockFile,
					lockFileContent,
					allowParentUpdates: false
				});
				if (status === "unsupported") {
					if (!updatedFileContents[packageFile]) nonUpdatedFileContents[packageFile] = packageFileContent;
				} else if (status === "already-updated") logger.debug(`Upgrade of ${depName} to ${newVersion} is already done in existing branch`);
				else {
					if (reuseExistingBranch) {
						logger.debug({
							lockFile,
							depName,
							status
						}, "Need to retry branch as upgrade requirements are not mets");
						return getUpdatedPackageFiles({
							...config,
							reuseExistingBranch: false
						});
					}
					if (files) {
						updatedFileContents = {
							...updatedFileContents,
							...files
						};
						Object.keys(files).forEach((file) => delete nonUpdatedFileContents[file]);
					}
				}
			} else {
				logger.debug({ manager }, "isLockFileUpdate without updateLockedDependency");
				if (!updatedFileContents[packageFile]) nonUpdatedFileContents[packageFile] = packageFileContent;
			}
		} else {
			const updateDependency = get(manager, "updateDependency");
			if (!updateDependency) {
				let res = await doAutoReplace(upgrade, packageFileContent, reuseExistingBranch, firstUpdate);
				firstUpdate = false;
				if (res) {
					res = await applyManagerBumpPackageVersion(res, upgrade);
					if (res === packageFileContent) logger.debug({
						packageFile,
						depName
					}, "No content changed");
					else {
						logger.debug({
							packageFile,
							depName
						}, "Contents updated");
						updatedFileContents[packageFile] = res;
						delete nonUpdatedFileContents[packageFile];
					}
					continue;
				} else if (reuseExistingBranch) return getUpdatedPackageFiles({
					...config,
					reuseExistingBranch: false
				});
				logger.error({
					packageFile,
					depName
				}, "Could not autoReplace");
				throw new Error(WORKER_FILE_UPDATE_FAILED);
			}
			const result = await updateDependency({
				packageFile,
				fileContent: packageFileContent,
				upgrade
			});
			const { content, updatedOutOfBand } = isObject(result) ? result : {
				content: result,
				updatedOutOfBand: false
			};
			const newContent = await applyManagerBumpPackageVersion(content, upgrade);
			if (!newContent) {
				if (reuseExistingBranch) {
					logger.debug({
						packageFile,
						depName
					}, "Rebasing branch after error updating content");
					return getUpdatedPackageFiles({
						...config,
						reuseExistingBranch: false
					});
				}
				logger.debug({
					existingContent: packageFileContent,
					config: upgrade
				}, "Error updating file");
				throw new Error(WORKER_FILE_UPDATE_FAILED);
			}
			if (newContent !== packageFileContent) {
				if (reuseExistingBranch) {
					logger.debug({
						packageFile,
						depName
					}, "Need to update package file so will rebase first");
					return getUpdatedPackageFiles({
						...config,
						reuseExistingBranch: false
					});
				}
				logger.debug(`Updating ${depName} in ${coerceString(packageFile, lockFile)}`);
				updatedFileContents[packageFile] = newContent;
				delete nonUpdatedFileContents[packageFile];
			} else if (updatedOutOfBand) {
				updatedFileContents[packageFile] = newContent;
				delete nonUpdatedFileContents[packageFile];
			}
		}
	}
	const updatedPackageFiles = Object.keys(updatedFileContents).map((name) => ({
		type: "addition",
		path: name,
		contents: updatedFileContents[name]
	}));
	const updatedArtifacts = [];
	const artifactErrors = [];
	const artifactNotices = [];
	if (isNonEmptyArray(updatedPackageFiles)) {
		logger.debug("updateArtifacts for updatedPackageFiles");
		const updatedPackageFileManagers = getManagersForPackageFiles(updatedPackageFiles, managerPackageFiles);
		for (const manager of updatedPackageFileManagers) {
			const packageFilesForManager = getPackageFilesForManager(updatedPackageFiles, managerPackageFiles[manager]);
			sortPackageFiles(config, manager, packageFilesForManager);
			for (const packageFile of packageFilesForManager) {
				const updatedDeps = packageFileUpdatedDeps[packageFile.path];
				const results = await managerUpdateArtifacts(manager, {
					packageFileName: packageFile.path,
					updatedDeps,
					newPackageFileContent: packageFile.contents.toString(),
					newLockFileContent: getUpdatedLockFileContent(updatedDeps, updatedFileContents),
					config: patchConfigForArtifactsUpdate(config, manager, packageFile.path)
				});
				if (manager === "mise") {
					reportUnrefreshedLockfileOnlyChanges(removeUnrefreshedLockfileOnlyChanges(updatedPackageFiles, updatedDeps, results), results, artifactErrors);
					removeSupersededLockFileChanges(updatedPackageFiles, updatedDeps, results);
				}
				processUpdateArtifactResults(results, updatedArtifacts, artifactErrors, artifactNotices);
				if (isNonEmptyArray(results)) await checkForPendingVersions(manager, packageFile.path, packageFile.contents.toString(), updatedDeps, artifactErrors, config);
			}
		}
	}
	const nonUpdatedPackageFiles = Object.keys(nonUpdatedFileContents).map((name) => ({
		type: "addition",
		path: name,
		contents: nonUpdatedFileContents[name]
	}));
	if (isNonEmptyArray(nonUpdatedPackageFiles)) {
		logger.debug("updateArtifacts for nonUpdatedPackageFiles");
		const nonUpdatedPackageFileManagers = getManagersForPackageFiles(nonUpdatedPackageFiles, managerPackageFiles);
		for (const manager of nonUpdatedPackageFileManagers) {
			const packageFilesForManager = getPackageFilesForManager(nonUpdatedPackageFiles, managerPackageFiles[manager]);
			sortPackageFiles(config, manager, packageFilesForManager);
			for (const packageFile of packageFilesForManager) {
				const updatedDeps = packageFileUpdatedDeps[packageFile.path];
				const results = await managerUpdateArtifacts(manager, {
					packageFileName: packageFile.path,
					updatedDeps,
					newPackageFileContent: packageFile.contents.toString(),
					newLockFileContent: getUpdatedLockFileContent(updatedDeps, updatedFileContents),
					config: patchConfigForArtifactsUpdate(config, manager, packageFile.path)
				});
				processUpdateArtifactResults(results, updatedArtifacts, artifactErrors, artifactNotices);
				if (isNonEmptyArray(results)) {
					updatedPackageFiles.push(packageFile);
					await checkForPendingVersions(manager, packageFile.path, packageFile.contents.toString(), updatedDeps, artifactErrors, config);
				}
			}
		}
	}
	if (!reuseExistingBranch) {
		const lockFileMaintenancePackageFiles = lockFileMaintenanceFiles.map((name) => ({ path: name }));
		if (isNonEmptyArray(lockFileMaintenanceFiles)) {
			logger.debug("updateArtifacts for lockFileMaintenanceFiles");
			const lockFileMaintenanceManagers = getManagersForPackageFiles(lockFileMaintenancePackageFiles, managerPackageFiles);
			for (const manager of lockFileMaintenanceManagers) {
				const packageFilesForManager = getPackageFilesForManager(lockFileMaintenancePackageFiles, managerPackageFiles[manager]);
				sortPackageFiles(config, manager, packageFilesForManager);
				for (const packageFile of packageFilesForManager) {
					const contents = updatedFileContents[packageFile.path] || await getFile(packageFile.path, config.baseBranch);
					processUpdateArtifactResults(await managerUpdateArtifacts(manager, {
						packageFileName: packageFile.path,
						updatedDeps: [],
						newPackageFileContent: contents,
						config: patchConfigForArtifactsUpdate(config, manager, packageFile.path)
					}), updatedArtifacts, artifactErrors, artifactNotices);
				}
			}
		}
	}
	return {
		reuseExistingBranch,
		updatedPackageFiles,
		updatedArtifacts,
		artifactErrors,
		artifactNotices
	};
}
function patchConfigForArtifactsUpdate(config, manager, packageFileName) {
	const updatedConfig = { ...config };
	delete updatedConfig.lockFiles;
	if (isNonEmptyArray(updatedConfig.packageFiles?.[manager])) {
		const packageFile = (updatedConfig.packageFiles?.[manager]).find((p) => p.packageFile === packageFileName);
		if (packageFile && isNonEmptyArray(packageFile.lockFiles)) updatedConfig.lockFiles = packageFile.lockFiles;
	}
	return updatedConfig;
}
async function managerUpdateArtifacts(manager, updateArtifact) {
	const updateArtifacts = get(manager, "updateArtifacts");
	if (!updateArtifacts) return null;
	if (updateArtifact.config.skipArtifactsUpdate) {
		logger.debug({
			manager,
			packageFileName: updateArtifact.packageFileName
		}, "Skipping artifact update");
		return null;
	}
	return await updateArtifacts(updateArtifact);
}
function processUpdateArtifactResults(results, updatedArtifacts, artifactErrors, artifactNotices) {
	if (isNonEmptyArray(results)) for (const res of results) {
		const { file, notice, artifactError } = res;
		if (file) {
			const existingIndex = updatedArtifacts.findIndex((artifact) => artifact.path === file.path);
			if (existingIndex === -1) updatedArtifacts.push(file);
			else updatedArtifacts[existingIndex] = file;
		}
		if (artifactError) artifactErrors.push(artifactError);
		if (notice) artifactNotices.push(notice);
	}
}
/**
* When using Minimum Release Age, and a package manager that doesn't support being told an explicit version to update to (#41624) it is possible that an artifact update leads to a different version of a dependency being used compared to what Renovate is expecting.
*
* We should report these cases more explicitly with an Artifact Error, to allow the reviewers to decide what to do with the changes.
*/
async function checkForPendingVersions(manager, packageFileName, packageFileContent, updatedDeps, artifactErrors, config) {
	const depNameToUpgradeInfo = /* @__PURE__ */ new Map();
	for (const dep of updatedDeps) if (dep.depName && isNonEmptyArray(dep.pendingVersions)) depNameToUpgradeInfo.set(dep.depName, {
		pendingVersions: new Set(dep.pendingVersions),
		newVersion: dep.newVersion
	});
	if (!depNameToUpgradeInfo.size) return;
	const extracted = await extractPackageFile(manager, packageFileContent, packageFileName, config);
	if (!extracted) {
		logger.warn({
			packageFile: packageFileName,
			manager
		}, "Could not re-extract the packageFile after updating it");
		return;
	}
	for (const dep of extracted.deps) {
		normalizeDepNames(dep);
		const { depName } = dep;
		if (!depName) {
			logger.error({
				packageFile: packageFileName,
				manager,
				branchName: config.branchName,
				depName: dep.depName
			}, "No depName found after updating package file");
			throw new Error(WORKER_FILE_UPDATE_FAILED);
		}
		const upgradeInfo = depNameToUpgradeInfo.get(depName);
		if (!upgradeInfo) continue;
		const resolvedVersion = dep.lockedVersion ?? dep.newVersion ?? dep.currentVersion ?? dep.currentValue;
		if (!resolvedVersion) {
			logger.warn({
				packageFile: packageFileName,
				manager,
				branchName: config.branchName,
				depName
			}, "Could not determine resolved version after updating package file; skipping pending-version check");
			continue;
		}
		if (resolvedVersion && upgradeInfo.pendingVersions.has(resolvedVersion)) {
			const expectedVersion = upgradeInfo.newVersion;
			/* v8 ignore next if -- should not happen */
			if (!expectedVersion) {
				logger.error({
					packageFile: packageFileName,
					manager,
					branchName: config.branchName,
					depName,
					newVersion: resolvedVersion,
					expectedVersion
				}, "No expectedVersion found after updating package file");
				continue;
			}
			if (config.minimumReleaseAgeBehaviour === "timestamp-optional") {
				logger.once.warn({
					packageFileName,
					depName,
					expectedVersion,
					resolvedVersion
				}, "Artifact error would be reported due to a pending version in use which hasn't passed Minimum Release Age, but as we're running with minimumReleaseAgeBehaviour=timestamp-optional, proceeding. See debug logs for more information");
				continue;
			}
			logger.debug({
				packageFileName,
				depName,
				expectedVersion,
				resolvedVersion
			}, "Artifact update introduced a pending version");
			let stderr = `Artifact update for ${depName} resolved to version ${resolvedVersion}, which is a pending version that has not yet passed the Minimum Release Age threshold.`;
			stderr += `\nRenovate was attempting to update to ${expectedVersion}`;
			stderr += `\nThis is (likely) not a bug in Renovate, but due to the way your project pins dependencies, _and_ how Renovate calls your package manager to update them.\nUntil Renovate supports specifying an exact update to your package manager (https://github.com/renovatebot/renovate/issues/41624), it is recommended to directly pin your dependencies (with \`rangeStrategy=pin\` for apps, or \`rangeStrategy=widen\` for libraries)\nSee also: ${GlobalConfig.get("productLinks").documentation}dependency-pinning/`;
			artifactErrors.push({
				fileName: packageFileName,
				stderr
			});
		}
	}
}
async function applyManagerBumpPackageVersion(packageFileContent, upgrade) {
	const bumpPackageVersion = get(upgrade.manager, "bumpPackageVersion");
	if (!bumpPackageVersion || !packageFileContent || !upgrade.bumpVersion || !upgrade.packageFileVersion) return packageFileContent;
	return (await bumpPackageVersion(packageFileContent, upgrade.packageFileVersion, upgrade.bumpVersion, upgrade.packageFile)).bumpedContent;
}
export { getUpdatedPackageFiles };

//# sourceMappingURL=get-updated.js.map