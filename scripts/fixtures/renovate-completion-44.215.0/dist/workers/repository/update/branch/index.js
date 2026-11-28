import { MANAGER_LOCKFILE_ERROR, REPOSITORY_CHANGED } from "../../../../constants/error-messages.js";
import { GlobalConfig } from "../../../../config/global.js";
import { coerceArray } from "../../../../util/array.js";
import { logger, removeMeta } from "../../../../logger/index.js";
import { compile } from "../../../../util/template/index.js";
import { isScheduledNow } from "./schedule.js";
import { ExternalHostError } from "../../../../types/errors/external-host-error.js";
import { getCount, isLimitReached } from "../../../global/limits.js";
import { emojify } from "../../../../util/emoji.js";
import { scm } from "../../../../modules/platform/scm.js";
import { platform } from "../../../../modules/platform/index.js";
import { compileCommitBody, compileCommitTrailers } from "../../model/commit-body.js";
import { ensureComment, ensureCommentRemoval } from "../../../../modules/platform/comment.js";
import { embedChangelogs } from "../../changelog/index.js";
import { computeInternalChecksStatus, setConfidence, setStability } from "./status-checks.js";
import { ensurePr, getPlatformPrOptions } from "../pr/index.js";
import { updateActionsLockfile } from "../../../../modules/manager/github-actions/artifacts.js";
import { getAdditionalFiles } from "../../../../modules/manager/npm/post-update/index.js";
import { checkAutoMerge } from "../pr/automerge.js";
import { setArtifactErrorStatus } from "./artifacts.js";
import { tryBranchAutomerge } from "./automerge.js";
import { bumpVersions } from "./bump-versions.js";
import { findClosedPrForModifiedBranch, prAlreadyExisted, rebaseCheck, userChangedTargetBranch } from "./check-existing.js";
import { commitFilesToBranch } from "./commit.js";
import { handleBranchError } from "./errors.js";
import executePostUpgradeCommands from "./execute-post-upgrade-commands.js";
import { getUpdatedPackageFiles } from "./get-updated.js";
import { handleClosedPr, handleModifiedPr } from "./handle-existing.js";
import { prBlockedByToResult } from "./pr-blocked-by.js";
import { decideBranchReuse, hasKeepUpdatedLabel, shouldReuseExistingBranch } from "./reuse.js";
import { DateTime } from "luxon";
// lib/workers/repository/update/branch/index.ts
async function setBranchStatusChecks(config) {
	await setStability(config);
	await setConfidence(config);
}
async function deleteBranchSilently(branchName) {
	try {
		await scm.deleteBranch(branchName);
	} catch (err) {
		/* v8 ignore next -- needs test */
		logger.debug({
			branchName,
			err
		}, "Branch auto-remove failed");
	}
}
async function processBranch(branchConfig, forceRebase = false) {
	let commitSha = null;
	let config = { ...branchConfig };
	logger.trace({ config }, "processBranch()");
	let branchExists = await scm.branchExists(config.branchName);
	const dependencyDashboardCheck = config.dependencyDashboardChecks?.[config.branchName];
	const unpendRequested = dependencyDashboardCheck === "unpend" || dependencyDashboardCheck === "global-config";
	let updatesVerified = false;
	if (!branchExists && config.branchPrefix !== config.branchPrefixOld) {
		const branchName = config.branchName.replace(config.branchPrefix, config.branchPrefixOld);
		branchExists = await scm.branchExists(branchName);
		// v8 ignore else -- TODO: add test #40625
		if (branchExists) {
			config.branchName = branchName;
			logger.debug("Found existing branch with branchPrefixOld");
		}
	}
	if (!branchExists && branchConfig.minimumGroupSize && branchConfig.minimumGroupSize > branchConfig.upgrades.length && !dependencyDashboardCheck) {
		logger.debug(`Skipping branch creation as minimumGroupSize: ${branchConfig.minimumGroupSize} is not met`);
		return {
			branchExists: false,
			result: "minimum-group-size-not-met"
		};
	}
	let branchPr = await platform.getBranchPr(config.branchName, config.baseBranch);
	logger.debug(`branchExists=${branchExists}`);
	logger.debug(`dependencyDashboardCheck=${dependencyDashboardCheck}`);
	if (branchPr) {
		config.rebaseRequested = await rebaseCheck(config, branchPr);
		logger.debug(`PR rebase requested=${config.rebaseRequested}`);
	}
	const pendingRebaseTopic = emojify(":warning: Rebase not applied");
	const artifactErrorTopic = emojify(":warning: Artifact update problem");
	const artifactNoticeTopic = emojify(":information_source: Artifact update notice");
	try {
		const existingPr = !branchPr || config.automerge ? await prAlreadyExisted(config) : void 0;
		if (existingPr?.state === "merged") {
			logger.debug(`Matching PR #${existingPr.number} was merged previously`);
			if (config.automerge) {
				logger.debug("Disabling automerge because PR was merged previously");
				config.automerge = false;
				config.automergedPreviously = true;
			}
		} else if (!branchPr && existingPr && !dependencyDashboardCheck) {
			logger.debug({ prTitle: config.prTitle }, `Closed PR #${existingPr.number} already exists. Skipping branch.`);
			await handleClosedPr(config, existingPr);
			return {
				branchExists: false,
				prNo: existingPr.number,
				result: "already-existed"
			};
		}
		if (!branchExists && branchConfig.pendingChecks && !unpendRequested) {
			logger.debug(`Branch ${config.branchName} creation is disabled because internalChecksFilter was not met`);
			return {
				branchExists: false,
				result: "pending"
			};
		}
		if (!branchExists) {
			if (config.mode === "silent" && !dependencyDashboardCheck) {
				logger.debug(`Branch ${config.branchName} creation is disabled because mode=silent`);
				return {
					branchExists,
					result: "needs-approval"
				};
			}
			if (config.dependencyDashboardApproval && !dependencyDashboardCheck) {
				logger.debug(`Branch ${config.branchName} creation is disabled because dependencyDashboardApproval=true`);
				return {
					branchExists,
					result: "needs-approval"
				};
			}
		}
		logger.debug(`Open PR Count: ${getCount("ConcurrentPRs")}, Existing Branch Count: ${getCount("Branches")}, Hourly PR Count: ${getCount("HourlyPRs")}, Hourly Commit Count: ${getCount("HourlyCommits")}`);
		if (!branchExists && isLimitReached("Branches", branchConfig) && !dependencyDashboardCheck) {
			logger.debug("Reached branch limit - skipping branch creation");
			return {
				branchExists,
				result: "branch-limit-reached"
			};
		}
		if (!config.rebaseRequested && isLimitReached("Commits") && !dependencyDashboardCheck && !config.isVulnerabilityAlert) {
			logger.debug("Reached commits per run limit - skipping branch");
			return {
				branchExists,
				prNo: branchPr?.number,
				result: "commit-per-run-limit-reached"
			};
		}
		if (!config.rebaseRequested && isLimitReached("HourlyCommits", branchConfig) && !dependencyDashboardCheck && !config.isVulnerabilityAlert) {
			logger.debug("Reached hourly commits limit - skipping branch");
			return {
				branchExists,
				prNo: branchPr?.number,
				result: "commit-hourly-limit-reached"
			};
		}
		if (branchExists) {
			config.stopUpdating = branchPr?.labels?.includes(config.stopUpdatingLabel);
			const prRebaseChecked = !!branchPr?.bodyStruct?.rebaseRequested;
			if (!dependencyDashboardCheck && !prRebaseChecked && config.stopUpdating) {
				logger.info("Branch updating is skipped because stopUpdatingLabel is present in config");
				return {
					branchExists: true,
					prNo: branchPr?.number,
					result: "no-work"
				};
			}
			if (!unpendRequested && config.pendingChecks) {
				if (config.rebaseRequested) {
					logger.info("Branch updating is skipped because internalChecksFilter was not met, despite the requested rebase");
					if (branchPr) {
						const content = "This branch has not been rebased, as its update has not yet met the internal checks configured for it, such as `minimumReleaseAge`. Rebasing it now would add a dependency version which is still within its configured observation period.\n\nRenovate will rebase this branch once its update has met those checks.";
						if (GlobalConfig.get("dryRun")) logger.info(`DRY-RUN: Would ensure pending rebase comment in PR #${branchPr.number}`);
						else await ensureComment({
							number: branchPr.number,
							topic: pendingRebaseTopic,
							content
						});
					}
				} else logger.info("Branch updating is skipped because internalChecksFilter was not met");
				return {
					branchExists: true,
					prNo: branchPr?.number,
					result: "pending"
				};
			}
			logger.debug("Checking if PR has been edited");
			const branchIsModified = await scm.isBranchModified(config.branchName, config.baseBranch);
			if (branchPr) {
				logger.debug(`Found existing branch PR #${branchPr.number}`);
				if (branchPr.state !== "open") {
					logger.debug("PR has been closed or merged since this run started - aborting");
					throw new Error(REPOSITORY_CHANGED);
				}
				if (branchIsModified || userChangedTargetBranch(branchPr)) {
					logger.debug(`PR has been edited, PrNo:${branchPr.number}`);
					await handleModifiedPr(config, branchPr);
					if (!(!!dependencyDashboardCheck || config.rebaseRequested)) return {
						branchExists,
						prNo: branchPr.number,
						result: "pr-edited"
					};
				}
			} else if (branchIsModified && !dependencyDashboardCheck && !await findClosedPrForModifiedBranch(config)) return {
				branchExists,
				result: "pr-edited"
			};
		}
		config.isScheduledNow = isScheduledNow(config, "schedule");
		if (!config.isScheduledNow && !dependencyDashboardCheck) {
			if (!branchExists) {
				logger.debug("Skipping branch creation as not within schedule");
				return {
					branchExists,
					result: "not-scheduled"
				};
			}
			if (config.updateNotScheduled === false && !config.rebaseRequested) {
				logger.debug("Skipping branch update as not within schedule");
				return {
					branchExists,
					prNo: branchPr?.number,
					result: "update-not-scheduled"
				};
			}
			if (!branchPr && !(config.automerge && config.automergeType === "branch")) {
				logger.debug("Skipping PR creation out of schedule");
				return {
					branchExists,
					result: "not-scheduled"
				};
			}
			logger.debug("Branch + PR exists but is not scheduled -- will update if necessary");
		}
		const internalChecksStatus = await computeInternalChecksStatus(config);
		if (internalChecksStatus) {
			config.stabilityStatus = internalChecksStatus.stabilityStatus;
			config.confidenceStatus = internalChecksStatus.confidenceStatus;
			if (!dependencyDashboardCheck && !branchExists && config.stabilityStatus === "yellow" && ["not-pending", "status-success"].includes(config.prCreation)) {
				logger.debug("Skipping branch creation due to internal status checks not met");
				return {
					branchExists,
					result: "pending"
				};
			}
		}
		const reuseDecision = decideBranchReuse({
			config,
			branchPr,
			branchExists,
			dependencyDashboardCheck,
			forceRebase
		});
		const { userRebaseRequested } = reuseDecision;
		if (reuseDecision.action === "skip-update") return {
			branchExists,
			prNo: branchPr?.number,
			result: "no-work"
		};
		if (reuseDecision.action === "no-reuse") config.reuseExistingBranch = false;
		else if (reuseDecision.action === "check-reuse") config = await shouldReuseExistingBranch(config, branchPr);
		logger.debug(`Using reuseExistingBranch: ${config.reuseExistingBranch}`);
		if (!(config.reuseExistingBranch && config.cacheFingerprintMatch === "matched")) {
			await scm.checkoutBranch(config.baseBranch);
			const res = await getUpdatedPackageFiles(config);
			if (res.artifactErrors && config.artifactErrors) res.artifactErrors = config.artifactErrors.concat(res.artifactErrors);
			config = {
				...config,
				...res
			};
			if (config.updatedPackageFiles?.length) {
				logger.debug(`Updated ${config.updatedPackageFiles.length} package files`);
				if (config.reuseExistingBranch && !forceRebase) {
					logger.debug("Existing branch needs updating. Restarting processBranch() with a clean branch");
					return processBranch(branchConfig, true);
				}
			} else logger.debug("No package files need updating");
			const additionalFiles = await getAdditionalFiles(config, branchConfig.packageFiles);
			config.artifactErrors = coerceArray(config.artifactErrors).concat(additionalFiles.artifactErrors);
			config.artifactNotices = coerceArray(config.artifactNotices).concat(coerceArray(additionalFiles.artifactNotices));
			config.updatedArtifacts = coerceArray(config.updatedArtifacts).concat(additionalFiles.updatedArtifacts);
			const actionsLockfile = await updateActionsLockfile(config, branchConfig.packageFiles);
			config.artifactErrors = config.artifactErrors.concat(actionsLockfile.artifactErrors);
			config.updatedArtifacts = config.updatedArtifacts.concat(actionsLockfile.updatedArtifacts);
			if (config.updatedArtifacts?.length) {
				logger.debug({ updatedArtifacts: config.updatedArtifacts.map((f) => f.type === "deletion" ? `${f.path} (delete)` : f.path) }, `Updated ${config.updatedArtifacts.length} lock files`);
				if (config.reuseExistingBranch && !forceRebase) {
					logger.debug("Existing branch needs updating. Restarting processBranch() with a clean branch");
					return processBranch(branchConfig, true);
				}
			} else logger.debug("No updated lock files in branch");
			await embedChangelogs({
				upgrades: config.upgrades,
				stage: "branch"
			});
			const postUpgradeCommandResults = await executePostUpgradeCommands(config);
			if (postUpgradeCommandResults !== null) {
				const { updatedArtifacts, artifactErrors } = postUpgradeCommandResults;
				config.updatedArtifacts = updatedArtifacts;
				config.artifactErrors = artifactErrors;
			}
			await bumpVersions(config);
			removeMeta(["dep"]);
			if (config.artifactErrors?.length) {
				if (config.releaseTimestamp) {
					logger.debug(`Branch timestamp: ${config.releaseTimestamp}`);
					if (DateTime.fromISO(config.releaseTimestamp).plus({ hours: 2 }) < DateTime.local()) logger.debug("PR is older than 2 hours, raise PR with lock file errors");
					else if (branchExists) logger.debug("PR is less than 2 hours old but branchExists so updating anyway");
					else {
						logger.debug("PR is less than 2 hours old - raise error instead of PR");
						throw new Error(MANAGER_LOCKFILE_ERROR);
					}
				} else logger.debug("PR has no releaseTimestamp");
			} else if (config.updatedArtifacts?.length && branchPr) {
				if (GlobalConfig.get("dryRun")) logger.info(`DRY-RUN: Would ensure comment removal in PR #${branchPr.number}`);
				else {
					await ensureCommentRemoval({
						type: "by-topic",
						number: branchPr.number,
						topic: artifactErrorTopic
					});
					// v8 ignore else -- TODO: add test #40625
					if (!config.artifactNotices?.length) await ensureCommentRemoval({
						type: "by-topic",
						number: branchPr.number,
						topic: artifactNoticeTopic
					});
				}
			}
			const forcedManually = userRebaseRequested || !branchExists;
			config.isConflicted ??= branchExists && await scm.isBranchConflicted(config.baseBranch, config.branchName);
			config.forceCommit = forcedManually || config.isConflicted;
			config.commitMessage = compileCommitBody(config, config.commitMessage, {
				...config,
				logJSON: config.upgrades[0].logJSON,
				releases: config.upgrades[0].releases
			});
			config.commitTrailers = compileCommitTrailers(config, config);
			commitSha = await commitFilesToBranch(config);
			await scm.checkoutBranch(config.baseBranch);
			updatesVerified = true;
			if (commitSha) await setArtifactErrorStatus(config);
			if (commitSha && branchPr && config.rebaseRequested) {
				if (GlobalConfig.get("dryRun")) logger.info(`DRY-RUN: Would ensure pending rebase comment removal in PR #${branchPr.number}`);
				else await ensureCommentRemoval({
					type: "by-topic",
					number: branchPr.number,
					topic: pendingRebaseTopic
				});
			}
		}
		if (branchPr) {
			const platformPrOptions = getPlatformPrOptions(config);
			if (commitSha && platformPrOptions.usePlatformAutomerge && platform.reattemptPlatformAutomerge) {
				if (GlobalConfig.get("dryRun")) logger.info(`DRY-RUN: Would reattempt platform automerge for PR #${branchPr.number}`);
				else await platform.reattemptPlatformAutomerge({
					number: branchPr.number,
					platformPrOptions
				});
			}
			// v8 ignore else -- TODO: add test #40625
			if (platform.refreshPr) await platform.refreshPr(branchPr.number);
		}
		if (!commitSha && !branchExists) return {
			branchExists,
			result: "no-work"
		};
		if (commitSha) {
			const action = branchExists ? "updated" : "created";
			logger.info({ commitSha }, `Branch ${action}`);
		}
		await setBranchStatusChecks(config);
		if (!branchPr && !config.artifactErrors?.length && !userRebaseRequested && commitSha && config.prCreation !== "immediate") {
			logger.debug(`Branch status pending, current sha: ${commitSha}`);
			return {
				branchExists: true,
				updatesVerified,
				result: "pending",
				commitSha
			};
		}
		const canAutomerge = !commitSha || config.ignoreTests;
		if (!config.artifactErrors?.length && canAutomerge) {
			const mergeStatus = await tryBranchAutomerge(config, branchPr);
			logger.debug(`mergeStatus=${mergeStatus}`);
			if (mergeStatus === "automerged") {
				if (GlobalConfig.get("dryRun")) logger.info(`DRY-RUN: Would delete branch${config.branchName}`);
				else await deleteBranchSilently(config.branchName);
				logger.debug("Branch is automerged - returning");
				return {
					branchExists: false,
					result: "automerged"
				};
			}
			if (mergeStatus === "off schedule") {
				if (userRebaseRequested) config.forcePr = true;
				else {
					logger.debug("Branch cannot automerge now because automergeSchedule is off schedule - skipping");
					return {
						branchExists,
						result: "not-scheduled",
						commitSha
					};
				}
			}
			if (mergeStatus === "stale" && ["conflicted", "never"].includes(config.rebaseWhen) && 
			/* v8 ignore next -- needs test */
			!hasKeepUpdatedLabel(config, branchPr)) {
				logger.warn("Branch cannot automerge because it is behind base branch and rebaseWhen setting disallows rebasing - raising a PR instead");
				config.forcePr = true;
				config.branchAutomergeFailureMessage = mergeStatus;
			}
			if (mergeStatus === "automerge aborted - merge queue" || mergeStatus === "automerge aborted - PR exists" || mergeStatus === "branch status error" || mergeStatus === "failed") {
				logger.debug(`Branch automerge not possible, mergeStatus:${mergeStatus}`);
				config.forcePr = true;
				config.branchAutomergeFailureMessage = mergeStatus;
			}
		}
	} catch (err) {
		return handleBranchError(err, {
			branchExists,
			prNo: branchPr?.number,
			commitSha,
			updatesVerified
		});
	}
	try {
		logger.debug("Ensuring PR");
		logger.debug(`There are ${config.errors.length} errors and ${config.warnings.length} warnings`);
		const ensurePrResult = await ensurePr(config);
		if (ensurePrResult.type === "without-pr") {
			const { prBlockedBy } = ensurePrResult;
			return {
				branchExists,
				prBlockedBy,
				result: prBlockedByToResult(prBlockedBy, config.isVulnerabilityAlert),
				commitSha
			};
		}
		// v8 ignore else -- TODO: add test #40625
		if (ensurePrResult.type === "with-pr") {
			const { pr } = ensurePrResult;
			branchPr = pr;
			await setBranchStatusChecks(config);
			if (commitSha) await setArtifactErrorStatus(config);
			if (config.artifactErrors?.length) {
				logger.warn({ artifactErrors: config.artifactErrors }, "artifactErrors");
				let content = `Renovate failed to update `;
				content += config.artifactErrors.length > 1 ? "artifacts" : "an artifact";
				content += " related to this branch. ";
				content += compile(config.userStrings.artifactErrorWarning, config);
				content += emojify(`\n\n:recycle: Renovate will retry this branch, including artifacts, only when one of the following happens:\n\n`);
				content += " - any of the package files in this branch needs updating, or \n";
				content += " - the branch becomes conflicted, or\n";
				content += " - you click the rebase/retry checkbox if found above, or\n";
				content += " - you rename this PR's title to start with \"rebase!\" to trigger it manually";
				content += "\n\nThe artifact failure details are included below:\n\n";
				config.artifactErrors.forEach((error) => {
					content += `##### File name: ${error.fileName}\n\n`;
					content += `\`\`\`\n${error.stderr}\n\`\`\`\n\n`;
				});
				content = platform.massageMarkdown(content, config.rebaseLabel);
				// v8 ignore else -- TODO: add test #40625
				if (!(config.suppressNotifications.includes("artifactErrors") || config.suppressNotifications.includes("lockFileErrors"))) {
					if (GlobalConfig.get("dryRun")) logger.info(`DRY-RUN: Would ensure lock file error comment in PR #${pr.number}`);
					else await ensureComment({
						number: pr.number,
						topic: artifactErrorTopic,
						content
					});
				}
			} else {
				if (config.artifactNotices?.length) {
					const contentLines = [];
					for (const notice of config.artifactNotices) {
						contentLines.push(`##### File name: ${notice.file}`);
						contentLines.push(notice.message);
					}
					const content = contentLines.join("\n\n");
					await ensureComment({
						number: pr.number,
						topic: artifactNoticeTopic,
						content
					});
				}
				if (config.automerge) {
					logger.debug("PR is configured for automerge");
					// v8 ignore else -- TODO: add test #40625
					if (config.ignoreTests === true || !commitSha) {
						logger.debug("checking auto-merge");
						if ((await checkAutoMerge(pr, config))?.automerged) return {
							branchExists,
							result: "automerged",
							commitSha
						};
					}
				} else logger.debug("PR is not configured for automerge");
			}
		}
	} catch (err) {
		/* v8 ignore if -- needs test */
		if (err instanceof ExternalHostError || ["rate-limit-exceeded", "repository-changed"].includes(err.message)) {
			logger.debug("Passing PR error up");
			throw err;
		}
		logger.error({ err }, `Error ensuring PR`);
	}
	if (!branchExists) return {
		branchExists: true,
		updatesVerified,
		prNo: branchPr?.number,
		result: "pr-created",
		commitSha
	};
	return {
		branchExists,
		updatesVerified,
		prNo: branchPr?.number,
		result: "done",
		commitSha
	};
}
export { processBranch };

//# sourceMappingURL=index.js.map