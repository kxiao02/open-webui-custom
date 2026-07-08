// @ts-nocheck
import { WEBUI_API_BASE_URL } from '$lib/constants';

const KNOWFLOW_STATUS_ENDPOINTS = [
	'/knowflow/status',
	'/knowflow/binding',
	'/knowledge/knowflow/status',
	'/knowledge/knowflow/binding',
	'/knowledge/binding'
];

const KNOWFLOW_BIND_ENDPOINTS = [
	'/knowflow/binding',
	'/knowflow/bind',
	'/knowledge/knowflow/binding',
	'/knowledge/knowflow/bind',
	'/knowledge/binding'
];

const KNOWFLOW_CLEAR_ENDPOINTS = [
	'/knowflow/binding',
	'/knowflow/clear',
	'/knowledge/knowflow/binding',
	'/knowledge/knowflow/clear',
	'/knowledge/binding'
];

const toBoolean = (value: any, fallback = false) => {
	if (typeof value === 'boolean') {
		return value;
	}
	return fallback;
};

const pickFirstString = (...values: any[]) => {
	for (const value of values) {
		if (typeof value === 'string' && value.trim().length > 0) {
			return value.trim();
		}
	}
	return '';
};

const parseErrorMessage = (error: any) => {
	if (typeof error === 'string') {
		return error;
	}
	if (typeof error?.detail === 'string') {
		return error.detail;
	}
	if (typeof error?.message === 'string') {
		return error.message;
	}
	return 'Knowflow request failed.';
};

const requestKnownEndpoints = async (
	token: string,
	method: 'GET' | 'POST' | 'DELETE',
	endpoints: string[],
	body: any = null
) => {
	let fallbackError = null;

	for (const endpoint of endpoints) {
		try {
			const response = await fetch(`${WEBUI_API_BASE_URL}${endpoint}`, {
				method,
				headers: {
					Accept: 'application/json',
					'Content-Type': 'application/json',
					authorization: `Bearer ${token}`
				},
				...(body ? { body: JSON.stringify(body) } : {})
			});

			if (response.status === 404 || response.status === 405) {
				continue;
			}

			const hasJson = response.headers.get('content-type')?.includes('application/json');
			const payload = hasJson ? await response.json() : null;

			if (!response.ok) {
				throw payload ?? { detail: `Request failed with status ${response.status}` };
			}

			return payload ?? true;
		} catch (error) {
			fallbackError = error;
		}
	}

	throw parseErrorMessage(fallbackError ?? { detail: 'Knowflow integration API is unavailable.' });
};

export const getKnowflowFrontendConfig = (config: any = {}) => {
	const knowflow =
		config?.knowflow ?? config?.knowledge?.knowflow ?? config?.integrations?.knowflow ?? {};

	const siteUrl = pickFirstString(
		knowflow?.site_url,
		knowflow?.siteUrl,
		knowflow?.url,
		config?.knowledge?.site_url,
		config?.knowledge?.siteUrl,
		config?.knowflow_site_url
	);

	const enabled =
		toBoolean(knowflow?.enabled, false) ||
		toBoolean(config?.features?.enable_knowflow, false) ||
		pickFirstString(config?.features?.knowledge_provider).toLowerCase() === 'knowflow' ||
		Boolean(siteUrl);

	const readOnlyRaw =
		knowflow?.read_only ??
		knowflow?.readonly ??
		(config?.features?.knowledge_read_only ?? config?.features?.knowledge_readonly);

	const readOnly = enabled ? toBoolean(readOnlyRaw, true) : false;

	return {
		enabled,
		readOnly,
		siteUrl,
		manualBindEnabled: knowflow?.manual_bind_enabled ?? knowflow?.manualBindEnabled ?? true,
		publicReadOnlyEnabled:
			knowflow?.public_read_only_enabled ?? knowflow?.publicReadOnlyEnabled ?? false
	};
};

const normalizeKnowflowBindingStatus = (status: any = {}) => {
	const mode = pickFirstString(status?.mode, status?.binding_mode, status?.auth_mode);
	const connected = toBoolean(
		status?.connected,
		pickFirstString(status?.status, status?.state).toLowerCase() === 'connected'
	);
	const bindRequired = toBoolean(status?.bind_required, !connected);
	const displayStatus = pickFirstString(status?.status, status?.state, connected ? 'connected' : 'not_bound');

	return {
		...status,
		mode,
		connected,
		bind_required: bindRequired,
		status: displayStatus,
		user_email: pickFirstString(status?.user_email, status?.knowflow_user_email),
		user_id: pickFirstString(status?.user_id, status?.knowflow_user_id)
	};
};

export const getKnowflowBindingStatus = async (token: string) => {
	const res = await requestKnownEndpoints(token, 'GET', KNOWFLOW_STATUS_ENDPOINTS);
	return normalizeKnowflowBindingStatus(res);
};

export const bindKnowflowApiKey = async (token: string, apiKey: string) => {
	const res = await requestKnownEndpoints(token, 'POST', KNOWFLOW_BIND_ENDPOINTS, {
		api_key: apiKey
	});
	return normalizeKnowflowBindingStatus(res);
};

export const clearKnowflowBinding = async (token: string) => {
	const res = await requestKnownEndpoints(token, 'DELETE', KNOWFLOW_CLEAR_ENDPOINTS);
	return normalizeKnowflowBindingStatus(res);
};
