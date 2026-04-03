import {
	compareVersion,
	extractFrontmatter,
	removeAllDetails,
	removeDetails
} from '$lib/utils';

type ToolVisibility = 'public' | 'restricted' | 'hidden';

type ToolDraftMeta = {
	description: string;
	manifest: Record<string, string>;
	published: boolean;
	category: string;
	visibility: ToolVisibility;
	dependencies: string[];
	is_default: boolean;
};

export type ParsedToolDraft = {
	id: string;
	name: string;
	content: string;
	meta: ToolDraftMeta;
	access_grants: any[];
	source: 'structured' | 'python';
};

const CODE_BLOCK_REGEX = /```([a-zA-Z0-9_-]+)?[ \t]*\n([\s\S]*?)```/g;
const HEADING_REGEX = /^\s{0,3}#{1,6}\s+(.+?)\s*$/m;
const METHOD_REGEX = /^\s+def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/gm;

const shortHash = (value: string) => {
	let hash = 2166136261;

	for (let index = 0; index < value.length; index += 1) {
		hash ^= value.charCodeAt(index);
		hash = Math.imul(hash, 16777619);
	}

	return (hash >>> 0).toString(36).slice(0, 6);
};

const humanizeIdentifier = (value: string) =>
	value
		.replace(/[_-]+/g, ' ')
		.replace(/\s+/g, ' ')
		.trim()
		.replace(/\b\w/g, (match) => match.toUpperCase());

const sanitizeToolId = (value: string) => {
	const sanitized = value
		.toLowerCase()
		.replace(/[^a-z0-9_]+/g, '_')
		.replace(/_+/g, '_')
		.replace(/^_+|_+$/g, '');

	if (!sanitized) {
		return 'generated_tool';
	}

	if (/^[0-9]/.test(sanitized)) {
		return `tool_${sanitized}`;
	}

	return sanitized;
};

const normalizeVisibility = (value: unknown): ToolVisibility => {
	const normalized = `${value ?? ''}`.trim().toLowerCase();

	if (
		['hidden', 'private', 'owner', 'owner_only', 'creator_only', 'creator-only'].includes(
			normalized
		)
	) {
		return 'hidden';
	}

	if (['restricted', 'shared', 'internal'].includes(normalized)) {
		return 'restricted';
	}

	if (normalized === 'public') {
		return 'public';
	}

	return 'hidden';
};

const normalizeDependencies = (value: unknown): string[] => {
	if (Array.isArray(value)) {
		return value.map((entry) => `${entry}`.trim()).filter(Boolean);
	}

	if (typeof value === 'string') {
		return value
			.split(/[,\n]/)
			.map((entry) => entry.trim())
			.filter(Boolean);
	}

	return [];
};

const toBoolean = (value: unknown, fallback = false) => {
	if (typeof value === 'boolean') return value;
	if (typeof value === 'string') {
		const normalized = value.trim().toLowerCase();
		if (normalized === 'true') return true;
		if (normalized === 'false') return false;
	}
	return fallback;
};

const stripCodeBlocks = (content: string) => content.replace(CODE_BLOCK_REGEX, '').trim();

const extractHeading = (content: string) => {
	const match = stripCodeBlocks(content).match(HEADING_REGEX);
	return match?.[1]?.trim() || '';
};

const extractDescription = (content: string) => {
	const cleaned = stripCodeBlocks(content)
		.split('\n')
		.map((line) => line.trim())
		.filter(Boolean)
		.filter((line) => !line.startsWith('#'))
		.filter((line) => !line.startsWith('>'))
		.filter((line) => !line.startsWith('- '))
		.filter((line) => !line.startsWith('* '));

	return cleaned[0] || '';
};

const extractPrimaryMethodName = (code: string) => {
	for (const match of code.matchAll(METHOD_REGEX)) {
		const methodName = match[1];
		if (!methodName || methodName === '__init__' || methodName.startsWith('_')) {
			continue;
		}

		return methodName;
	}

	return '';
};

const buildName = (
	explicitName: unknown,
	manifest: Record<string, string>,
	surroundingContent: string,
	code: string
) => {
	const candidate =
		`${explicitName ?? ''}`.trim() ||
		manifest.title?.trim() ||
		manifest.name?.trim() ||
		extractHeading(surroundingContent) ||
		humanizeIdentifier(extractPrimaryMethodName(code)) ||
		'Generated Tool';

	return candidate;
};

const buildId = (
	explicitId: unknown,
	name: string,
	content: string,
	manifest: Record<string, string>
) => {
	const manifestId = manifest.id?.trim();
	const preferredId = `${explicitId ?? ''}`.trim() || manifestId || '';

	if (preferredId) {
		return sanitizeToolId(preferredId);
	}

	return `${sanitizeToolId(name)}_${shortHash(content)}`;
};

const buildMeta = (
	meta: Record<string, any>,
	manifest: Record<string, string>,
	surroundingContent: string
): ToolDraftMeta => ({
	description:
		`${meta?.description ?? ''}`.trim() || manifest.description?.trim() || extractDescription(surroundingContent),
	manifest,
	published: toBoolean(meta?.published ?? manifest.published, false),
	category: `${meta?.category ?? manifest.category ?? ''}`.trim(),
	visibility: normalizeVisibility(meta?.visibility ?? manifest.visibility),
	dependencies: normalizeDependencies(meta?.dependencies ?? manifest.dependencies),
	is_default: false
});

const parseStructuredDraft = (code: string, surroundingContent: string): ParsedToolDraft | null => {
	if (!code.trim().startsWith('{')) {
		return null;
	}

	try {
		const parsed = JSON.parse(code);
		const payload = parsed?.tool ?? parsed;
		const toolContent = `${payload?.content ?? payload?.code ?? ''}`.trim();

		if (!toolContent.includes('class Tools:')) {
			return null;
		}

		const manifest = extractFrontmatter(toolContent);
		const name = buildName(payload?.name ?? payload?.title, manifest, surroundingContent, toolContent);

		return {
			id: buildId(payload?.id, name, toolContent, manifest),
			name,
			content: toolContent,
			meta: buildMeta(payload?.meta ?? payload ?? {}, manifest, surroundingContent),
			access_grants: Array.isArray(payload?.access_grants) ? payload.access_grants : [],
			source: 'structured'
		};
	} catch {
		return null;
	}
};

const parsePythonDraft = (code: string, surroundingContent: string): ParsedToolDraft | null => {
	if (!code.includes('class Tools:')) {
		return null;
	}

	const manifest = extractFrontmatter(code);
	const name = buildName('', manifest, surroundingContent, code);

	return {
		id: buildId('', name, code, manifest),
		name,
		content: code.trim(),
		meta: buildMeta({}, manifest, surroundingContent),
		access_grants: [],
		source: 'python'
	};
};

export const parseToolDraftFromMessageContent = (content: string): ParsedToolDraft | null => {
	const cleaned = removeAllDetails(removeDetails(content ?? '', ['tool_calls']));

	const codeBlocks = Array.from(cleaned.matchAll(CODE_BLOCK_REGEX)).map((match) => ({
		lang: `${match[1] ?? ''}`.trim().toLowerCase(),
		code: `${match[2] ?? ''}`.trim()
	}));

	for (const block of codeBlocks) {
		const parsed = parseStructuredDraft(block.code, cleaned);
		if (parsed) {
			return parsed;
		}
	}

	for (const block of codeBlocks) {
		if (block.lang && !['python', 'py', 'tool'].includes(block.lang)) {
			continue;
		}

		const parsed = parsePythonDraft(block.code, cleaned);
		if (parsed) {
			return parsed;
		}
	}

	return null;
};

export const getToolVersionRequirement = (content: string, currentVersion: string) => {
	const manifest = extractFrontmatter(content);
	const requiredVersion = manifest?.required_open_webui_version ?? '0.0.0';

	return compareVersion(requiredVersion, currentVersion) ? requiredVersion : null;
};
