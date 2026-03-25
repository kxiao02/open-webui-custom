import { decode } from 'html-entities';

import { WEBUI_API_BASE_URL } from '$lib/constants';
import { resolveToolDisplay } from '$lib/utils/tool-display';

export type GeneratedFileItem = {
	id: string;
	name: string;
	url?: string;
	path?: string;
	downloadMode: 'link' | 'terminal';
	source: string;
	size?: number;
	type?: string;
	contentType?: string;
	isImage?: boolean;
	timestamp?: number;
};

const TOOL_CALL_BLOCK_REGEX = /<details\b[^>]*\btype="tool_calls"[^>]*>[\s\S]*?<\/details>/gim;
const TOOL_CALL_OPEN_TAG_REGEX = /^<details\b([^>]*)>/i;
const TOOL_CALL_ATTR_REGEX = /(\w+)="([^"]*)"/g;

const FILE_GENERATING_TOOL_IDS = new Set([
	'write_file',
	'replace_file_content',
	'display_file',
	'write_structured_file',
	'gotenberg_convert'
]);

export const normalizeFileRef = (value: unknown): string | null => {
	if (typeof value !== 'string') return null;
	const normalized = value.trim();
	if (!normalized) return null;
	const lowered = normalized.toLowerCase();
	if (lowered === 'null' || lowered === 'undefined') return null;
	return normalized;
};

export const inferFileName = (value: string, fallback = 'generated-file') => {
	const sanitized = (value || '').split('?')[0];
	const pathPart = sanitized.split('/').pop() || sanitized;
	const windowsPathPart = pathPart.split('\\').pop() || pathPart;
	return windowsPathPart.trim() || fallback;
};

export const normalizeOpenWebUiFileUrl = (value: string): string => {
	if (!value) return value;

	let output = value;
	if (output.includes('/v1/files/') && !output.includes('/openai/v1/files/')) {
		output = output.replace(/(^|[^/])\/v1\/files\//g, '$1/openai/v1/files/');
	}

	if (output.startsWith('http') || output.startsWith('data:') || output.startsWith('/')) {
		return output;
	}

	return `${WEBUI_API_BASE_URL}/files/${output}/content`;
};

export const isDownloadRef = (value: string): boolean => {
	const normalized = value.trim();
	if (!normalized) return false;

	return (
		normalized.startsWith('http') ||
		normalized.startsWith('data:') ||
		normalized.startsWith('/api/v1/files/') ||
		normalized.startsWith('/openai/v1/files/') ||
		normalized.startsWith('/v1/files/')
	);
};

export const isImageRef = (name: string, type?: string, contentType?: string): boolean => {
	const ext = (name.split('.').pop() || '').toLowerCase();
	if ((contentType || '').startsWith('image/')) return true;
	if ((type || '').toLowerCase() === 'image') return true;
	return ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext);
};

export const parseNestedJSON = (value: unknown): unknown => {
	if (typeof value !== 'string') return value;

	try {
		return parseNestedJSON(JSON.parse(value));
	} catch {
		return value;
	}
};

const getToolCallAttrs = (block: string): Record<string, string> => {
	const openTagMatch = block.match(TOOL_CALL_OPEN_TAG_REGEX);
	if (!openTagMatch) return {};

	const attrs: Record<string, string> = {};
	for (const match of openTagMatch[1].matchAll(TOOL_CALL_ATTR_REGEX)) {
		attrs[match[1]] = match[2];
	}

	return attrs;
};

export const toGeneratedFile = (
	item: unknown,
	source: string,
	timestamp?: number
): GeneratedFileItem | null => {
	if (typeof item === 'string') {
		const ref = normalizeFileRef(item);
		if (!ref) return null;
		const name = inferFileName(ref);
		return {
			id: `${source}:${ref}`,
			name,
			url: normalizeOpenWebUiFileUrl(ref),
			downloadMode: 'link',
			source,
			isImage: isImageRef(name),
			timestamp
		};
	}

	if (!item || typeof item !== 'object') return null;

	const record = item as Record<string, unknown>;
	const ref =
		normalizeFileRef(record.url) ??
		normalizeFileRef(record.download_url) ??
		normalizeFileRef(record.downloadUrl) ??
		normalizeFileRef(record.ossUrl) ??
		normalizeFileRef(record.domainUrl) ??
		normalizeFileRef(record.id) ??
		normalizeFileRef(record.file_id) ??
		normalizeFileRef(record.fileId);
	const terminalPath =
		normalizeFileRef(record.path) ??
		normalizeFileRef(record.output_path) ??
		normalizeFileRef(record.target_path) ??
		normalizeFileRef(record.file_path);

	if (!ref && !terminalPath) return null;

	const name =
		(typeof record.name === 'string' && record.name.trim()) ||
		(typeof record.filename === 'string' && record.filename.trim()) ||
		(typeof record.fileName === 'string' && record.fileName.trim()) ||
		inferFileName(ref || terminalPath || '');

	const type = typeof record.type === 'string' ? record.type : undefined;
	const contentType =
		typeof record.content_type === 'string' ? record.content_type : undefined;

	if (!ref && terminalPath && !isDownloadRef(terminalPath)) {
		return {
			id: `${source}:${terminalPath}:${name}`,
			name,
			path: terminalPath,
			downloadMode: 'terminal',
			source,
			size: typeof record.size === 'number' ? record.size : undefined,
			type,
			contentType,
			isImage: isImageRef(name, type, contentType),
			timestamp
		};
	}

	if (!ref) return null;

	return {
		id: `${source}:${ref}:${name}`,
		name,
		url: normalizeOpenWebUiFileUrl(ref),
		downloadMode: 'link',
		source,
		size: typeof record.size === 'number' ? record.size : undefined,
		type,
		contentType,
		isImage: isImageRef(name, type, contentType),
		timestamp
	};
};

const dedupeGeneratedFiles = (files: GeneratedFileItem[]): GeneratedFileItem[] => {
	const deduped = new Map<string, GeneratedFileItem>();

	for (const file of files) {
		const key = `${file.url ?? file.path ?? ''}|${file.name}`;
		const existing = deduped.get(key);
		deduped.set(key, { ...existing, ...file });
	}

	return Array.from(deduped.values());
};

export const collectGeneratedFilesFromValue = (
	value: unknown,
	source: string,
	timestamp?: number
): GeneratedFileItem[] => {
	const files: GeneratedFileItem[] = [];

	const visit = (entry: unknown) => {
		if (!entry) return;

		if (Array.isArray(entry)) {
			for (const item of entry) {
				visit(item);
			}
			return;
		}

		const normalized = toGeneratedFile(entry, source, timestamp);
		if (normalized) {
			files.push(normalized);
		}

		if (typeof entry !== 'object') {
			return;
		}

		const record = entry as Record<string, unknown>;
		for (const nestedKey of ['files', 'outputs', 'artifacts', 'attachments']) {
			if (Array.isArray(record[nestedKey])) {
				visit(record[nestedKey]);
			}
		}
	};

	visit(value);
	return dedupeGeneratedFiles(files);
};

export const extractGeneratedFilesFromToolBlocks = (
	content: string,
	timestamp?: number
): GeneratedFileItem[] => {
	if (!content || !content.includes('type="tool_calls"')) return [];

	const files: GeneratedFileItem[] = [];

	for (const match of content.matchAll(TOOL_CALL_BLOCK_REGEX)) {
		const block = match[0] || '';
		const attrs = getToolCallAttrs(block);
		const toolId = resolveToolDisplay({
			toolId: attrs.tool_id,
			toolName: attrs.tool_name,
			legacyName: attrs.name
		}).toolId;

		if (!FILE_GENERATING_TOOL_IDS.has(toolId)) {
			continue;
		}

		const parsedFiles = parseNestedJSON(decode(attrs.files ?? ''));
		files.push(...collectGeneratedFilesFromValue(parsedFiles, 'tool', timestamp));

		const parsedResult = parseNestedJSON(decode(attrs.result ?? ''));
		if (
			parsedResult &&
			typeof parsedResult === 'object' &&
			!Array.isArray(parsedResult) &&
			(parsedResult as Record<string, unknown>).success === false
		) {
			continue;
		}

		files.push(...collectGeneratedFilesFromValue(parsedResult, 'tool', timestamp));
	}

	return dedupeGeneratedFiles(files);
};

export const collectGeneratedFilesFromMessage = (messageData: any): GeneratedFileItem[] => {
	if (!messageData) return [];

	const source =
		messageData?.role === 'assistant' ? 'assistant' : messageData?.role || 'message';
	const timestamp =
		typeof messageData?.timestamp === 'number' ? messageData.timestamp : undefined;
	const files: GeneratedFileItem[] = [];

	if (Array.isArray(messageData?.files)) {
		files.push(...collectGeneratedFilesFromValue(messageData.files, source, timestamp));
	}

	files.push(...extractGeneratedFilesFromToolBlocks(messageData?.content ?? '', timestamp));

	return dedupeGeneratedFiles(files);
};

export const collectGeneratedFilesFromHistory = (history: any): GeneratedFileItem[] => {
	const result: GeneratedFileItem[] = [];
	const messages = Object.values(history?.messages ?? {}) as Array<Record<string, unknown>>;

	for (const message of messages) {
		result.push(...collectGeneratedFilesFromMessage(message));
	}

	return dedupeGeneratedFiles(result).sort((a, b) => (b.timestamp ?? 0) - (a.timestamp ?? 0));
};
