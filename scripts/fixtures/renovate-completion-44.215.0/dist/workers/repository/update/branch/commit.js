import { CONFIG_SECRETS_EXPOSED } from "../../../../constants/error-messages.js";
import { GlobalConfig } from "../../../../config/global.js";
import { minimatch } from "../../../../util/minimatch.js";
import { sanitize } from "../../../../util/sanitize.js";
import { logger } from "../../../../logger/index.js";
import { scm } from "../../../../modules/platform/scm.js";
import { isNonEmptyArray } from "@sindresorhus/is";
// lib/workers/repository/update/branch/commit.ts
function commitFilesToBranch(config) {
	let updatedFiles = config.updatedPackageFiles.concat(config.updatedArtifacts);
	// istanbul ignore if
	if (isNonEmptyArray(config.excludeCommitPaths)) updatedFiles = updatedFiles.filter(({ path: filePath }) => {
		if (config.excludeCommitPaths.some((excludedPath) => minimatch(excludedPath, { dot: true }).match(filePath))) {
			logger.debug(`Excluding ${filePath} from commit`);
			return false;
		}
		return true;
	});
	if (!isNonEmptyArray(updatedFiles)) {
		logger.debug(`No files to commit`);
		return Promise.resolve(null);
	}
	const fileLength = new Set(updatedFiles.map((file) => file.path)).size;
	logger.debug(`${fileLength} file(s) to commit`);
	// istanbul ignore if
	if (config.branchName !== sanitize(config.branchName) || config.commitMessage !== sanitize(config.commitMessage) || config.commitTrailers?.some((trailer) => trailer !== sanitize(trailer))) {
		logger.debug({ branchName: config.branchName }, "Secrets exposed in branchName, commitMessage or commitTrailers");
		throw new Error(CONFIG_SECRETS_EXPOSED);
	}
	const commitFilesConfig = {
		baseBranch: config.baseBranch,
		branchName: config.branchName,
		files: updatedFiles,
		message: config.commitMessage,
		trailers: config.commitTrailers,
		force: !!config.forceCommit,
		platformCommit: config.platformCommit,
		prTitle: config.prTitle,
		autoApprove: config.autoApprove
	};
	// istanbul ignore if
	if (GlobalConfig.get("dryRun")) {
		const logExtra = { ...commitFilesConfig };
		for (const file of logExtra.files) if (file.type === "addition") file.rawContents = file.contents;
		logger.info(`DRY-RUN: Would commit files to branch ${config.branchName}. See debug logs for raw commit information`);
		logger.debug({ ...logExtra }, `DRY-RUN: Would commit files to branch ${config.branchName}`);
		return Promise.resolve(null);
	}
	return scm.commitAndPush(commitFilesConfig);
}
export { commitFilesToBranch };

//# sourceMappingURL=commit.js.map