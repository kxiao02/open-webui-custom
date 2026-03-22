<script lang="ts">
	import { getContext, onDestroy } from 'svelte';
	import { marked } from 'marked';
	import hljs from 'highlight.js';
	import { canvasState, settings } from '$lib/stores';
	import SvgPanZoom from '$lib/components/common/SVGPanZoom.svelte';

	const i18n = getContext('i18n');

	let iframeElement: HTMLIFrameElement;

	$: lang = $canvasState?.lang?.toLowerCase() ?? '';
	$: code = $canvasState?.code ?? '';

	$: isHtml = lang === 'html';
	$: isSvg = lang === 'svg';
	$: isMarkdown = ['markdown', 'md'].includes(lang);
	$: isPreviewable = isHtml || isSvg || isMarkdown;

	// Debounce preview rendering to avoid per-keystroke iframe rebuilds
	let previewCode = '';
	let previewTimer: ReturnType<typeof setTimeout>;

	$: {
		code;
		clearTimeout(previewTimer);
		previewTimer = setTimeout(() => {
			previewCode = code;
		}, 400);
	}

	onDestroy(() => clearTimeout(previewTimer));

	$: renderedMarkdown = isMarkdown ? marked.parse(previewCode) : '';

	$: highlightedCode = (() => {
		if (isPreviewable || !previewCode) return '';
		const langObj = hljs.getLanguage(lang);
		if (langObj) {
			return hljs.highlight(previewCode, { language: lang }).value;
		}
		return hljs.highlightAuto(previewCode).value || previewCode;
	})();

	const iframeLoadHandler = () => {
		if (!iframeElement?.contentWindow) return;
		iframeElement.contentWindow.addEventListener(
			'click',
			(e) => {
				const target = (e.target as HTMLElement).closest('a');
				if (target && (target as HTMLAnchorElement).href) {
					e.preventDefault();
				}
			},
			true
		);
	};
</script>

<div class="h-full w-full overflow-auto">
	{#if isHtml}
		<iframe
			bind:this={iframeElement}
			title="Preview"
			srcdoc={previewCode}
			class="w-full h-full border-0"
			sandbox="allow-scripts allow-downloads{($settings?.iframeSandboxAllowForms ?? false)
				? ' allow-forms'
				: ''}{($settings?.iframeSandboxAllowSameOrigin ?? false) ? ' allow-same-origin' : ''}"
			on:load={iframeLoadHandler}
		></iframe>
	{:else if isSvg}
		<SvgPanZoom className="w-full h-full max-h-full overflow-hidden" svg={previewCode} />
	{:else if isMarkdown}
		<div class="prose dark:prose-invert max-w-none p-4 text-sm">
			{@html renderedMarkdown}
		</div>
	{:else if previewCode}
		<pre
			class="hljs p-4 overflow-x-auto h-full text-sm"><code class="language-{lang}">{@html highlightedCode}</code></pre>
	{:else}
		<div class="flex items-center justify-center h-full text-gray-400 text-sm">
			{$i18n.t('No content to preview')}
		</div>
	{/if}
</div>
