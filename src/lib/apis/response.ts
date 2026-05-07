import { installGlobalErrorReporting, reportResponseError } from '$lib/utils/error-reporting';

installGlobalErrorReporting();

const JSON_CONTENT_TYPES = ['application/json', '+json'];

const hasJsonContentType = (response: Response) => {
	const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
	return JSON_CONTENT_TYPES.some((type) => contentType.includes(type));
};

const isAuthRedirect = (response: Response) => {
	if (!response.redirected && !response.headers.get('location')) {
		return false;
	}

	const target = response.url || response.headers.get('location') || '';
	return /\/(?:auth|oauth|sso)(?:\/|$)/.test(target);
};

const readTextPreview = async (response: Response) => {
	try {
		return (await response.text()).trim().slice(0, 160);
	} catch {
		return '';
	}
};

const buildResponseError = async (response: Response, fallback = 'Request failed.') => {
	const status = response.status;
	const statusText = response.statusText || 'Request failed';
	const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
	const textPreview = await readTextPreview(response);

	if (status === 401 || status === 403 || isAuthRedirect(response)) {
		reportResponseError({
			type: 'api.non_json_response',
			message: 'Session expired or access was denied while expecting JSON.',
			response,
			responsePreview: textPreview
		});
		return {
			detail: 'Session expired or access was denied. Please sign in again.',
			status,
			statusText
		};
	}

	if (contentType.includes('text/html') || textPreview.startsWith('<')) {
		reportResponseError({
			type: 'api.html_response',
			message: 'Expected JSON but received an HTML response.',
			response,
			responsePreview: textPreview
		});
		return {
			detail: `Expected JSON but received an HTML response (${status || 'unknown'} ${statusText}).`,
			status,
			statusText
		};
	}

	reportResponseError({
		type: 'api.non_json_response',
		message: fallback,
		response,
		responsePreview: textPreview
	});

	return {
		detail: textPreview || fallback,
		status,
		statusText
	};
};

export const parseResponseError = async (response: Response) => {
	if (hasJsonContentType(response)) {
		try {
			return await response.json();
		} catch {
			return {
				detail: `Invalid JSON response (${response.status || 'unknown'} ${
					response.statusText || 'Request failed'
				}).`,
				status: response.status,
				statusText: response.statusText
			};
		}
	}

	return await buildResponseError(response);
};

export const parseJsonResponse = async (response: Response) => {
	if (!hasJsonContentType(response)) {
		throw await buildResponseError(response, 'Expected JSON response.');
	}

	let payload = null;
	try {
		payload = await response.json();
	} catch {
		reportResponseError({
			type: 'api.invalid_json_response',
			message: 'Invalid JSON response.',
			response
		});
		throw {
			detail: `Invalid JSON response (${response.status || 'unknown'} ${
				response.statusText || 'Request failed'
			}).`,
			status: response.status,
			statusText: response.statusText
		};
	}

	if (!response.ok) {
		throw (
			payload ?? {
				detail: response.statusText || 'Request failed.',
				status: response.status,
				statusText: response.statusText
			}
		);
	}

	return payload;
};
