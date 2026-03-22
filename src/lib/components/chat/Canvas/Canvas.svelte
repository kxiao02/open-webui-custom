<script lang="ts">
	import { createEventDispatcher, getContext } from 'svelte';
	import { canvasState, chatId, settings, showCanvas, showControls } from '$lib/stores';
	import { copyToClipboard } from '$lib/utils';

	import CanvasCodeEditor from './CanvasCodeEditor.svelte';
	import CanvasPreview from './CanvasPreview.svelte';
	import CanvasDiff from './CanvasDiff.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import ArrowsPointingOut from '$lib/components/icons/ArrowsPointingOut.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import SvgPanZoom from '$lib/components/common/SVGPanZoom.svelte';

	const i18n = getContext('i18n');
	const dispatch = createEventDispatcher();

	export let overlay = false;

	let activeTab: 'code' | 'preview' | 'diff' = 'code';
	let copied = false;
	let iframeElement: HTMLIFrameElement;

	$: mode = $canvasState?.mode ?? 'edit';
	$: lang = $canvasState?.lang ?? '';
	$: title = $canvasState?.title || lang || $i18n.t('Code');
	$: contents = $canvasState?.contents ?? [];
	$: selectedContentIdx = $canvasState?.selectedContentIdx ?? 0;

	// In preview mode, default to preview tab
	$: if (mode === 'preview' && activeTab === 'code') {
		activeTab = 'preview';
	}
	// In edit mode, default to code tab
	$: if (mode === 'edit' && activeTab !== 'code' && activeTab !== 'preview' && activeTab !== 'diff') {
		activeTab = 'code';
	}

	const close = () => {
		dispatch('close');
		showCanvas.set(false);
		showControls.set(false);
	};

	const copyCode = async () => {
		let textToCopy = '';
		if (mode === 'preview' && contents.length > 0) {
			textToCopy = contents[selectedContentIdx]?.content ?? '';
		} else {
			textToCopy = $canvasState?.code ?? '';
		}
		if (textToCopy) {
			await copyToClipboard(textToCopy);
			copied = true;
			setTimeout(() => {
				copied = false;
			}, 2000);
		}
	};

	function navigateContent(direction: 'prev' | 'next') {
		if (!$canvasState) return;
		const newIdx =
			direction === 'prev'
				? Math.max(selectedContentIdx - 1, 0)
				: Math.min(selectedContentIdx + 1, contents.length - 1);
		$canvasState = { ...$canvasState, selectedContentIdx: newIdx };
	}

	const downloadArtifact = () => {
		if (contents.length === 0) return;
		const item = contents[selectedContentIdx];
		const isSvg = item.type === 'svg';
		const mimeType = isSvg ? 'image/svg+xml' : 'text/html';
		const ext = isSvg ? 'svg' : 'html';
		const blob = new Blob([item.content], { type: mimeType });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = `artifact-${$chatId}-${selectedContentIdx}.${ext}`;
		document.body.appendChild(a);
		a.click();
		document.body.removeChild(a);
		URL.revokeObjectURL(url);
	};

	const showFullScreen = () => {
		if (iframeElement?.requestFullscreen) {
			iframeElement.requestFullscreen();
		} else if ((iframeElement as any)?.webkitRequestFullscreen) {
			(iframeElement as any).webkitRequestFullscreen();
		}
	};

	const iframeLoadHandler = () => {
		if (!iframeElement?.contentWindow) return;
		const cw = iframeElement.contentWindow;
		cw.addEventListener(
			'click',
			function (e) {
				const target = (e.target as HTMLElement).closest('a');
				if (target && (target as HTMLAnchorElement).href) {
					e.preventDefault();
					const url = new URL((target as HTMLAnchorElement).href, iframeElement.baseURI);
					if (url.origin === window.location.origin) {
						cw.history.pushState(null, '', url.pathname + url.search + url.hash);
					}
				}
			},
			true
		);
		cw.addEventListener('dragstart', (e) => e.preventDefault(), true);
	};
</script>

<div class="w-full h-full relative flex flex-col bg-white dark:bg-gray-850" id="canvas-container">
	<!-- Header -->
	<div
		class="flex items-center justify-between px-3 py-2 border-b border-gray-100 dark:border-gray-800 shrink-0"
	>
		<div class="flex items-center gap-2 min-w-0">
			{#if mode === 'edit'}
				<!-- Edit mode: Code/Preview/Diff tabs -->
				<div class="flex items-center gap-1 bg-gray-100 dark:bg-gray-800 rounded-lg p-0.5">
					<button
						class="px-2.5 py-1 text-xs rounded-md transition {activeTab === 'code'
							? 'bg-white dark:bg-gray-700 font-medium text-gray-900 dark:text-white shadow-sm'
							: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
						on:click={() => (activeTab = 'code')}
					>
						{$i18n.t('Code')}
					</button>
					<button
						class="px-2.5 py-1 text-xs rounded-md transition {activeTab === 'preview'
							? 'bg-white dark:bg-gray-700 font-medium text-gray-900 dark:text-white shadow-sm'
							: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
						on:click={() => (activeTab = 'preview')}
					>
						{$i18n.t('Preview')}
					</button>
					<button
						class="px-2.5 py-1 text-xs rounded-md transition {activeTab === 'diff'
							? 'bg-white dark:bg-gray-700 font-medium text-gray-900 dark:text-white shadow-sm'
							: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
						on:click={() => (activeTab = 'diff')}
					>
						{$i18n.t('Diff')}
					</button>
				</div>
				<span class="text-xs text-gray-500 dark:text-gray-400 truncate">{title}</span>
			{:else}
				<!-- Preview mode: version navigation -->
				<div class="flex items-center gap-0.5 self-center min-w-fit" dir="ltr">
					<button
						class="self-center p-1 hover:bg-black/5 dark:hover:bg-white/5 dark:hover:text-white hover:text-black rounded-md transition disabled:cursor-not-allowed"
						on:click={() => navigateContent('prev')}
						disabled={contents.length <= 1}
					>
						<svg
							xmlns="http://www.w3.org/2000/svg"
							fill="none"
							viewBox="0 0 24 24"
							stroke="currentColor"
							stroke-width="2.5"
							class="size-3.5"
						>
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								d="M15.75 19.5 8.25 12l7.5-7.5"
							/>
						</svg>
					</button>

					<div class="text-xs self-center dark:text-gray-100 min-w-fit">
						{$i18n.t('Version {{selectedVersion}} of {{totalVersions}}', {
							selectedVersion: selectedContentIdx + 1,
							totalVersions: contents.length
						})}
					</div>

					<button
						class="self-center p-1 hover:bg-black/5 dark:hover:bg-white/5 dark:hover:text-white hover:text-black rounded-md transition disabled:cursor-not-allowed"
						on:click={() => navigateContent('next')}
						disabled={contents.length <= 1}
					>
						<svg
							xmlns="http://www.w3.org/2000/svg"
							fill="none"
							viewBox="0 0 24 24"
							stroke="currentColor"
							stroke-width="2.5"
							class="size-3.5"
						>
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								d="m8.25 4.5 7.5 7.5-7.5 7.5"
							/>
						</svg>
					</button>
				</div>
			{/if}
		</div>

		<div class="flex items-center gap-1 shrink-0">
			<button
				class="text-xs px-1.5 py-0.5 rounded-md bg-gray-50 hover:bg-gray-100 dark:bg-gray-800 dark:hover:bg-gray-700 transition"
				on:click={copyCode}
			>
				{copied ? $i18n.t('Copied') : $i18n.t('Copy')}
			</button>

			{#if mode === 'preview'}
				<Tooltip content={$i18n.t('Download')}>
					<button
						class="text-xs p-0.5 rounded-md bg-gray-50 hover:bg-gray-100 dark:bg-gray-800 dark:hover:bg-gray-700 transition"
						on:click={downloadArtifact}
					>
						<Download className="size-3.5" />
					</button>
				</Tooltip>

				{#if contents[selectedContentIdx]?.type === 'iframe'}
					<Tooltip content={$i18n.t('Open in full screen')}>
						<button
							class="text-xs p-0.5 rounded-md bg-gray-50 hover:bg-gray-100 dark:bg-gray-800 dark:hover:bg-gray-700 transition"
							on:click={showFullScreen}
						>
							<ArrowsPointingOut className="size-3.5" />
						</button>
					</Tooltip>
				{/if}
			{/if}

			<button
				class="p-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition text-gray-500 dark:text-gray-400"
				on:click={close}
				aria-label={$i18n.t('Close')}
			>
				<XMark className="size-3.5" />
			</button>
		</div>
	</div>

	<!-- Overlay for drag state -->
	{#if overlay}
		<div class="absolute top-0 left-0 right-0 bottom-0 z-10"></div>
	{/if}

	<!-- Content -->
	<div class="flex-1 min-h-0 overflow-hidden">
		{#if mode === 'edit'}
			{#if activeTab === 'code'}
				<CanvasCodeEditor />
			{:else if activeTab === 'preview'}
				<CanvasPreview />
			{:else if activeTab === 'diff'}
				<CanvasDiff />
			{/if}
		{:else if mode === 'preview'}
			<!-- Artifact preview mode -->
			{#if contents.length > 0}
				{#if contents[selectedContentIdx]?.type === 'iframe'}
					<iframe
						bind:this={iframeElement}
						title="Content"
						srcdoc={contents[selectedContentIdx].content}
						class="w-full border-0 h-full rounded-none"
						sandbox="allow-scripts allow-downloads{($settings?.iframeSandboxAllowForms ?? false)
							? ' allow-forms'
							: ''}{($settings?.iframeSandboxAllowSameOrigin ?? false)
							? ' allow-same-origin'
							: ''}"
						on:load={iframeLoadHandler}
					></iframe>
				{:else if contents[selectedContentIdx]?.type === 'svg'}
					<SvgPanZoom
						className="w-full h-full max-h-full overflow-hidden"
						svg={contents[selectedContentIdx].content}
					/>
				{/if}
			{:else}
				<div class="m-auto font-medium text-xs text-gray-900 dark:text-white flex items-center justify-center h-full">
					{$i18n.t('No HTML, CSS, or JavaScript content found.')}
				</div>
			{/if}
		{/if}
	</div>
</div>
