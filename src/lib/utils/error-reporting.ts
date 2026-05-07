import { browser } from '$app/environment';
import { WEBUI_API_BASE_URL, WEBUI_BUILD_HASH, WEBUI_VERSION } from '$lib/constants';

type ErrorReportEvent = Record<string, unknown>;

const REDACTION_MARKER = '[REDACTED]';
const MAX_STRING_LENGTH = 2048;
const DEDUPE_WINDOW_MS = 15000;
const SENSITIVE_KEY_PATTERN =
	/(^|[_-])(authorization|cookie|set_cookie|token|access_token|refresh_token|id_token|api_key|apikey|secret|password|passwd|prompt|prompts|input|inputs|file_content|uploaded_content|raw_body|request_body|response_body|html_body|body|html)($|[_-])/i;

let reporting = false;
let globalHandlersInstalled = false;
const recentEvents = new Map<string, number>();

const truncate = (value: string, limit = MAX_STRING_LENGTH) => {
	if (value.length <= limit) return value;
	return `${value.slice(0, limit)}...[truncated ${value.length - limit} chars]`;
};

const sanitize = (value: unknown, key = ''): unknown => {
	if (key && SENSITIVE_KEY_PATTERN.test(key)) {
		return REDACTION_MARKER;
	}

	if (Array.isArray(value)) {
		return value.slice(0, 100).map((item) => sanitize(item));
	}

	if (value && typeof value === 'object') {
		return Object.fromEntries(
			Object.entries(value as Record<string, unknown>).map(([itemKey, itemValue]) => [
				itemKey,
				sanitize(itemValue, itemKey)
			])
		);
	}

	if (typeof value === 'string') {
		return truncate(value);
	}

	return value;
};

const normalizeError = (error: unknown) => {
	if (error instanceof Error) {
		return {
			name: error.name,
			message: error.message,
			stack: error.stack
		};
	}

	if (typeof error === 'string') {
		return { message: error };
	}

	return { message: String(error ?? '') };
};

const dedupeKey = (event: ErrorReportEvent) =>
	[
		event.type,
		event.message,
		(event.response as Record<string, unknown> | undefined)?.url,
		(event.response as Record<string, unknown> | undefined)?.status
	]
		.filter(Boolean)
		.join('|');

const shouldDropDuplicate = (event: ErrorReportEvent) => {
	const key = dedupeKey(event);
	if (!key) return false;

	const now = Date.now();
	const lastSeen = recentEvents.get(key) ?? 0;
	recentEvents.set(key, now);

	for (const [eventKey, timestamp] of recentEvents) {
		if (now - timestamp > DEDUPE_WINDOW_MS * 4) {
			recentEvents.delete(eventKey);
		}
	}

	return now - lastSeen < DEDUPE_WINDOW_MS;
};

const deliver = (payload: Record<string, unknown>) => {
	const url = `${WEBUI_API_BASE_URL}/error-events`;
	const body = JSON.stringify(payload);

	try {
		if (navigator.sendBeacon) {
			const accepted = navigator.sendBeacon(url, new Blob([body], { type: 'application/json' }));
			if (accepted) return;
		}
	} catch {
		// Fall through to fetch; reporting failures must never surface to users.
	}

	fetch(url, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body,
		keepalive: true
	}).catch(() => {});
};

export const reportErrorEvent = (event: ErrorReportEvent) => {
	if (!browser || reporting || !event || shouldDropDuplicate(event)) {
		return;
	}

	reporting = true;
	try {
		const payload = sanitize({
			...event,
			source: event.source ?? 'browser',
			reported_at: new Date().toISOString(),
			app: {
				version: WEBUI_VERSION,
				build_hash: WEBUI_BUILD_HASH,
				route: window.location.pathname,
				query_present: Boolean(window.location.search)
			}
		}) as Record<string, unknown>;

		deliver(payload);
	} catch {
		// Never let diagnostics affect product behavior.
	} finally {
		reporting = false;
	}
};

export const reportResponseError = (event: ErrorReportEvent & { response?: Response }) => {
	const response = event.response;
	if (!response) {
		reportErrorEvent(event);
		return;
	}

	reportErrorEvent({
		...event,
		response: {
			url: response.url,
			status: response.status,
			statusText: response.statusText,
			contentType: response.headers.get('content-type'),
			redirected: response.redirected
		}
	});
};

export const installGlobalErrorReporting = () => {
	if (!browser || globalHandlersInstalled) {
		return;
	}

	globalHandlersInstalled = true;

	window.addEventListener('error', (event) => {
		reportErrorEvent({
			type: 'browser.unhandled_error',
			message: event.message,
			error: normalizeError(event.error),
			location: {
				filename: event.filename,
				lineno: event.lineno,
				colno: event.colno
			}
		});
	});

	window.addEventListener('unhandledrejection', (event) => {
		reportErrorEvent({
			type: 'browser.unhandled_rejection',
			message: normalizeError(event.reason).message,
			error: normalizeError(event.reason)
		});
	});
};
