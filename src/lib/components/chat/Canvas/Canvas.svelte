<script lang="ts">
	import { createEventDispatcher, getContext } from 'svelte';
	import { canvasState, showCanvas, showControls } from '$lib/stores';
	import { copyToClipboard } from '$lib/utils';

	import CanvasCodeEditor from './CanvasCodeEditor.svelte';
	import CanvasPreview from './CanvasPreview.svelte';
	import CanvasDiff from './CanvasDiff.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n = getContext('i18n');
	const dispatch = createEventDispatcher();

	export let overlay = false;

	let activeTab: 'code' | 'preview' | 'diff' = 'code';
	let copied = false;

	$: lang = $canvasState?.lang ?? '';
	$: title = $canvasState?.title || lang || $i18n.t('Code');

	const close = () => {
		dispatch('close');
		showCanvas.set(false);
		showControls.set(false);
	};

	const copyCode = () => {
		if ($canvasState?.code) {
			copyToClipboard($canvasState.code);
			copied = true;
			setTimeout(() => {
				copied = false;
			}, 2000);
		}
	};
</script>

<div class="w-full h-full relative flex flex-col bg-white dark:bg-gray-850" id="canvas-container">
	<!-- Header -->
	<div
		class="flex items-center justify-between px-3 py-2 border-b border-gray-100 dark:border-gray-800 shrink-0"
	>
		<div class="flex items-center gap-2 min-w-0">
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
		</div>

		<div class="flex items-center gap-1 shrink-0">
			<button
				class="text-xs px-1.5 py-0.5 rounded-md bg-gray-50 hover:bg-gray-100 dark:bg-gray-800 dark:hover:bg-gray-700 transition"
				on:click={copyCode}
			>
				{copied ? $i18n.t('Copied') : $i18n.t('Copy')}
			</button>

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
		{#if activeTab === 'code'}
			<CanvasCodeEditor />
		{:else if activeTab === 'preview'}
			<CanvasPreview />
		{:else if activeTab === 'diff'}
			<CanvasDiff />
		{/if}
	</div>
</div>
