import { browser } from '$app/environment';

import { WEBUI_BASE_URL } from '$lib/constants';

const KNOWFLOW_MINIO_PATH_REGEX = /^\/?(?:openai\/)?minio\/.+/i;
const MEDIA_URL_ATTR_REGEX = /\b(src|href|poster)=("([^"]*)"|'([^']*)')/gi;

const getBrowserOrigin = () => (browser ? window.location.origin : 'http://localhost');

export const getKnowflowAssetProxyUrl = (value: string | null | undefined): string | null => {
	const normalized = typeof value === 'string' ? value.trim() : '';
	if (!normalized) {
		return null;
	}

	if (normalized.startsWith('data:') || normalized.startsWith('blob:')) {
		return normalized;
	}

	const directAssetMatch = normalized.match(/^(\/?(?:openai\/)?minio\/[^?#]+)([?#].*)?$/i);
	if (directAssetMatch) {
		const assetPath = directAssetMatch[1].replace(/^\/?openai(?=\/minio\/)/i, '');
		return `${WEBUI_BASE_URL}/api/v1/knowflow/assets${
			assetPath.startsWith('/') ? assetPath : `/${assetPath}`
		}${directAssetMatch[2] ?? ''}`;
	}

	try {
		const parsed = new URL(normalized, getBrowserOrigin());
		const assetPath = parsed.pathname.replace(/^\/openai(?=\/minio\/)/i, '');
		if (!KNOWFLOW_MINIO_PATH_REGEX.test(assetPath)) {
			return null;
		}

		return `${WEBUI_BASE_URL}/api/v1/knowflow/assets${assetPath}${parsed.search}${parsed.hash}`;
	} catch {
		const path = normalized.replace(/^\/?openai(?=\/minio\/)/i, '');
		if (!KNOWFLOW_MINIO_PATH_REGEX.test(path)) {
			return null;
		}

		return `${WEBUI_BASE_URL}/api/v1/knowflow/assets${path.startsWith('/') ? path : `/${path}`}`;
	}
};

export const normalizeMediaUrl = (value: string | null | undefined): string => {
	const normalized = typeof value === 'string' ? value.trim() : '';
	if (!normalized) {
		return '';
	}

	const knowflowProxyUrl = getKnowflowAssetProxyUrl(normalized);
	if (knowflowProxyUrl) {
		return knowflowProxyUrl;
	}

	return normalized.startsWith('/') ? `${WEBUI_BASE_URL}${normalized}` : normalized;
};

export const normalizeHtmlMediaUrls = (value: string): string => {
	if (typeof value !== 'string' || !value) {
		return value;
	}

	return value.replace(MEDIA_URL_ATTR_REGEX, (match, attrName, quotedValue, doubleValue, singleValue) => {
		const quote = quotedValue.startsWith("'") ? "'" : '"';
		const rawValue = doubleValue ?? singleValue ?? '';
		const normalizedValue = normalizeMediaUrl(rawValue);
		if (!normalizedValue || normalizedValue === rawValue) {
			return match;
		}

		return `${attrName}=${quote}${normalizedValue}${quote}`;
	});
};
