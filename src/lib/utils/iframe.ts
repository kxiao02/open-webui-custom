import { normalizeHtmlMediaUrls, normalizeMediaUrl } from '$lib/utils/knowflowAssets';

const KNOWN_THEME_CLASSES = ['light', 'dark', 'her'] as const;
const FILE_CONTENT_URL_REGEX =
	/^(?:https?:\/\/[^/]+)?\/?(?:api\/v1|openai\/v1|v1)\/files\/[^/?#]+\/content(?:\/html)?(?:[?#].*)?$/i;
const SYNCED_ROOT_CSS_VARIABLES = [
	'--app-text-scale',
	'--color-gray-50',
	'--color-gray-100',
	'--color-gray-200',
	'--color-gray-300',
	'--color-gray-400',
	'--color-gray-500',
	'--color-gray-600',
	'--color-gray-700',
	'--color-gray-800',
	'--color-gray-850',
	'--color-gray-900',
	'--color-gray-950'
] as const;

export const IFRAME_THEME_MESSAGE_TYPE = 'open-webui:theme';
const IFRAME_HEIGHT_PADDING_PX = 20;

export type IframeThemeSnapshot = {
	theme: string;
	classes: string[];
	cssVariables: Record<string, string>;
};

type IframeDocumentOptions = {
	html: string;
	baseHref?: string | null;
	theme: IframeThemeSnapshot;
	headMarkup?: string;
};

const escapeHtmlAttribute = (value: string) => {
	return value
		.replaceAll('&', '&amp;')
		.replaceAll('"', '&quot;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;');
};

const injectIntoHead = (html: string, additions: string) => {
	if (/<\/head>/i.test(html)) {
		return html.replace(/<\/head>/i, `${additions}\n</head>`);
	}

	if (/<html(?:\s[^>]*)?>/i.test(html)) {
		return html.replace(/<html(?:\s[^>]*)?>/i, (match) => `${match}<head>${additions}</head>`);
	}

	if (/<body(?:\s[^>]*)?>/i.test(html)) {
		return html.replace(/<body(?:\s[^>]*)?>/i, (match) => `<head>${additions}</head>${match}`);
	}

	return `<!doctype html><html><head>${additions}</head><body>${html}</body></html>`;
};

const THEME_BRIDGE_STYLES = `
<style id="open-webui-iframe-theme">
	@font-face {
		font-family: 'Inter';
		src: url('/assets/fonts/Inter-Variable.ttf');
		font-display: swap;
	}

	@font-face {
		font-family: 'Archivo';
		src: url('/assets/fonts/Archivo-Variable.ttf');
		font-display: swap;
	}

	@font-face {
		font-family: 'InstrumentSerif';
		src: url('/assets/fonts/InstrumentSerif-Regular.ttf');
		font-display: swap;
	}

	@font-face {
		font-family: 'Vazirmatn';
		src: url('/assets/fonts/Vazirmatn-Variable.ttf');
		font-display: swap;
	}

	:root {
		--owui-font-body: -apple-system, BlinkMacSystemFont, 'Inter', 'Vazirmatn', ui-sans-serif,
			system-ui, 'Segoe UI', Roboto, Ubuntu, Cantarell, 'Noto Sans', sans-serif,
			'Helvetica Neue', Arial, 'Apple Color Emoji', 'Segoe UI Emoji', 'Segoe UI Symbol',
			'Noto Color Emoji';
		--owui-font-primary: 'Archivo', 'Vazirmatn', sans-serif;
		--owui-font-secondary: 'InstrumentSerif', sans-serif;
		--owui-frame-background: #ffffff;
		--owui-frame-foreground: #111827;
	}

	html,
	body {
		height: auto;
	}

	body {
		margin: 0;
		font-family: var(--owui-font-body);
		font-size: calc(1rem * var(--app-text-scale, 1));
		background-color: var(--owui-frame-background);
		color: var(--owui-frame-foreground);
	}

	button,
	input,
	select,
	textarea {
		font: inherit;
	}

	img {
		display: block;
		max-width: 100%;
		height: auto;
	}

	table {
		width: 100%;
		border-collapse: collapse;
		border-spacing: 0;
	}

	caption {
		margin-bottom: 0.5rem;
		text-align: left;
		font-weight: 600;
	}

	th,
	td {
		border: 1px solid var(--color-gray-200, #e5e7eb);
		padding: 0.5rem 0.75rem;
		vertical-align: top;
		text-align: left;
	}

	th {
		background: var(--color-gray-50, #f9fafb);
	}

	html.light,
	html.her,
	html[data-open-webui-theme='light'],
	html[data-open-webui-theme='her'] {
		color-scheme: light;
		--owui-frame-background: #ffffff;
		--owui-frame-foreground: #111827;
	}

	html.dark,
	html[data-open-webui-theme='dark'] {
		color-scheme: dark;
		--owui-frame-background: var(--color-gray-900, #171717);
		--owui-frame-foreground: #f9fafb;
	}

	html.dark th,
	html[data-open-webui-theme='dark'] th {
		background: var(--color-gray-850, #202020);
	}

	.font-primary {
		font-family: var(--owui-font-primary);
	}

	.font-secondary {
		font-family: var(--owui-font-secondary);
	}
</style>`;

const createThemeBridgeScript = (theme: IframeThemeSnapshot) => {
	const initialTheme = JSON.stringify(theme);

	return `
<script id="open-webui-iframe-bridge">
	(() => {
		const THEME_MESSAGE_TYPE = '${IFRAME_THEME_MESSAGE_TYPE}'
		const HEIGHT_PADDING_PX = ${IFRAME_HEIGHT_PADDING_PX}
		const KNOWN_THEME_CLASSES = ${JSON.stringify(KNOWN_THEME_CLASSES)}
		const initialTheme = ${initialTheme}
		const root = document.documentElement
		let resizeObserver = null
		let mutationObserver = null
		let queuedHeightFrame = null

		const measureDocumentHeight = () => {
			const body = document.body
			if (!body) {
				return 0
			}

			const bodyRect = body.getBoundingClientRect()
			let rangeHeight = 0
			try {
				const range = document.createRange()
				range.selectNodeContents(body)
				rangeHeight = Math.ceil(range.getBoundingClientRect().height)
			} catch {}

			const childBottom = Array.from(body.children).reduce((maxHeight, child) => {
				return Math.max(maxHeight, Math.ceil(child.getBoundingClientRect().bottom - bodyRect.top))
			}, 0)

			if (rangeHeight > 0 || childBottom > 0) {
				return Math.max(rangeHeight, childBottom)
			}

			const docEl = document.documentElement
			return Math.max(
				docEl?.scrollHeight ?? 0,
				body.scrollHeight ?? 0,
				docEl?.offsetHeight ?? 0,
				body.offsetHeight ?? 0
			)
		}

		const applyTheme = (theme) => {
			if (!theme || !root) {
				return
			}

			KNOWN_THEME_CLASSES.forEach((className) => root.classList.remove(className))

			const nextClasses = Array.isArray(theme.classes) && theme.classes.length > 0
				? theme.classes
				: theme.theme
					? [theme.theme]
					: []

			nextClasses.forEach((className) => {
				if (typeof className === 'string' && className) {
					root.classList.add(className)
				}
			})

			if (theme.theme) {
				root.dataset.openWebuiTheme = theme.theme
				root.style.colorScheme = theme.theme === 'dark' ? 'dark' : 'light'
			}

			if (theme.cssVariables && typeof theme.cssVariables === 'object') {
				Object.entries(theme.cssVariables).forEach(([name, value]) => {
					if (typeof name === 'string' && typeof value === 'string' && value) {
						root.style.setProperty(name, value)
					}
				})
			}
		}

		const reportHeight = () => {
			if (queuedHeightFrame !== null) {
				cancelAnimationFrame(queuedHeightFrame)
			}

			queuedHeightFrame = requestAnimationFrame(() => {
				queuedHeightFrame = null
				const height = measureDocumentHeight()

				window.parent?.postMessage(
					{ type: 'iframe:height', height: height + HEIGHT_PADDING_PX },
					'*'
				)
			})
		}

		window.addEventListener('message', (event) => {
			if (event?.data?.type === THEME_MESSAGE_TYPE) {
				applyTheme(event.data.theme)
				reportHeight()
			}
		})

		window.addEventListener('load', reportHeight)
		window.addEventListener('resize', reportHeight)
		document.addEventListener('DOMContentLoaded', () => {
			applyTheme(initialTheme)
			reportHeight()
		})

		applyTheme(initialTheme)

		if ('ResizeObserver' in window) {
			resizeObserver = new ResizeObserver(reportHeight)
			resizeObserver.observe(document.documentElement)
			if (document.body) {
				resizeObserver.observe(document.body)
			}
		}

		if ('MutationObserver' in window) {
			mutationObserver = new MutationObserver(reportHeight)
			mutationObserver.observe(document.documentElement, {
				subtree: true,
				childList: true,
				attributes: true,
				characterData: true
			})
		}

		requestAnimationFrame(reportHeight)
	})()
</script>`;
};

export const isIframeMarkup = (value: string) => {
	return /^\s*</.test(value);
};

export const resolveIframeUrl = (value: string) => {
	if (typeof window === 'undefined') {
		return null;
	}

	try {
		return new URL(normalizeMediaUrl(value), window.location.href).toString();
	} catch {
		return null;
	}
};

export const isHtmlLikeResponse = (url: string, contentType = '') => {
	const normalizedType = contentType.toLowerCase();
	if (normalizedType.includes('text/html') || normalizedType.includes('application/xhtml+xml')) {
		return true;
	}

	return /(?:^data:text\/html\b)|(?:\/content\/html(?:[?#].*)?$)|(?:\.html?(?:[?#].*)?$)/i.test(
		url
	);
};

export const shouldFetchIframeUrl = (value: string) => {
	if (typeof window === 'undefined') {
		return false;
	}

	try {
		const url = new URL(value);
		if (url.protocol === 'blob:' || url.protocol === 'data:') {
			return true;
		}

		if (url.origin !== window.location.origin) {
			return false;
		}

		return FILE_CONTENT_URL_REGEX.test(url.toString()) || /\.html?(?:[?#].*)?$/i.test(url.pathname);
	} catch {
		return false;
	}
};

export const buildIframeRequestHeaders = (): Record<string, string> => {
	const token = typeof localStorage !== 'undefined' ? localStorage.getItem('token') : null;
	return token ? { Authorization: `Bearer ${token}` } : {};
};

export const captureIframeThemeSnapshot = (): IframeThemeSnapshot => {
	if (typeof window === 'undefined' || typeof document === 'undefined') {
		return {
			theme: 'light',
			classes: ['light'],
			cssVariables: {}
		};
	}

	const root = document.documentElement;
	const computedStyle = getComputedStyle(root);
	const classes = KNOWN_THEME_CLASSES.filter((className) => root.classList.contains(className));
	const cssVariables = Object.fromEntries(
		SYNCED_ROOT_CSS_VARIABLES.map((name) => [
			name,
			computedStyle.getPropertyValue(name).trim()
		]).filter(([, value]) => Boolean(value))
	);

	const theme =
		classes[0] ?? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');

	return {
		theme,
		classes: classes.length > 0 ? classes : [theme],
		cssVariables
	};
};

export const measureIframeDocumentHeight = (doc: Document | null | undefined): number => {
	if (!doc?.body) {
		return 0;
	}

	const body = doc.body;
	const bodyRect = body.getBoundingClientRect();

	let rangeHeight = 0;
	try {
		const range = doc.createRange();
		range.selectNodeContents(body);
		rangeHeight = Math.ceil(range.getBoundingClientRect().height);
	} catch {
		rangeHeight = 0;
	}

	const childBottom = Array.from(body.children).reduce((maxHeight, child) => {
		return Math.max(maxHeight, Math.ceil(child.getBoundingClientRect().bottom - bodyRect.top));
	}, 0);

	if (rangeHeight > 0 || childBottom > 0) {
		return Math.max(rangeHeight, childBottom);
	}

	const docEl = doc.documentElement;
	return Math.max(
		docEl?.scrollHeight ?? 0,
		body.scrollHeight ?? 0,
		docEl?.offsetHeight ?? 0,
		body.offsetHeight ?? 0
	);
};

export const getIframeHeightWithPadding = (height: number): number =>
	Math.max(0, Math.ceil(height)) + IFRAME_HEIGHT_PADDING_PX;

export const buildThemedIframeDocument = ({
	html,
	baseHref = null,
	theme,
	headMarkup = ''
}: IframeDocumentOptions) => {
	const baseTag = baseHref ? `<base href="${escapeHtmlAttribute(baseHref)}">` : '';
	const additions = [baseTag, headMarkup, THEME_BRIDGE_STYLES, createThemeBridgeScript(theme)]
		.filter(Boolean)
		.join('\n');

	return injectIntoHead(normalizeHtmlMediaUrls(html), additions);
};
