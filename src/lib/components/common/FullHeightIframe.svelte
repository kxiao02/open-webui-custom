<script lang="ts">
	import { browser } from '$app/environment';
	import { onDestroy, onMount, tick } from 'svelte';

	import {
		IFRAME_THEME_MESSAGE_TYPE,
		buildIframeRequestHeaders,
		buildThemedIframeDocument,
		captureIframeThemeSnapshot,
		getIframeHeightWithPadding,
		isHtmlLikeResponse,
		isIframeMarkup,
		measureIframeDocumentHeight,
		resolveIframeUrl,
		shouldFetchIframeUrl
	} from '$lib/utils/iframe';

	export let src: string | null = null;
	export let title = 'Embedded Content';
	export let initialHeight: number | null = null;
	export let iframeClassName = 'w-full rounded-2xl';
	export let args: unknown = null;

	export let allowScripts = true;
	export let allowForms = false;
	export let allowSameOrigin = false;
	export let allowPopups = false;
	export let allowDownloads = true;
	export let useSandbox = true;

	export let referrerPolicy: HTMLIFrameElement['referrerPolicy'] =
		'strict-origin-when-cross-origin';
	export let allowFullscreen = true;
	export let payload: unknown = null;

	let iframe: HTMLIFrameElement | null = null;
	let iframeSrc: string | null = null;
	let iframeDoc: string | null = null;
	let loadRequestId = 0;
	let injectedBlobUrls: string[] = [];
	let themeObserver: MutationObserver | null = null;

	$: sandbox = useSandbox
		? [
				allowScripts && 'allow-scripts',
				allowForms && 'allow-forms',
				allowSameOrigin && 'allow-same-origin',
				allowPopups && 'allow-popups',
				allowDownloads && 'allow-downloads'
			]
				.filter(Boolean)
				.join(' ') || undefined
		: undefined;

	$: if (browser && src) {
		void refreshIframeSource();
	} else {
		loadRequestId += 1;
		iframeSrc = null;
		iframeDoc = null;
		releaseInjectedBlobUrls();
	}

	const alpineDirectives = [
		'x-data',
		'x-init',
		'x-show',
		'x-bind',
		'x-on',
		'x-text',
		'x-html',
		'x-model',
		'x-modelable',
		'x-ref',
		'x-for',
		'x-if',
		'x-effect',
		'x-transition',
		'x-cloak',
		'x-ignore',
		'x-teleport',
		'x-id'
	];

	const releaseInjectedBlobUrls = () => {
		injectedBlobUrls.forEach((blobUrl) => URL.revokeObjectURL(blobUrl));
		injectedBlobUrls = [];
	};

	const buildDependencyMarkup = async (html: string) => {
		if (!allowSameOrigin) {
			return '';
		}

		const scriptTags: string[] = [];
		const hasAlpineDirectives = alpineDirectives.some((directive) => html.includes(directive));
		if (hasAlpineDirectives) {
			try {
				const { default: alpineCode } = await import('alpinejs/dist/cdn.min.js?raw');
				const alpineBlob = new Blob([alpineCode], { type: 'text/javascript' });
				const alpineUrl = URL.createObjectURL(alpineBlob);
				injectedBlobUrls = [...injectedBlobUrls, alpineUrl];
				scriptTags.push(`<script src="${alpineUrl}" defer><\/script>`);
			} catch (error) {
				console.error('Error processing Alpine for iframe:', error);
			}
		}

		const chartJsDirectives = ['new Chart(', 'Chart.'];
		const hasChartJsDirectives = chartJsDirectives.some((directive) => html.includes(directive));
		if (hasChartJsDirectives) {
			try {
				const { default: Chart } = await import('chart.js/auto');
				(window as Window & { Chart?: unknown }).Chart = Chart;
				scriptTags.push(`<script>
window.Chart = parent.Chart
<\/script>`);
			} catch (error) {
				console.error('Error processing Chart.js for iframe:', error);
			}
		}

		return scriptTags.join('\n');
	};

	const loadUrlAsHtml = async (resolvedUrl: string) => {
		if (!shouldFetchIframeUrl(resolvedUrl)) {
			return null;
		}

		try {
			const requestHeaders = buildIframeRequestHeaders();
			const response = await fetch(resolvedUrl, {
				credentials: 'include',
				headers: Object.keys(requestHeaders).length > 0 ? requestHeaders : undefined
			});
			if (!response.ok) {
				return null;
			}

			const contentType = response.headers.get('content-type') ?? '';
			if (!isHtmlLikeResponse(resolvedUrl, contentType)) {
				return null;
			}

			return await response.text();
		} catch (error) {
			console.error('Error loading iframe HTML source:', error);
			return null;
		}
	};

	const buildIframeDocument = async (html: string, baseHref: string | null = null) => {
		const headMarkup = await buildDependencyMarkup(html);
		return buildThemedIframeDocument({
			html,
			baseHref,
			theme: captureIframeThemeSnapshot(),
			headMarkup
		});
	};

	const refreshIframeSource = async () => {
		const requestId = ++loadRequestId;
		releaseInjectedBlobUrls();
		await tick();

		const nextSrc = src?.trim() ?? '';
		if (!nextSrc) {
			if (requestId === loadRequestId) {
				iframeSrc = null;
				iframeDoc = null;
			}
			return;
		}

		if (isIframeMarkup(nextSrc)) {
			const nextDoc = await buildIframeDocument(nextSrc);
			if (requestId !== loadRequestId) {
				return;
			}
			iframeDoc = nextDoc;
			iframeSrc = null;
			return;
		}

		const resolvedUrl = resolveIframeUrl(nextSrc);
		if (!resolvedUrl) {
			const nextDoc = await buildIframeDocument(nextSrc);
			if (requestId !== loadRequestId) {
				return;
			}
			iframeDoc = nextDoc;
			iframeSrc = null;
			return;
		}

		const urlHtml = await loadUrlAsHtml(resolvedUrl);
		if (requestId !== loadRequestId) {
			return;
		}

		if (urlHtml !== null) {
			const nextDoc = await buildIframeDocument(urlHtml, resolvedUrl);
			if (requestId !== loadRequestId) {
				releaseInjectedBlobUrls();
				return;
			}
			iframeDoc = nextDoc;
			iframeSrc = null;
			return;
		}

		iframeSrc = resolvedUrl;
		iframeDoc = null;
	};

	const postThemeToIframe = () => {
		if (!iframe?.contentWindow) {
			return;
		}

		iframe.contentWindow.postMessage(
			{
				type: IFRAME_THEME_MESSAGE_TYPE,
				theme: captureIframeThemeSnapshot()
			},
			'*'
		);
	};

	function resizeSameOrigin() {
		if (!iframe) return;
		try {
			const doc = iframe.contentDocument || iframe.contentWindow?.document;
			if (!doc) return;
			const height = measureIframeDocumentHeight(doc);
			if (height > 0) {
				const nextHeight = `${getIframeHeightWithPadding(height)}px`;
				if (iframe.style.height !== nextHeight) {
					iframe.style.height = nextHeight;
				}
			}
		} catch {
			// Cross-origin documents report their own height via postMessage.
		}
	}

	function onMessage(event: MessageEvent) {
		if (!iframe || event.source !== iframe.contentWindow) return;

		const data = event.data || {};
		if (data?.type === 'iframe:height' && typeof data.height === 'number') {
			const nextHeight = `${Math.max(0, Math.ceil(data.height))}px`;
			if (iframe.style.height !== nextHeight) {
				iframe.style.height = nextHeight;
			}
		}

		if (data?.type === 'pong') {
			iframe.contentWindow?.postMessage({ type: 'pong:ack' }, '*');
		}

		if (data?.type === 'payload') {
			iframe.contentWindow?.postMessage(
				{ type: 'payload', requestId: data?.requestId ?? null, payload },
				'*'
			);
		}
	}

	const onLoad = () => {
		requestAnimationFrame(() => {
			resizeSameOrigin();
			postThemeToIframe();
		});

		if (args && iframe?.contentWindow) {
			(iframe.contentWindow as Window & { args?: unknown }).args = args;
		}
	};

	onMount(() => {
		window.addEventListener('message', onMessage);
		themeObserver = new MutationObserver(() => {
			postThemeToIframe();
			requestAnimationFrame(resizeSameOrigin);
		});
		themeObserver.observe(document.documentElement, {
			attributes: true,
			attributeFilter: ['class', 'style']
		});
	});

	onDestroy(() => {
		themeObserver?.disconnect();
		window.removeEventListener('message', onMessage);
		releaseInjectedBlobUrls();
	});
</script>

{#if iframeDoc}
	<iframe
		bind:this={iframe}
		srcdoc={iframeDoc}
		{title}
		class={iframeClassName}
		style={initialHeight ? `height:${initialHeight}px;` : undefined}
		width="100%"
		frameborder="0"
		{sandbox}
		allowfullscreen={allowFullscreen}
		on:load={onLoad}
	></iframe>
{:else if iframeSrc}
	<iframe
		bind:this={iframe}
		src={iframeSrc}
		{title}
		class={iframeClassName}
		style={initialHeight ? `height:${initialHeight}px;` : undefined}
		width="100%"
		frameborder="0"
		{sandbox}
		referrerpolicy={referrerPolicy}
		allowfullscreen={allowFullscreen}
		on:load={onLoad}
	></iframe>
{/if}
