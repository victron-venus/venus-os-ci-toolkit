import { LOGGER_NOT_INITIALIZED } from "../constants/error-messages.js";
import { once, reset } from "./once.js";
import { getRemappedLevel } from "./remap.js";
import { getMessage, toMeta } from "./utils.js";
import { withSanitizer } from "./with-sanitizer.js";
import { isString } from "@sindresorhus/is";
// lib/logger/renovate-logger.ts
const loggerLevels = [
	"trace",
	"debug",
	"info",
	"warn",
	"error",
	"fatal"
];
var RenovateLogger = class RenovateLogger {
	queue = [];
	logger = { once: { reset } };
	once = this.logger.once;
	bunyanLogger;
	uninitializedWarningFired;
	context;
	meta;
	constructor(context, meta, bunyanLogger) {
		this.bunyanLogger = bunyanLogger;
		this.context = context;
		this.meta = meta;
		for (const level of loggerLevels) {
			this.logger[level] = this.logFactory(level);
			this.logger.once[level] = this.logOnceFn(level);
		}
		this.uninitializedWarningFired = false;
	}
	trace(p1, p2) {
		this.log("trace", p1, p2);
	}
	debug(p1, p2) {
		this.log("debug", p1, p2);
	}
	info(p1, p2) {
		this.log("info", p1, p2);
	}
	warn(p1, p2) {
		this.log("warn", p1, p2);
	}
	error(p1, p2) {
		this.log("error", p1, p2);
	}
	fatal(p1, p2) {
		this.log("fatal", p1, p2);
	}
	addSerializers(serializers) {
		this.ensureLogger().addSerializers(serializers);
	}
	addStream(stream) {
		this.ensureLogger().addStream(withSanitizer(stream));
	}
	childLogger() {
		return new RenovateLogger(this.context, this.meta, this.ensureLogger().child({}));
	}
	levels(name, level) {
		this.ensureLogger().levels(name, level);
	}
	get logContext() {
		return this.context;
	}
	set logContext(context) {
		this.context = context;
	}
	/**
	* For internal initialization only
	*/
	set bunyan(bunyanLogger) {
		this.bunyanLogger = bunyanLogger;
		for (const logFn of this.queue) logFn();
		this.queue.length = 0;
	}
	setMeta(obj) {
		this.meta = { ...obj };
	}
	addMeta(obj) {
		this.meta = {
			...this.meta,
			...obj
		};
	}
	removeMeta(fields) {
		for (const key of Object.keys(this.meta)) if (fields.includes(key)) delete this.meta[key];
	}
	ensureLogger() {
		if (!this.bunyanLogger) throw new Error(LOGGER_NOT_INITIALIZED);
		return this.bunyanLogger;
	}
	logFactory(_level) {
		return (p1, p2) => {
			const meta = {
				logContext: this.context,
				...this.meta,
				...toMeta(p1)
			};
			const msg = getMessage(p1, p2);
			let level = _level;
			if (isString(msg)) {
				const remappedLevel = getRemappedLevel(msg);
				/* v8 ignore next -- not easily testable */
				if (remappedLevel) {
					meta.oldLevel = level;
					level = remappedLevel;
				}
				this.ensureLogger()[level](meta, msg);
			} else this.ensureLogger()[level](meta);
		};
	}
	logOnceFn(level) {
		const logOnceFn = (p1, p2) => {
			once(() => {
				const logFn = this[level].bind(this);
				if (isString(p1)) logFn(p1);
				else logFn(p1, p2);
			}, logOnceFn, p1, p2);
		};
		return logOnceFn;
	}
	log(level, p1, p2) {
		if (!this.bunyanLogger) {
			this.queue.push(() => this.log(level, p1, p2));
			if (!this.uninitializedWarningFired) {
				console.warn(`⚠️ NOTE ⚠️: Renovate's logger has not yet been initialized. If you see no other output, this is a bug`);
				this.uninitializedWarningFired = true;
			}
			return;
		}
		const logFn = this.logger[level];
		if (isString(p1)) logFn(p1);
		else logFn(p1, p2);
	}
};
export { RenovateLogger };

//# sourceMappingURL=renovate-logger.js.map