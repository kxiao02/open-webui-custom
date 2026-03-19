<script lang="ts">
	import { onMount, getContext } from 'svelte';
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

	$: renderedMarkdown = isMarkdown ? marked.parse(code) : '';

	$: highlightedCode =
		!isPreviewable && code
			? hljs.highlightAuto(code, hljs.getLanguage(lang)?.aliases).value || code
			: '';

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
			srcdoc={code}
			class="w-full h-full border-0"
			sandbox="allow-scripts allow-downloads{($settings?.iframeSandboxAllowForms ?? false)
				? ' allow-forms'
				: ''}{($settings?.iframeSandboxAllowSameOrigin ?? false) ? ' allow-same-origin' : ''}"
			on:load={iframeLoadHandler}
		></iframe>
	{:else if isSvg}
		<SvgPanZoom className="w-full h-full max-h-full overflow-hidden" svg={code} />
	{:else if isMarkdown}
		<div class="prose dark:prose-invert max-w-none p-4 text-sm">
			{@html renderedMarkdown}
		</div>
	{:else if code}
		<pre
			class="hljs p-4 overflow-x-auto h-full text-sm"><code class="language-{lang}">{@html highlightedCode}</code></pre>
	{:else}
		<div class="flex items-center justify-center h-full text-gray-400 text-sm">
			{$i18n.t('No content to preview')}
		</div>
	{/if}
</div>
