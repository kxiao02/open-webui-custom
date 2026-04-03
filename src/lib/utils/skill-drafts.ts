import { formatSkillName, parseFrontmatter, removeAllDetails, removeDetails, slugify } from '$lib/utils';

type SkillVisibility = 'public' | 'restricted' | 'hidden';

type SkillDraftMeta = {
	tags: string[];
	published: boolean;
	category: string;
	visibility: SkillVisibility;
	dependencies: string[];
	is_default: boolean;
};

export type ParsedSkillDraft = {
	id: string;
	name: string;
	description: string;
	content: string;
	meta: SkillDraftMeta;
	access_grants: any[];
	source: 'structured' | 'markdown';
};

const CODE_BLOCK_REGEX = /```([a-zA-Z0-9_-]+)?[ \t]*\n([\s\S]*?)```/g;
const HEADING_REGEX = /^\s{0,3}#{1,6}\s+(.+?)\s*$/m;

const shortHash = (value: string) => {
	let hash = 2166136261;

	for (let index = 0; index < value.length; index += 1) {
		hash ^= value.charCodeAt(index);
		hash = Math.imul(hash, 16777619);
	}

	return (hash >>> 0).toString(36).slice(0, 6);
};

const normalizeVisibility = (value: unknown): SkillVisibility => {
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

const normalizeList = (value: unknown): string[] => {
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

const stripFrontmatter = (content: string) => content.replace(/^---\s*\n[\s\S]*?\n---\s*/m, '').trim();

const extractHeading = (content: string) => {
	const body = stripFrontmatter(content);
	return body.match(HEADING_REGEX)?.[1]?.trim() || '';
};

const extractDescription = (content: string) => {
	const body = stripFrontmatter(content);
	const lines = body
		.split('\n')
		.map((line) => line.trim())
		.filter(Boolean)
		.filter((line) => !line.startsWith('#'))
		.filter((line) => !line.startsWith('>'))
		.filter((line) => !line.startsWith('- '))
		.filter((line) => !line.startsWith('* '))
		.filter((line) => !line.startsWith('```'));

	return lines[0] || '';
};

const buildName = (explicitName: unknown, frontmatter: Record<string, string>, content: string) => {
	const candidate =
		`${explicitName ?? ''}`.trim() ||
		frontmatter.name?.trim() ||
		extractHeading(content) ||
		'Generated Skill';

	return formatSkillName(candidate);
};

const buildId = (
	explicitId: unknown,
	name: string,
	content: string,
	frontmatter: Record<string, string>
) => {
	const preferredId = `${explicitId ?? ''}`.trim() || frontmatter.id?.trim() || '';
	if (preferredId) {
		return slugify(preferredId);
	}

	const nameSlug = slugify(name);
	return nameSlug ? `${nameSlug}-${shortHash(content)}` : `generated-skill-${shortHash(content)}`;
};

const buildMeta = (
	meta: Record<string, any>,
	frontmatter: Record<string, string>,
	content: string
): SkillDraftMeta => ({
	tags: normalizeList(meta?.tags ?? frontmatter.tags),
	published: toBoolean(meta?.published ?? frontmatter.published, false),
	category: `${meta?.category ?? frontmatter.category ?? ''}`.trim(),
	visibility: normalizeVisibility(meta?.visibility ?? frontmatter.visibility),
	dependencies: normalizeList(meta?.dependencies ?? frontmatter.dependencies),
	is_default: false
});

const parseStructuredDraft = (code: string): ParsedSkillDraft | null => {
	if (!code.trim().startsWith('{')) {
		return null;
	}

	try {
		const parsed = JSON.parse(code);
		const payload = parsed?.skill ?? parsed;
		const content = `${payload?.content ?? payload?.instructions ?? payload?.markdown ?? ''}`.trim();

		if (!content) {
			return null;
		}

		const frontmatter = parseFrontmatter(content);
		const name = buildName(payload?.name ?? payload?.title, frontmatter, content);
		const description =
			`${payload?.description ?? payload?.meta?.description ?? ''}`.trim() ||
			frontmatter.description?.trim() ||
			extractDescription(content);

		return {
			id: buildId(payload?.id, name, content, frontmatter),
			name,
			description,
			content,
			meta: buildMeta(payload?.meta ?? payload ?? {}, frontmatter, content),
			access_grants: Array.isArray(payload?.access_grants) ? payload.access_grants : [],
			source: 'structured'
		};
	} catch {
		return null;
	}
};

const parseMarkdownDraft = (code: string): ParsedSkillDraft | null => {
	const content = code.trim();
	const frontmatter = parseFrontmatter(content);

	if (!frontmatter.name && !frontmatter.id) {
		return null;
	}

	const name = buildName('', frontmatter, content);
	const description = frontmatter.description?.trim() || extractDescription(content);

	return {
		id: buildId('', name, content, frontmatter),
		name,
		description,
		content,
		meta: buildMeta({}, frontmatter, content),
		access_grants: [],
		source: 'markdown'
	};
};

export const parseSkillDraftFromMessageContent = (content: string): ParsedSkillDraft | null => {
	const cleaned = removeAllDetails(removeDetails(content ?? '', ['tool_calls']));

	const codeBlocks = Array.from(cleaned.matchAll(CODE_BLOCK_REGEX)).map((match) => ({
		lang: `${match[1] ?? ''}`.trim().toLowerCase(),
		code: `${match[2] ?? ''}`.trim()
	}));

	for (const block of codeBlocks) {
		const parsed = parseStructuredDraft(block.code);
		if (parsed) {
			return parsed;
		}
	}

	for (const block of codeBlocks) {
		if (!['markdown', 'md', 'skill', 'text', ''].includes(block.lang)) {
			continue;
		}

		const parsed = parseMarkdownDraft(block.code);
		if (parsed) {
			return parsed;
		}
	}

	return parseMarkdownDraft(cleaned);
};
