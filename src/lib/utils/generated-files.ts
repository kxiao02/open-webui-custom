import { decode } from 'html-entities';

import { WEBUI_API_BASE_URL } from '$lib/constants';
import { normalizeMediaUrl } from '$lib/utils/knowflowAssets';
import { normalizeToolId, resolveToolDisplay } from '$lib/utils/tool-display';

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
	toolId?: string;
	isImage?: boolean;
	timestamp?: number;
};

type GeneratedFileLike = Pick<
	GeneratedFileItem,
	'name' | 'url' | 'path' | 'toolId' | 'source' | 'contentType' | 'type'
>;

const FILE_NAME_WITH_EXTENSION_REGEX = /^[^\\/:*?"<>|\r\n]+\.[A-Za-z0-9]{1,16}$/;
const TRAILING_PUNCTUATION_REGEX = /[.,;:!?，。！？；：）】》」』]+$/g;
const QUOTED_WRAPPER_REGEX = /^['"`“”‘’]+|['"`“”‘’]+$/g;
const LEADING_LIST_MARKER_REGEX = /^[\-\u2022*]+\s*/;
const PATH_LIKE_TOKEN_REGEX =
	/(?:^|[\s"'`([{<，。！？；：])((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|~[\\/]|\/)[^\s"'`<>，。！？；：]+)(?=$|[\s"'`)\]}>，。！？；：])/g;
const FILENAME_TOKEN_REGEX =
	/(?:^|[\s"'`([{<，。！？；：])([^\\/:*?"<>|\r\n\s]+\.[A-Za-z0-9]{1,16})(?=$|[\s"'`)\]}>.,;:!?，。！？；：])/g;
const FILE_ID_REGEX = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
const GENERATED_FILES_CACHE_LIMIT = 200;
const messageGeneratedFilesCache = new Map<string, GeneratedFileItem[]>();
const historyGeneratedFilesCache = new Map<string, GeneratedFileItem[]>();

const TOOL_CALL_BLOCK_REGEX = /<details\b[^>]*\btype="tool_calls"[^>]*>[\s\S]*?<\/details>/gim;
const TOOL_CALL_OPEN_TAG_REGEX = /^<details\b([^>]*)>/i;
const TOOL_CALL_ATTR_REGEX = /(\w+)="([^"]*)"/g;
const TOOL_TIMEOUT_TEXT_REGEX = /(?:timed?\s*out|timeout|超时)/i;
const TOOL_ERROR_TEXT_REGEX =
	/(?:\berror\b|\bfailed\b|\bfailure\b|\bexception\b|\btraceback\b|internal server error|错误|失败|异常)/i;
const TOOL_RESULT_STRING_TIMEOUT_REGEX =
	/(?:["']?status["']?\s*:\s*["'](?:timeout|timed[_-]?out|timed out)["']|["']?success["']?\s*:\s*(?:false|False)[\s\S]{0,240}(?:timed?\s*out|timeout|超时))/i;
const TOOL_RESULT_STRING_ERROR_REGEX =
	/(?:["']?status["']?\s*:\s*["'](?:error|failed|failure)["']|["']?(?:success|ok)["']?\s*:\s*(?:false|False)|\berror_count\b\s*:\s*[1-9]\d*\b|\berrors?\b\s*:\s*(?!\[\s*\]))/i;
const TOOL_RESULT_STRING_SUCCESS_REGEX =
	/(?:["']?status["']?\s*:\s*["'](?:ok|success|succeeded|completed|complete)["']|["']?(?:success|ok)["']?\s*:\s*(?:true|True)|["']?exit_code["']?\s*:\s*0\b)/i;
const TOOL_RESULT_SUCCESS_STATUSES = new Set(['ok', 'success', 'succeeded', 'completed', 'complete']);
const TOOL_RESULT_ERROR_STATUSES = new Set(['error', 'failed', 'failure']);
const TOOL_RESULT_TIMEOUT_STATUSES = new Set(['timeout', 'timed_out', 'timed-out']);

const FILE_GENERATING_TOOL_IDS = new Set([
	'write_file',
	'replace_file_content',
	'display_file',
	'write_structured_file',
	'gotenberg_convert',
	'pdf_create_document',
	'pdf_inspect_form',
	'pdf_fill_form_tool',
	'pdf_reformat_document',
	'docx_export_document',
	'pptx_export_presentation',
	'xlsx_create_workbook',
	'xlsx_add_column_tool',
	'xlsx_insert_row_tool'
]);
const FINAL_DOCUMENT_TOOL_IDS = new Set([
	'gotenberg_convert',
	'pdf_create_document',
	'pdf_inspect_form',
	'pdf_fill_form_tool',
	'pdf_reformat_document',
	'docx_export_document',
	'pptx_export_presentation',
	'xlsx_create_workbook',
	'xlsx_add_column_tool',
	'xlsx_insert_row_tool'
]);
const HELPER_ARTIFACT_TOOL_IDS = new Set([
	'write_file',
	'replace_file_content',
	'display_file',
	'write_structured_file'
]);
const FINAL_DOCUMENT_EXTENSIONS = new Set(['pdf', 'docx', 'pptx', 'xlsx']);
const HELPER_ARTIFACT_EXTENSIONS = new Set(['json', 'md', 'markdown', 'txt', 'yaml', 'yml', 'xml']);
const HELPER_ARTIFACT_NAME_REGEX =
	/(?:^|[_\-.])(workbook|content|draft|helper|intermediate|temp|tmp|staging|scratch|preview)(?:[_\-.]|$)/i;
const WORKBOOK_CONTENT_TYPE_REGEX = /(spreadsheet|excel|sheet)/i;
export const isFileGeneratingToolId = (value: string | undefined): boolean => {
	if (!value) return false;
	return FILE_GENERATING_TOOL_IDS.has(resolveToolDisplay({ toolId: value }).toolId);
};
const FILE_GENERATING_TOOL_ID_MARKER_REGEX =
	/tool_id="(?:write_file|replace_file_content|display_file|write_structured_file|gotenberg_convert|pdf_create_document|pdf_inspect_form|pdf_fill_form_tool|pdf_reformat_document|docx_export_document|pptx_export_presentation|xlsx_create_workbook|xlsx_add_column_tool|xlsx_insert_row_tool)"/i;
const FILE_GENERATING_TOOL_NAME_MARKER_REGEX =
	/name="(?:write_file|replace_file_content|display_file|write_structured_file|gotenberg_convert|pdf_create_document|pdf_inspect_form|pdf_fill_form_tool|pdf_reformat_document|docx_export_document|pptx_export_presentation|xlsx_create_workbook|xlsx_add_column_tool|xlsx_insert_row_tool)"/i;
const OPEN_WEBUI_FILE_URL_REGEX =
	/^(?:https?:\/\/[^/]+)?\/?(?:api\/v1|openai\/v1|v1)\/files\/([^/?#]+)(?:\/content)?(?:[?#].*)?$/i;
const OPEN_WEBUI_GENERATED_FILE_URL_REGEX =
	/^(?:https?:\/\/[^/]+)?\/?(?:api\/v1|openai\/v1|v1)\/generated-files\/([^?#]+)(?:[?#].*)?$/i;
const KNOWFLOW_MINIO_REF_REGEX = /\/(?:openai\/)?minio\/[^?#]+/i;

const normalizeTokenForMatching = (value: string): string => {
	return value.trim().replace(QUOTED_WRAPPER_REGEX, '').replace(TRAILING_PUNCTUATION_REGEX, '');
};

const hashString = (value: string): string => {
	let hash = 2166136261;

	for (let index = 0; index < value.length; index += 1) {
		hash ^= value.charCodeAt(index);
		hash = Math.imul(hash, 16777619);
	}

	return (hash >>> 0).toString(36);
};

export const normalizeFileRef = (value: unknown): string | null => {
	if (typeof value !== 'string') return null;
	const normalized = value.trim();
	if (!normalized) return null;
	const lowered = normalized.toLowerCase();
	if (lowered === 'null' || lowered === 'undefined') return null;
	return normalized;
};

export const normalizeVisualUrlForMatching = (value: unknown): string => {
	const normalized = normalizeFileRef(value);
	if (!normalized) return '';
	if (normalized.startsWith('data:') || normalized.startsWith('blob:')) return normalized;

	const openWebUiFileUrlMatch = normalized.match(OPEN_WEBUI_FILE_URL_REGEX);
	if (openWebUiFileUrlMatch?.[1]) {
		return `/api/v1/files/${openWebUiFileUrlMatch[1]}`;
	}

	const openWebUiGeneratedFileUrlMatch = normalized.match(OPEN_WEBUI_GENERATED_FILE_URL_REGEX);
	if (openWebUiGeneratedFileUrlMatch?.[1]) {
		return `/api/v1/generated-files/${openWebUiGeneratedFileUrlMatch[1]}`;
	}

	try {
		const parsed = new URL(normalized, WEBUI_API_BASE_URL);
		const knowflowRef = parsed.pathname.match(KNOWFLOW_MINIO_REF_REGEX)?.[0];
		if (knowflowRef) {
			return knowflowRef.replace(/^\/openai(?=\/minio\/)/i, '');
		}

		return `${parsed.pathname}${parsed.search}`;
	} catch {
		const knowflowRef = normalized.match(KNOWFLOW_MINIO_REF_REGEX)?.[0];
		if (knowflowRef) {
			return knowflowRef.replace(/^\/openai(?=\/minio\/)/i, '');
		}

		return normalized;
	}
};

const extractKnowflowAssetRef = (value: string | undefined): string => {
	const normalized = normalizeFileRef(value ?? '');
	if (!normalized) return '';
	if (normalized.startsWith('data:') || normalized.startsWith('blob:')) return '';

	let path = normalized;
	try {
		path = new URL(normalized, WEBUI_API_BASE_URL).pathname || normalized;
	} catch {
		path = normalized;
	}

	if (!path.startsWith('/')) {
		path = `/${path.replace(/^\/+/, '')}`;
	}

	const match = path.match(KNOWFLOW_MINIO_REF_REGEX);
	if (!match) return '';

	return match[0].replace(/^\/openai(?=\/minio\/)/i, '');
};

export const normalizeKnowflowAssetUrl = (value: unknown): string | null => {
	const normalized = normalizeFileRef(value);
	if (!normalized) return null;
	if (normalized.startsWith('data:') || normalized.startsWith('blob:')) return normalized;

	const knowflowAssetRef = extractKnowflowAssetRef(normalized);
	return knowflowAssetRef || normalized;
};

export const inferFileName = (value: string, fallback = 'generated-file') => {
	const sanitized = (value || '').split('?')[0];
	const pathPart = sanitized.split('/').pop() || sanitized;
	const windowsPathPart = pathPart.split('\\').pop() || pathPart;
	return windowsPathPart.trim() || fallback;
};

const cleanCandidateFileName = (value: string): string => {
	return value
		.trim()
		.replace(QUOTED_WRAPPER_REGEX, '')
		.replace(LEADING_LIST_MARKER_REGEX, '')
		.replace(TRAILING_PUNCTUATION_REGEX, '')
		.replace(/\s+/g, ' ');
};

const extractPathLikeRef = (value: string): string | null => {
	const matches = Array.from(value.matchAll(PATH_LIKE_TOKEN_REGEX));
	if (matches.length === 0) return null;

	for (let i = matches.length - 1; i >= 0; i -= 1) {
		const candidate = normalizeTokenForMatching(matches[i][1] ?? '');
		if (candidate) return candidate;
	}

	return null;
};

const extractFileNameToken = (value: string): string | null => {
	const matches = Array.from(value.matchAll(FILENAME_TOKEN_REGEX));
	if (matches.length === 0) return null;

	for (let i = matches.length - 1; i >= 0; i -= 1) {
		const candidate = cleanCandidateFileName(matches[i][1] ?? '');
		if (FILE_NAME_WITH_EXTENSION_REGEX.test(candidate)) return candidate;
	}

	return null;
};

const isLikelyOpaqueFileId = (value: string): boolean => {
	const normalized = normalizeTokenForMatching(value);
	if (!normalized || /\s/.test(normalized)) return false;
	if (!FILE_ID_REGEX.test(normalized)) return false;
	if (!(normalized.includes('-') || normalized.includes('_') || /[0-9]/.test(normalized))) {
		return false;
	}
	if (/^[A-Za-z]+$/.test(normalized)) return false;
	return true;
};

const isLikelyPathLikeRef = (value: string): boolean => {
	const normalized = normalizeTokenForMatching(value);
	if (!normalized || /\s{2,}/.test(normalized)) return false;
	if (isDownloadRef(normalized)) return true;
	if (/^(?:\.{1,2}[\\/]|~[\\/]|\/)/.test(normalized)) return true;
	if (/^[A-Za-z]:[\\/]/.test(normalized)) return true;
	if (normalized.includes('/') || normalized.includes('\\')) return true;
	if (FILE_NAME_WITH_EXTENSION_REGEX.test(normalized)) return true;
	return false;
};

const isLikelyFileReference = (value: string): boolean => {
	return isLikelyPathLikeRef(value) || isLikelyOpaqueFileId(value);
};

const isDirectStringFileReference = (value: string): boolean => {
	const normalized = normalizeTokenForMatching(value);
	if (!normalized) return false;
	if (isDownloadRef(normalized) || isLikelyOpaqueFileId(normalized)) return true;
	if (/^(?:\.{1,2}[\\/]|~[\\/]|\/)/.test(normalized)) return true;
	if (/^[A-Za-z]:[\\/]/.test(normalized)) return true;
	if (/^[A-Za-z][A-Za-z0-9+.-]*:[\\/]/.test(normalized)) return true;
	if (!/\s/.test(normalized) && FILE_NAME_WITH_EXTENSION_REGEX.test(normalized)) return true;
	return false;
};

const resolveStringFileReference = (value: string): string | null => {
	const normalized = normalizeFileRef(value);
	if (!normalized) return null;
	if (isDirectStringFileReference(normalized)) return normalized;

	const extractedPath = extractPathLikeRef(normalized);
	if (extractedPath) return extractedPath;

	const extractedFileName = extractFileNameToken(normalized);
	if (extractedFileName) return extractedFileName;

	return isLikelyFileReference(normalized) ? normalized : null;
};

const setGeneratedFilesCache = (
	cache: Map<string, GeneratedFileItem[]>,
	key: string,
	value: GeneratedFileItem[]
) => {
	cache.set(key, value);
	if (cache.size <= GENERATED_FILES_CACHE_LIMIT) return;
	const oldestKey = cache.keys().next().value;
	if (oldestKey) {
		cache.delete(oldestKey);
	}
};

const isLikelyDisplaySentence = (value: string): boolean => {
	const normalized = cleanCandidateFileName(value);
	if (!normalized) return false;
	if (normalized.length > 120) return true;
	if (normalized.includes('\n')) return true;
	if (/[。！？：；]/.test(normalized)) return true;
	if (/:\s/.test(normalized)) return true;
	if (/`[^`]+`/.test(normalized)) return true;
	return false;
};

const buildGeneratedFilesValueKey = (value: unknown): string => {
	if (!Array.isArray(value) || value.length === 0) return '';

	return value
		.map((entry) => {
			if (!entry || typeof entry !== 'object') {
				return String(entry ?? '');
			}

			const record = entry as Record<string, unknown>;
			return [
				record.bridge_file_id ?? '',
				record.url ?? '',
				record.bridge_url ?? '',
				record.generated_file_url ?? '',
				record.download_url ?? '',
				record.downloadUrl ?? '',
				record.path ?? '',
				record.output_path ?? '',
				record.target_path ?? '',
				record.file_path ?? '',
				record.name ?? '',
				record.filename ?? '',
				record.fileName ?? '',
				record.size ?? ''
			].join(':');
		})
		.join('|');
};

const buildGeneratedFilesOutputKey = (value: unknown): string => {
	if (value === null || value === undefined) return '';

	try {
		const serialized = JSON.stringify(value);
		if (!serialized) return '';
		return `${serialized.length}:${hashString(serialized)}`;
	} catch {
		return '';
	}
};

const hasPotentialGeneratedFilesInContent = (content: string): boolean => {
	if (!content || !content.includes('type="tool_calls"')) return false;

	return (
		FILE_GENERATING_TOOL_ID_MARKER_REGEX.test(content) ||
		FILE_GENERATING_TOOL_NAME_MARKER_REGEX.test(content) ||
		content.includes(' files=') ||
		content.includes('download_url') ||
		content.includes('downloadUrl') ||
		content.includes('output_path') ||
		content.includes('target_path') ||
		content.includes('file_path')
	);
};

const buildGeneratedFilesMessageCacheKey = (
	messageData: any,
	source: string,
	timestamp?: number
): string => {
	const filesKey = buildGeneratedFilesValueKey(messageData?.files);
	const outputKey = buildGeneratedFilesOutputKey(messageData?.output);
	const content = typeof messageData?.content === 'string' ? messageData.content : '';
	const hasPotentialContent = hasPotentialGeneratedFilesInContent(content);

	return [
		messageData?.id ?? '',
		source,
		timestamp ?? '',
		Array.isArray(messageData?.files) ? messageData.files.length : 0,
		filesKey,
		outputKey,
		hasPotentialContent ? `${content.length}:${hashString(content)}` : ''
	].join('::');
};

const buildGeneratedFilesHistoryCacheKey = (
	messages: Array<Record<string, unknown>>
): string => {
	return messages
		.map((message) => {
			const role = typeof message?.role === 'string' ? message.role : 'message';
			const source = role === 'assistant' ? 'assistant' : role;
			const timestamp =
				typeof message?.timestamp === 'number' ? message.timestamp : undefined;
			return buildGeneratedFilesMessageCacheKey(message, source, timestamp);
		})
		.join('||');
};

export const hasGeneratedFilesInHistory = (history: any): boolean => {
	const messages = Object.values(history?.messages ?? {}) as Array<Record<string, unknown>>;

	for (const message of messages) {
		if (Array.isArray(message?.files) && message.files.length > 0) {
			return true;
		}

		if (collectGeneratedFilesFromResponseOutput(message?.output).length > 0) {
			return true;
		}

		if (
			typeof message?.content === 'string' &&
			hasPotentialGeneratedFilesInContent(message.content)
		) {
			return true;
		}
	}

	return false;
};

export const sanitizeGeneratedFileName = (
	value: unknown,
	fallbackRef: string,
	fallback = 'generated-file'
): string => {
	const inferred = inferFileName(fallbackRef, fallback);
	if (typeof value !== 'string') return inferred;

	const cleaned = cleanCandidateFileName(value);
	if (!cleaned) return inferred;
	if (cleaned === inferred) return cleaned;
	if (cleaned.includes('/') || cleaned.includes('\\')) {
		const extractedPath = extractPathLikeRef(cleaned);
		if (extractedPath) return inferFileName(extractedPath, inferred);
		return inferFileName(cleaned, inferred);
	}
	const extractedToken = extractFileNameToken(cleaned);
	if (extractedToken) return extractedToken;
	if (cleaned.includes(inferred)) return inferred;
	if (FILE_NAME_WITH_EXTENSION_REGEX.test(cleaned) && !isLikelyDisplaySentence(cleaned)) {
		return cleaned;
	}

	return inferred;
};

export const normalizeOpenWebUiFileUrl = (value: string): string => {
	if (!value) return value;

	const output = value.trim();
	if (!output) return output;
	if (output.startsWith('data:')) return output;

	const openWebUiFileMatch = output.match(OPEN_WEBUI_FILE_URL_REGEX);
	if (openWebUiFileMatch?.[1]) {
		return `${WEBUI_API_BASE_URL}/files/${openWebUiFileMatch[1]}/content`;
	}

	const openWebUiGeneratedFileMatch = output.match(OPEN_WEBUI_GENERATED_FILE_URL_REGEX);
	if (openWebUiGeneratedFileMatch?.[1]) {
		return `/openai/v1/generated-files/${openWebUiGeneratedFileMatch[1]}`;
	}

	const knowflowAssetRef = extractKnowflowAssetRef(output);
	if (knowflowAssetRef) {
		return normalizeMediaUrl(knowflowAssetRef);
	}

	if (/^(?:api\/v1|openai\/v1|v1)\//i.test(output)) {
		return `/${output}`;
	}

	if (output.startsWith('http') || output.startsWith('/')) {
		return output;
	}

	return `${WEBUI_API_BASE_URL}/files/${output}/content`;
};

export const extractOpenWebUiFileId = (value: string): string | null => {
	if (!value) return null;
	return value.trim().match(OPEN_WEBUI_FILE_URL_REGEX)?.[1] ?? null;
};

export const isDownloadRef = (value: string): boolean => {
	const normalized = value.trim();
	if (!normalized) return false;

	return (
		normalized.startsWith('http') ||
		normalized.startsWith('data:') ||
		normalized.startsWith('/minio/') ||
		normalized.startsWith('/openai/minio/') ||
		normalized.startsWith('/api/v1/files/') ||
		normalized.startsWith('/api/v1/generated-files/') ||
		normalized.startsWith('/openai/v1/files/') ||
		normalized.startsWith('/openai/v1/generated-files/') ||
		normalized.startsWith('minio/') ||
		normalized.startsWith('openai/minio/') ||
		normalized.startsWith('/v1/files/') ||
		normalized.startsWith('/v1/generated-files/') ||
		normalized.startsWith('api/v1/files/') ||
		normalized.startsWith('api/v1/generated-files/') ||
		normalized.startsWith('openai/v1/files/') ||
		normalized.startsWith('openai/v1/generated-files/') ||
		normalized.startsWith('v1/files/') ||
		normalized.startsWith('v1/generated-files/')
	);
};

const getCanonicalGeneratedFileRef = (value: string | undefined): string => {
	const normalized = normalizeFileRef(value ?? '');
	if (!normalized) return '';

	const knowflowAssetRef = extractKnowflowAssetRef(normalized);
	if (knowflowAssetRef) {
		return `knowflow:${knowflowAssetRef}`;
	}

	const openWebUiFileUrlMatch = normalized.match(OPEN_WEBUI_FILE_URL_REGEX);
	if (openWebUiFileUrlMatch?.[1]) {
		return `openwebui-file:${openWebUiFileUrlMatch[1]}`;
	}

	const openWebUiGeneratedFileUrlMatch = normalized.match(OPEN_WEBUI_GENERATED_FILE_URL_REGEX);
	if (openWebUiGeneratedFileUrlMatch?.[1]) {
		return `openwebui-generated-file:${openWebUiGeneratedFileUrlMatch[1]}`;
	}

	if (isLikelyOpaqueFileId(normalized)) {
		return `openwebui-file:${normalized}`;
	}

	return normalized;
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
		const pythonLikeLiteral = parsePythonLikeLiteral(value);
		return pythonLikeLiteral === value ? value : pythonLikeLiteral;
	}
};

const asToolOutputRecord = (value: unknown): Record<string, unknown> | null => {
	if (!value || typeof value !== 'object' || Array.isArray(value)) {
		return null;
	}
	return value as Record<string, unknown>;
};

const extractToolOutputPayload = (value: unknown): unknown => {
	if (Array.isArray(value)) {
		const text = value
			.map((entry) => {
				if (typeof entry === 'string') return entry;
				const record = asToolOutputRecord(entry);
				if (!record) return '';
				const textValue = record.text ?? record.output ?? record.content;
				return typeof textValue === 'string' ? textValue : '';
			})
			.filter(Boolean)
			.join('\n')
			.trim();
		return text ? parseNestedJSON(text) : value;
	}

	return parseNestedJSON(value);
};

const isSearchToolOutputPayload = (value: unknown): value is Record<string, unknown> => {
	const record = asToolOutputRecord(value);
	if (!record) return false;

	const query = record.query;
	if (typeof query !== 'string' || !query.trim()) {
		return false;
	}

	return (
		Array.isArray(record.results) ||
		Array.isArray(record.data) ||
		Array.isArray(record.images) ||
		Object.prototype.hasOwnProperty.call(record, 'raw')
	);
};

const stringifyToolOutputPayload = (value: unknown): string => {
	if (typeof value === 'string') return value;
	try {
		return JSON.stringify(value, null, 2);
	} catch {
		return String(value ?? '');
	}
};

export const normalizeToolResponseOutput = (value: unknown): unknown => {
	if (!Array.isArray(value)) return value;

	const output = value.map((entry) =>
		entry && typeof entry === 'object' && !Array.isArray(entry)
			? { ...(entry as Record<string, unknown>) }
			: entry
	);
	let changed = false;
	const functionCallByKey = new Map<string, Record<string, unknown>>();

	for (const entry of output) {
		const record = asToolOutputRecord(entry);
		if (!record || record.type !== 'function_call') continue;
		const key = String(record.call_id ?? record.id ?? '').trim();
		if (!key) continue;
		functionCallByKey.set(key, record);
	}

	for (const entry of output) {
		const record = asToolOutputRecord(entry);
		if (!record || record.type !== 'function_call_output') continue;

		const key = String(record.call_id ?? record.id ?? '').trim();
		if (!key) continue;

		const functionCall = functionCallByKey.get(key);
		const toolName = String(functionCall?.name ?? record.name ?? '').trim();
		if (normalizeToolId(toolName) !== 'internet_search') {
			continue;
		}

		const parsedPayload = extractToolOutputPayload(record.output ?? record.result ?? record.content);
		if (!isSearchToolOutputPayload(parsedPayload)) {
			continue;
		}

		const query = String(parsedPayload.query ?? '').trim();
		if (!query) continue;

		if (functionCall) {
			const rawArguments = asToolOutputRecord(functionCall.arguments) ?? {};
			if (!String(rawArguments.query ?? '').trim()) {
				functionCall.arguments = { ...rawArguments, query };
				changed = true;
			}
		}

		const currentQuery = functionCall
			? String(asToolOutputRecord(functionCall.arguments)?.query ?? '').trim()
			: '';
		if (currentQuery !== query) {
			continue;
		}

		const visiblePayload = { ...parsedPayload };
		delete visiblePayload.query;
		if (Object.keys(visiblePayload).length === 0) {
			continue;
		}

		const serializedPayload = stringifyToolOutputPayload(visiblePayload);
		const replacementOutput = [{ type: 'input_text', text: serializedPayload }];
		if (JSON.stringify(record.output ?? null) !== JSON.stringify(replacementOutput)) {
			record.output = replacementOutput;
			changed = true;
		}
	}

	return changed ? output : value;
};

const PYTHON_COLLECTION_LITERAL_REGEX = /^(?:\{[\s\S]*\}|\[[\s\S]*\])$/;
const PYTHON_SINGLE_QUOTED_STRING_REGEX = /'([^'\\]*(?:\\.[^'\\]*)*)'/gs;

const replacePythonKeywordsOutsideStrings = (value: string): string => {
	let output = '';
	let index = 0;
	let inDoubleQuotedString = false;
	let escaped = false;

	const isTokenBoundary = (char: string | undefined): boolean => {
		if (!char) return true;
		return !/[A-Za-z0-9_]/.test(char);
	};

	while (index < value.length) {
		const char = value[index];

		if (inDoubleQuotedString) {
			output += char;
			if (escaped) {
				escaped = false;
			} else if (char === '\\') {
				escaped = true;
			} else if (char === '"') {
				inDoubleQuotedString = false;
			}
			index += 1;
			continue;
		}

		if (char === '"') {
			inDoubleQuotedString = true;
			output += char;
			index += 1;
			continue;
		}

		const slice = value.slice(index);
		if (slice.startsWith('True') && isTokenBoundary(value[index - 1]) && isTokenBoundary(value[index + 4])) {
			output += 'true';
			index += 4;
			continue;
		}
		if (
			slice.startsWith('False') &&
			isTokenBoundary(value[index - 1]) &&
			isTokenBoundary(value[index + 5])
		) {
			output += 'false';
			index += 5;
			continue;
		}
		if (slice.startsWith('None') && isTokenBoundary(value[index - 1]) && isTokenBoundary(value[index + 4])) {
			output += 'null';
			index += 4;
			continue;
		}

		output += char;
		index += 1;
	}

	return output;
};

const clickDownloadLink = (url: string, filename?: string) => {
	if (typeof document === 'undefined') return false;

	const link = document.createElement('a');
	link.href = url;
	link.rel = 'noopener noreferrer';
	if (filename?.trim()) {
		link.download = filename.trim();
	}
	document.body.appendChild(link);
	link.click();
	link.remove();

	return true;
};

export const triggerGeneratedFileDownload = async (value: string, filename?: string) => {
	const normalizedUrl = normalizeOpenWebUiFileUrl(value);
	if (!normalizedUrl) return false;

	const resolvedFileName = filename?.trim() || inferFileName(normalizedUrl);
	if (normalizedUrl.startsWith('data:')) {
		return clickDownloadLink(normalizedUrl, resolvedFileName);
	}

	try {
		const response = await fetch(normalizedUrl, {
			credentials: 'same-origin'
		});
		if (!response.ok) {
			throw new Error(`Generated file download failed with status ${response.status}`);
		}

		const blob = await response.blob();
		const objectUrl = URL.createObjectURL(blob);
		const triggered = clickDownloadLink(objectUrl, resolvedFileName);
		setTimeout(() => {
			URL.revokeObjectURL(objectUrl);
		}, 0);
		return triggered;
	} catch {
		return clickDownloadLink(normalizedUrl, resolvedFileName);
	}
};

const parsePythonLikeLiteral = (value: string): unknown => {
	const trimmed = value.trim();
	if (!trimmed || !PYTHON_COLLECTION_LITERAL_REGEX.test(trimmed)) {
		return value;
	}

	const normalizedQuotedStrings = trimmed.replace(
		PYTHON_SINGLE_QUOTED_STRING_REGEX,
		(_match, inner: string) => JSON.stringify(inner)
	);
	const normalizedLiteral = replacePythonKeywordsOutsideStrings(normalizedQuotedStrings);

	try {
		return JSON.parse(normalizedLiteral);
	} catch {
		return value;
	}
};

export const parseToolCallPayload = (value: string | undefined): unknown => {
	if (!value) return null;
	const decoded = decode(value);
	if (!decoded) return null;
	return parseNestedJSON(decoded);
};

export const getToolCallAttrs = (block: string): Record<string, string> => {
	const openTagMatch = block.match(TOOL_CALL_OPEN_TAG_REGEX);
	if (!openTagMatch) return {};

	const attrs: Record<string, string> = {};
	for (const match of openTagMatch[1].matchAll(TOOL_CALL_ATTR_REGEX)) {
		attrs[match[1]] = match[2];
	}

	return attrs;
};

const getToolCallToolId = (attrs: Record<string, string>): string => {
	return resolveToolDisplay({
		toolId: attrs.tool_id,
		toolName: attrs.tool_name,
		legacyName: attrs.name
	}).toolId;
};

const looksLikeToolTimeout = (value: string): boolean => TOOL_TIMEOUT_TEXT_REGEX.test(value);

const looksLikeToolError = (value: string): boolean => TOOL_ERROR_TEXT_REGEX.test(value);

const getToolErrorText = (value: Record<string, unknown>): string => {
	for (const key of ['error', 'message', 'detail']) {
		const candidate = value[key];
		if (typeof candidate === 'string' && candidate.trim()) {
			return candidate.trim();
		}
		if (candidate !== undefined && candidate !== null && candidate !== false) {
			try {
				return JSON.stringify(candidate);
			} catch {
				return String(candidate);
			}
		}
	}
	return '';
};

const getToolResultStatus = (result: unknown): 'success' | 'error' | 'timeout' | null => {
	if (result && typeof result === 'object' && !Array.isArray(result)) {
		const record = result as Record<string, unknown>;
		const explicitStatus = String(record.status ?? '')
			.trim()
			.toLowerCase();
		if (TOOL_RESULT_TIMEOUT_STATUSES.has(explicitStatus)) {
			return 'timeout';
		}
		if (TOOL_RESULT_ERROR_STATUSES.has(explicitStatus)) {
			return 'error';
		}
		if (TOOL_RESULT_SUCCESS_STATUSES.has(explicitStatus)) {
			return 'success';
		}
		if (record.success === true || record.ok === true) {
			return 'success';
		}
		if (record.success === false || record.ok === false) {
			return looksLikeToolTimeout(getToolErrorText(record)) ? 'timeout' : 'error';
		}
		const errorText = getToolErrorText(record);
		if (errorText) {
			return looksLikeToolTimeout(errorText) ? 'timeout' : 'error';
		}
		return null;
	}

	if (typeof result !== 'string') return null;
	const normalized = result.trim();
	if (!normalized) return null;
	const parsedStructured = parseNestedJSON(normalized);
	if (parsedStructured !== normalized) {
		return getToolResultStatus(parsedStructured);
	}
	if (TOOL_RESULT_STRING_TIMEOUT_REGEX.test(normalized)) return 'timeout';
	if (TOOL_RESULT_STRING_ERROR_REGEX.test(normalized)) return 'error';
	if (TOOL_RESULT_STRING_SUCCESS_REGEX.test(normalized)) return 'success';
	if (looksLikeToolTimeout(normalized)) return 'timeout';
	if (looksLikeToolError(normalized)) return 'error';
	return null;
};

export const getToolCallArtifactEvidence = (attrs: Record<string, string>): boolean => {
	const toolId = getToolCallToolId(attrs);
	if (!FILE_GENERATING_TOOL_IDS.has(toolId)) {
		return false;
	}

	const filesPayload = parseToolCallPayload(attrs.files);
	if (collectGeneratedFilesFromValue(filesPayload, 'tool', undefined, toolId).length > 0) {
		return true;
	}

	const resultPayload = parseToolCallPayload(attrs.result);
	const resultStatus = getToolResultStatus(resultPayload);
	if (resultStatus === 'error' || resultStatus === 'timeout') {
		return false;
	}

	return collectGeneratedFilesFromValue(resultPayload, 'tool', undefined, toolId).length > 0;
};

export const resolveToolCallStatus = (
	attrs: Record<string, string>,
	options?: { promoteArtifactRunning?: boolean }
): 'running' | 'success' | 'error' | 'timeout' => {
	const normalizedStatus = (attrs.status || '').trim().toLowerCase();
	const done = (attrs.done || '').trim().toLowerCase() === 'true';
	const parsedResult = parseToolCallPayload(attrs.result);
	const resultStatus = getToolResultStatus(parsedResult);
	const hasExplicitStatus =
		normalizedStatus === 'running' ||
		normalizedStatus === 'success' ||
		normalizedStatus === 'error' ||
		normalizedStatus === 'timeout';

	if (resultStatus && typeof parsedResult !== 'string') {
		return resultStatus;
	}
	if (hasExplicitStatus) {
		return normalizedStatus as 'running' | 'success' | 'error' | 'timeout';
	}
	if (resultStatus) {
		return resultStatus;
	}

	const hasArtifacts = getToolCallArtifactEvidence(attrs);
	if (hasArtifacts && done) {
		return 'success';
	}
	if (
		hasArtifacts &&
		(options?.promoteArtifactRunning ?? true) &&
		(normalizedStatus === 'running' || !normalizedStatus)
	) {
		return 'success';
	}
	return done ? 'success' : 'running';
};

const upsertToolCallAttr = (openTag: string, name: string, value: string): string => {
	const attrRegex = new RegExp(`\\s${name}="[^"]*"`, 'i');
	if (attrRegex.test(openTag)) {
		return openTag.replace(attrRegex, ` ${name}="${value}"`);
	}
	return openTag.replace(/>$/, ` ${name}="${value}">`);
};

export const normalizeToolCallBlockMarkup = (block: string): string => {
	const openTagMatch = block.match(/^<details\b[^>]*>/i);
	if (!openTagMatch) return block;

	const attrs = getToolCallAttrs(block);
	if (Object.keys(attrs).length === 0) return block;

	const status = resolveToolCallStatus(attrs);
	const done = status === 'running' ? 'false' : 'true';
	let normalizedOpenTag = upsertToolCallAttr(openTagMatch[0], 'status', status);
	normalizedOpenTag = upsertToolCallAttr(normalizedOpenTag, 'done', done);

	return `${normalizedOpenTag}${block.slice(openTagMatch[0].length)}`;
};

export const normalizeToolCallContent = (content: string): string => {
	if (!content || !content.includes('type="tool_calls"')) return content;
	return content.replace(TOOL_CALL_BLOCK_REGEX, (block) => normalizeToolCallBlockMarkup(block));
};

export const toGeneratedFile = (
	item: unknown,
	source: string,
	timestamp?: number,
	toolId?: string
): GeneratedFileItem | null => {
	if (typeof item === 'string') {
		const ref = resolveStringFileReference(item);
		if (!ref) return null;
		const name = sanitizeGeneratedFileName(undefined, ref);

		if (!isDownloadRef(ref) && !isLikelyOpaqueFileId(ref)) {
			return {
				id: `${source}:${ref}:${name}`,
				name,
				path: ref,
				downloadMode: 'terminal',
				source,
				toolId,
				isImage: isImageRef(name),
				timestamp
			};
		}

		return {
			id: `${source}:${ref}`,
			name,
			url: normalizeOpenWebUiFileUrl(ref),
			downloadMode: 'link',
			source,
			toolId,
			isImage: isImageRef(name),
			timestamp
		};
	}

	if (!item || typeof item !== 'object') return null;

	const record = item as Record<string, unknown>;
	const urlRef = normalizeFileRef(record.url);
	const ref =
		normalizeFileRef(record.bridge_url) ??
		normalizeFileRef(record.generated_file_url) ??
		normalizeFileRef(record.download_url) ??
		normalizeFileRef(record.downloadUrl) ??
		normalizeFileRef(record.bridge_file_id) ??
		normalizeFileRef(record.file_id) ??
		normalizeFileRef(record.fileId) ??
		(urlRef && isDownloadRef(urlRef) ? urlRef : null);
	const terminalPath =
		normalizeFileRef(record.path) ??
		normalizeFileRef(record.output_path) ??
		normalizeFileRef(record.target_path) ??
		normalizeFileRef(record.file_path);

	if (!ref && !terminalPath) return null;

	const fallbackFileRef = ref || terminalPath || '';
	const name =
		(typeof record.name === 'string' && record.name.trim()) ||
		(typeof record.filename === 'string' && record.filename.trim()) ||
		(typeof record.fileName === 'string' && record.fileName.trim()) ||
		inferFileName(fallbackFileRef);
	const sanitizedName = sanitizeGeneratedFileName(name, fallbackFileRef);

	const type = typeof record.type === 'string' ? record.type : undefined;
	const contentType =
		typeof record.content_type === 'string' ? record.content_type : undefined;

	if (!ref && terminalPath && !isDownloadRef(terminalPath)) {
		return {
			id: `${source}:${terminalPath}:${sanitizedName}`,
			name: sanitizedName,
			path: terminalPath,
			downloadMode: 'terminal',
			source,
			toolId,
			size: typeof record.size === 'number' ? record.size : undefined,
			type,
			contentType,
			isImage: isImageRef(sanitizedName, type, contentType),
			timestamp
		};
	}

	if (!ref) return null;

	return {
		id: `${source}:${terminalPath ?? ref}:${sanitizedName}`,
		name: sanitizedName,
		url: normalizeOpenWebUiFileUrl(ref),
		path: terminalPath ?? undefined,
		downloadMode: 'link',
		source,
		toolId,
		size: typeof record.size === 'number' ? record.size : undefined,
		type,
		contentType,
		isImage: isImageRef(sanitizedName, type, contentType),
		timestamp
	};
};

const normalizeGeneratedFileNameForMatching = (name: string | undefined): string => {
	return (name ?? '').trim().toLowerCase();
};

const getGeneratedFilePathRef = (file: GeneratedFileItem): string => {
	return getCanonicalGeneratedFileRef(file.path ?? '');
};

const getGeneratedFileDownloadRef = (file: GeneratedFileItem): string => {
	return getCanonicalGeneratedFileRef(file.url ?? '');
};

const getGeneratedFilePrimaryRef = (file: GeneratedFileItem): string => {
	return getGeneratedFilePathRef(file) || getGeneratedFileDownloadRef(file);
};

const getGeneratedFileBasename = (file: GeneratedFileItem): string => {
	const normalizedName = normalizeGeneratedFileNameForMatching(file.name);
	if (normalizedName && normalizedName !== 'generated-file') {
		return normalizedName;
	}

	const ref = getGeneratedFilePrimaryRef(file);
	return normalizeGeneratedFileNameForMatching(inferFileName(ref || file.name || ''));
};

const isSameGeneratedFile = (
	existing: GeneratedFileItem | undefined,
	incoming: GeneratedFileItem
): boolean => {
	if (!existing) return false;

	const existingPathRef = getGeneratedFilePathRef(existing);
	const incomingPathRef = getGeneratedFilePathRef(incoming);
	if (existingPathRef && incomingPathRef && existingPathRef === incomingPathRef) {
		return true;
	}

	const existingDownloadRef = getGeneratedFileDownloadRef(existing);
	const incomingDownloadRef = getGeneratedFileDownloadRef(incoming);
	if (existingDownloadRef && incomingDownloadRef && existingDownloadRef === incomingDownloadRef) {
		return true;
	}

	const existingRef = getGeneratedFilePrimaryRef(existing);
	const incomingRef = getGeneratedFilePrimaryRef(incoming);
	if (existingRef && incomingRef && existingRef === incomingRef) {
		return true;
	}

	const existingName = normalizeGeneratedFileNameForMatching(existing.name);
	const incomingName = normalizeGeneratedFileNameForMatching(incoming.name);
	if (!existingName || !incomingName || existingName !== incomingName) {
		return false;
	}

	if (
		existing.timestamp !== undefined &&
		incoming.timestamp !== undefined &&
		existing.timestamp !== incoming.timestamp
	) {
		return false;
	}

	if (
		existing.size !== undefined &&
		incoming.size !== undefined &&
		existing.size !== incoming.size
	) {
		return false;
	}

	const existingBasename = getGeneratedFileBasename(existing);
	const incomingBasename = getGeneratedFileBasename(incoming);
	if (!existingBasename || existingBasename !== incomingBasename) {
		return false;
	}

	return (
		existing.downloadMode === incoming.downloadMode &&
		existing.source === incoming.source &&
		(!existingRef || !incomingRef)
	);
};

const scoreGeneratedFileName = (name: string | undefined): number => {
	const normalized = (name ?? '').trim();
	if (!normalized) return 0;
	if (normalized === 'generated-file') return 1;
	if (normalized.length <= 3) return 2;
	return normalized.length;
};

const scoreGeneratedFileUrl = (value: string | undefined): number => {
	const normalized = normalizeKnowflowAssetUrl(value ?? '');
	if (!normalized) return 0;

	const lowered = normalized.toLowerCase();
	if (lowered.includes('/api/v1/files/') && lowered.includes('/content')) {
		return 5;
	}
	if (lowered.includes('/api/v1/files/')) {
		return 4;
	}
	if (extractKnowflowAssetRef(normalized)) {
		return 3;
	}
	if (lowered.startsWith('https://') || lowered.startsWith('http://')) {
		return 2;
	}
	return 0;
};

const mergeGeneratedFile = (
	existing: GeneratedFileItem | undefined,
	incoming: GeneratedFileItem
): GeneratedFileItem => {
	if (!existing) return incoming;

	const existingNameScore = scoreGeneratedFileName(existing.name);
	const incomingNameScore = scoreGeneratedFileName(incoming.name);
	const preferredName = incomingNameScore >= existingNameScore ? incoming.name : existing.name;
	const existingUrl = normalizeKnowflowAssetUrl(existing.url) ?? existing.url;
	const incomingUrl = normalizeKnowflowAssetUrl(incoming.url) ?? incoming.url;
	const preferredUrl =
		scoreGeneratedFileUrl(existingUrl) >= scoreGeneratedFileUrl(incomingUrl)
			? (existingUrl ?? incomingUrl)
			: (incomingUrl ?? existingUrl);
	const preferredPath = incoming.path ?? existing.path;
	const preferredDownloadMode =
		preferredUrl && (!preferredPath || incoming.downloadMode === 'link' || existing.downloadMode === 'link')
			? 'link'
			: incoming.downloadMode ?? existing.downloadMode;
	const preferredId =
		incomingNameScore >= existingNameScore ? incoming.id ?? existing.id : existing.id ?? incoming.id;

	return {
		...existing,
		...incoming,
		name: preferredName,
		url: preferredUrl ? normalizeOpenWebUiFileUrl(preferredUrl) : preferredUrl,
		path: preferredPath,
		downloadMode: preferredDownloadMode,
		id: preferredId,
		timestamp: Math.max(existing.timestamp ?? 0, incoming.timestamp ?? 0) || undefined
	};
};

const shouldCollapseDocumentVariant = (file: GeneratedFileItem): boolean => {
	const extension = getFileExtension(file.name || file.url || file.path);
	return FINAL_DOCUMENT_EXTENSIONS.has(extension);
};

const pickPreferredDocumentVariant = (
	existing: GeneratedFileItem,
	incoming: GeneratedFileItem
): GeneratedFileItem => {
	const existingSize = typeof existing.size === 'number' ? existing.size : null;
	const incomingSize = typeof incoming.size === 'number' ? incoming.size : null;

	if (existingSize !== null && incomingSize !== null && existingSize !== incomingSize) {
		return incomingSize > existingSize ? mergeGeneratedFile(existing, incoming) : existing;
	}

	return mergeGeneratedFile(existing, incoming);
};

const collapseGeneratedDocumentVariants = (files: GeneratedFileItem[]): GeneratedFileItem[] => {
	const collapsed: GeneratedFileItem[] = [];
	const indexByLogicalName = new Map<string, number>();

	for (const file of files) {
		if (!shouldCollapseDocumentVariant(file)) {
			collapsed.push(file);
			continue;
		}

		const logicalName = getGeneratedFileBasename(file);
		if (!logicalName) {
			collapsed.push(file);
			continue;
		}

		const extension = getFileExtension(file.name || file.url || file.path);
		const key = `${logicalName}::${extension}`;
		const existingIndex = indexByLogicalName.get(key);
		if (existingIndex === undefined) {
			indexByLogicalName.set(key, collapsed.length);
			collapsed.push(file);
			continue;
		}

		collapsed[existingIndex] = pickPreferredDocumentVariant(collapsed[existingIndex], file);
	}

	return collapsed;
};

const dedupeGeneratedFiles = (files: GeneratedFileItem[]): GeneratedFileItem[] => {
	const deduped: GeneratedFileItem[] = [];

	for (const file of files) {
		const duplicateIndex = deduped.findIndex((existing) => isSameGeneratedFile(existing, file));
		if (duplicateIndex === -1) {
			deduped.push(file);
			continue;
		}

		deduped[duplicateIndex] = mergeGeneratedFile(deduped[duplicateIndex], file);
	}

	return collapseGeneratedDocumentVariants(deduped);
};

export const getFileExtension = (value: string | undefined): string => {
	const normalized = (value ?? '').trim().toLowerCase();
	if (!normalized) return '';
	const basename = inferFileName(normalized, normalized).toLowerCase();
	const extension = basename.split('.').pop() || '';
	return extension === basename ? '' : extension;
};

const normalizeContentType = (value: string | undefined): string => {
	return (value ?? '').trim().toLowerCase();
};

const hasExplicitGeneratedFileName = (file: GeneratedFileItem): boolean => {
	const candidate = inferFileName(file.name || file.url || file.path || '');
	if (!candidate) return false;
	if (!FILE_NAME_WITH_EXTENSION_REGEX.test(candidate)) return false;
	const base = candidate.replace(/\.[^.]+$/, '');
	if (!base || base === 'generated-file') return false;
	return !isLikelyOpaqueFileId(base);
};

const isWorkbookArtifact = (file: GeneratedFileItem): boolean => {
	const extension = getFileExtension(file.name || file.url || file.path);
	if (extension === 'xlsx' || extension === 'xls') return true;
	const contentType = normalizeContentType(file.contentType || file.type);
	if (!contentType) return false;
	return WORKBOOK_CONTENT_TYPE_REGEX.test(contentType);
};

const collapseIntermediateWorkbookArtifacts = (
	files: GeneratedFileItem[]
): GeneratedFileItem[] => {
	const workbookIndexes: number[] = [];
	const explicitNames = new Set<string>();
	const explicitByIndex = new Map<number, boolean>();

	for (let idx = 0; idx < files.length; idx += 1) {
		const file = files[idx];
		if (!isWorkbookArtifact(file)) {
			continue;
		}
		workbookIndexes.push(idx);
		const isExplicit = hasExplicitGeneratedFileName(file);
		explicitByIndex.set(idx, isExplicit);
		if (isExplicit) {
			const normalizedName = normalizeGeneratedFileNameForMatching(
				inferFileName(file.name || file.url || file.path || '')
			);
			if (normalizedName) {
				explicitNames.add(normalizedName);
			}
		}
	}

	if (workbookIndexes.length <= 1) {
		return files;
	}

	const normalizedVariantNames = new Set(
		Array.from(explicitNames).map((name) =>
			name.replace(/(?:[_\-\s]|\s*\()\d+\)?(?=\.[^.]+$)/, '')
		)
	);

	if (explicitNames.size > 1 && normalizedVariantNames.size > 1) {
		return files;
	}

	let selectedIndex = -1;
	for (const idx of workbookIndexes) {
		if (explicitByIndex.get(idx)) {
			selectedIndex = idx;
		}
	}
	if (selectedIndex < 0) {
		selectedIndex = workbookIndexes[workbookIndexes.length - 1];
	}

	return files.filter((file, idx) => !isWorkbookArtifact(file) || idx === selectedIndex);
};

export const isPrimaryDocumentArtifact = (file: GeneratedFileLike): boolean => {
	if (file.toolId && FINAL_DOCUMENT_TOOL_IDS.has(file.toolId)) {
		return true;
	}
	const extension = getFileExtension(file.name || file.url || file.path);
	if (extension && FINAL_DOCUMENT_EXTENSIONS.has(extension)) {
		return true;
	}
	const contentType = normalizeContentType(file.contentType || file.type);
	if (!contentType) return false;
	return WORKBOOK_CONTENT_TYPE_REGEX.test(contentType) || contentType.includes('pdf');
};

const matchesHelperArtifactPattern = (file: GeneratedFileLike): boolean => {
	if (!HELPER_ARTIFACT_EXTENSIONS.has(getFileExtension(file.name || file.url || file.path))) {
		return false;
	}
	const normalizedName = inferFileName(file.name || file.url || file.path || '').toLowerCase();
	return HELPER_ARTIFACT_NAME_REGEX.test(normalizedName);
};

export const shouldHideHelperArtifact = (
	file: GeneratedFileLike,
	hasFinalDocumentArtifact: boolean
): boolean => {
	if (!hasFinalDocumentArtifact) return false;
	const hasHelperExtension = HELPER_ARTIFACT_EXTENSIONS.has(
		getFileExtension(file.name || file.url || file.path)
	);
	if (!hasHelperExtension) return false;
	if (file.toolId) {
		return HELPER_ARTIFACT_TOOL_IDS.has(file.toolId) || matchesHelperArtifactPattern(file);
	}
	if (file.source === 'user') return false;
	return true;
};

const filterGeneratedFilesForDisplay = (files: GeneratedFileItem[]): GeneratedFileItem[] => {
	const hasFinalDocumentArtifact = files.some((file) => isPrimaryDocumentArtifact(file));
	const filtered = hasFinalDocumentArtifact
		? files.filter((file) => !shouldHideHelperArtifact(file, hasFinalDocumentArtifact))
		: files;
	return collapseIntermediateWorkbookArtifacts(filtered);
};

export const collectGeneratedFilesFromValue = (
	value: unknown,
	source: string,
	timestamp?: number,
	toolId?: string
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

		const normalized = toGeneratedFile(entry, source, timestamp, toolId);
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

export const collectGeneratedFilesFromResponseOutput = (
	value: unknown,
	source = 'assistant',
	timestamp?: number
): GeneratedFileItem[] => {
	const files: GeneratedFileItem[] = [];

	const collectCandidates = (record: Record<string, unknown>, toolId?: string) => {
		for (const key of ('generated_file generatedFile generated_files generatedFiles').split(' ')) {
			if (key in record) {
				files.push(...collectGeneratedFilesFromValue(record[key], source, timestamp, toolId));
			}
		}
	};

	const resolveToolIdFromRecord = (
		record: Record<string, unknown>,
		toolCallIds?: Map<string, string>,
		fallbackToolId?: string
	): string | undefined => {
		const rawToolId =
			(typeof record.tool_id === 'string' && record.tool_id) ||
			(typeof record.toolId === 'string' && record.toolId) ||
			(typeof record.tool_name === 'string' && record.tool_name) ||
			(typeof record.toolName === 'string' && record.toolName) ||
			(typeof record.name === 'string' && record.name) ||
			undefined;

		let resolved = rawToolId
			? resolveToolDisplay({
					toolId: rawToolId,
					toolName: rawToolId,
					legacyName: rawToolId
				}).toolId
			: '';

		const callId =
			(typeof record.call_id === 'string' && record.call_id) ||
			(typeof record.callId === 'string' && record.callId) ||
			(typeof record.id === 'string' && record.id) ||
			'';
		if (!resolved && callId && toolCallIds && toolCallIds.has(callId)) {
			resolved = toolCallIds.get(callId) ?? '';
		}

		return resolved || fallbackToolId;
	};

	const visit = (
		entry: unknown,
		toolCallIds?: Map<string, string>,
		fallbackToolId?: string
	) => {
		if (entry === null || entry === undefined) return;

		if (typeof entry === 'string') {
			const parsed = parseNestedJSON(entry);
			if (parsed !== entry) {
				visit(parsed, toolCallIds, fallbackToolId);
			}
			return;
		}

		if (Array.isArray(entry)) {
			const callIds = new Map<string, string>();
			for (const item of entry) {
				if (!item || typeof item !== 'object') continue;
				const record = item as Record<string, unknown>;
				const itemType = typeof record.type === 'string' ? record.type : '';
				if (itemType !== 'function_call') continue;
				const callId =
					(typeof record.call_id === 'string' && record.call_id) ||
					(typeof record.callId === 'string' && record.callId) ||
					(typeof record.id === 'string' && record.id) ||
					'';
				const toolName =
					(typeof record.name === 'string' && record.name) ||
					(typeof record.tool_name === 'string' && record.tool_name) ||
					(typeof record.toolName === 'string' && record.toolName) ||
					(typeof record.tool_id === 'string' && record.tool_id) ||
					(typeof record.toolId === 'string' && record.toolId) ||
					'';
				if (!callId || !toolName) continue;
				const resolved = resolveToolDisplay({
					toolId: toolName,
					toolName,
					legacyName: toolName
				}).toolId;
				if (resolved) {
					callIds.set(callId, resolved);
				}
			}

			for (const item of entry) {
				visit(item, callIds.size > 0 ? callIds : toolCallIds, fallbackToolId);
			}
			return;
		}

		if (!entry || typeof entry !== 'object') {
			return;
		}

		const record = entry as Record<string, unknown>;
		const toolId = resolveToolIdFromRecord(record, toolCallIds, fallbackToolId);
		collectCandidates(record, toolId);

		const metadata = record.metadata;
		if (metadata && typeof metadata === 'object' && !Array.isArray(metadata)) {
			collectCandidates(metadata as Record<string, unknown>, toolId);
		}

		for (const key of ['output', 'content', 'result', 'files', 'outputs', 'artifacts', 'attachments']) {
			if (key in record) {
				visit(record[key], toolCallIds, toolId);
			}
		}
	};

	visit(value);
	return filterGeneratedFilesForDisplay(dedupeGeneratedFiles(files));
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
		files.push(...collectGeneratedFilesFromValue(parsedFiles, 'tool', timestamp, toolId));

		const parsedResult = parseNestedJSON(decode(attrs.result ?? ''));
		if (
			parsedResult &&
			typeof parsedResult === 'object' &&
			!Array.isArray(parsedResult) &&
			(parsedResult as Record<string, unknown>).success === false
		) {
			continue;
		}

		files.push(...collectGeneratedFilesFromValue(parsedResult, 'tool', timestamp, toolId));
	}

	return filterGeneratedFilesForDisplay(dedupeGeneratedFiles(files));
};

export const collectGeneratedFilesFromMessage = (messageData: any): GeneratedFileItem[] => {
	if (!messageData) return [];

	const source =
		messageData?.role === 'assistant' ? 'assistant' : messageData?.role || 'message';
	const timestamp =
		typeof messageData?.timestamp === 'number' ? messageData.timestamp : undefined;
	const hasMessageFiles = Array.isArray(messageData?.files) && messageData.files.length > 0;
	const outputFiles = collectGeneratedFilesFromResponseOutput(
		messageData?.output,
		source,
		timestamp
	);
	const content = typeof messageData?.content === 'string' ? messageData.content : '';
	const hasPotentialContent = hasPotentialGeneratedFilesInContent(content);

	if (!hasMessageFiles && outputFiles.length === 0 && !hasPotentialContent) {
		return [];
	}

	const cacheKey = buildGeneratedFilesMessageCacheKey(messageData, source, timestamp);
	const cached = messageGeneratedFilesCache.get(cacheKey);
	if (cached) return cached;

	const files: GeneratedFileItem[] = [];

	if (Array.isArray(messageData?.files)) {
		files.push(...collectGeneratedFilesFromValue(messageData.files, source, timestamp));
	}

	files.push(...outputFiles);
	files.push(...extractGeneratedFilesFromToolBlocks(messageData?.content ?? '', timestamp));

	const deduped = filterGeneratedFilesForDisplay(dedupeGeneratedFiles(files));
	setGeneratedFilesCache(messageGeneratedFilesCache, cacheKey, deduped);
	return deduped;
};

export const collectGeneratedFilesFromHistory = (history: any): GeneratedFileItem[] => {
	const messages = Object.values(history?.messages ?? {}) as Array<Record<string, unknown>>;
	if (!hasGeneratedFilesInHistory(history)) {
		return [];
	}

	const historyCacheKey = buildGeneratedFilesHistoryCacheKey(messages);
	const cached = historyGeneratedFilesCache.get(historyCacheKey);
	if (cached) return cached;

	const result: GeneratedFileItem[] = [];

	for (const message of messages) {
		result.push(...collectGeneratedFilesFromMessage(message));
	}

	// Messages are already filtered individually; re-filtering the merged history would hide
	// valid text artifacts from one turn just because another turn produced a PDF/XLSX.
	const deduped = dedupeGeneratedFiles(result).sort(
		(a, b) => (b.timestamp ?? 0) - (a.timestamp ?? 0)
	);
	setGeneratedFilesCache(historyGeneratedFilesCache, historyCacheKey, deduped);
	return deduped;
};
