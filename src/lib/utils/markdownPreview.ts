import { marked } from 'marked';
import DOMPurify from 'dompurify';

export const MARKDOWN_PREVIEW_EXTS = new Set(['md', 'markdown', 'mdx']);

export const getMarkdownPreviewExtension = (value: string | null | undefined): string => {
	const normalized = value?.split(/[?#]/, 1)[0] ?? '';
	return normalized.split('.').pop()?.toLowerCase() ?? '';
};

export const isMarkdownPreviewPath = (value: string | null | undefined): boolean => {
	return MARKDOWN_PREVIEW_EXTS.has(getMarkdownPreviewExtension(value));
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

export const renderMarkdownPreviewHtml = (value: string | null | undefined): string => {
	return value ? DOMPurify.sanitize(marked.parse(value, { async: false }) as string) : '';
};
