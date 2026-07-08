import { marked } from 'marked';
import DOMPurify from 'dompurify';

export const MARKDOWN_PREVIEW_EXTS = new Set(['md', 'markdown', 'mdx']);
export const MERMAID_PREVIEW_EXTS = new Set(['mermaid', 'mmd']);

export const getMarkdownPreviewExtension = (value: string | null | undefined): string => {
	const normalized = value?.split(/[?#]/, 1)[0] ?? '';
	return normalized.split('.').pop()?.toLowerCase() ?? '';
};

export const isMarkdownPreviewPath = (value: string | null | undefined): boolean => {
	return MARKDOWN_PREVIEW_EXTS.has(getMarkdownPreviewExtension(value));
};

export const isMermaidPreviewPath = (value: string | null | undefined): boolean => {
	return MERMAID_PREVIEW_EXTS.has(getMarkdownPreviewExtension(value));
};

export const isMarkdownPreviewContentType = (value: string | null | undefined): boolean => {
	const normalized = (value ?? '').toLowerCase();

	return (
		normalized === 'text/markdown' ||
		normalized === 'text/x-markdown' ||
		normalized === 'application/markdown' ||
		normalized === 'application/x-markdown' ||
		normalized === 'application/mdx' ||
		normalized.includes('markdown')
	);
};

export const isMermaidPreviewContentType = (value: string | null | undefined): boolean => {
	const normalized = (value ?? '').toLowerCase();

	return normalized.includes('mermaid');
};

export const prepareMarkdownPreviewSource = (
	value: string | null | undefined,
	options: { mermaid?: boolean } = {}
): string => {
	const text = value ?? '';
	if (!text) return '';
	if (!options.mermaid) return text;
	if (/```(?:\s*mermaid)\b/i.test(text)) return text;
	return `\`\`\`mermaid\n${text.trim()}\n\`\`\``;
};

export const renderMarkdownPreviewHtml = (value: string | null | undefined): string => {
	return value ? DOMPurify.sanitize(marked.parse(value, { async: false }) as string) : '';
};
