<script context="module" lang="ts">
	let savedTab: 'controls' | 'files' | 'overview' = 'controls';
</script>

<script lang="ts">
	import { SvelteFlowProvider } from '@xyflow/svelte';
	import { slide } from 'svelte/transition';
	import { Pane, PaneResizer } from 'paneforge';
	import { v4 as uuidv4 } from 'uuid';

	import { onDestroy, onMount, tick, getContext } from 'svelte';
	import {
		config,
		terminalServers,
		mobile,
		showControls,
		showCallOverlay,
		showArtifacts,
		showEmbeds,
		showFilePreview,
		settings,
		showFileNavPath,
		selectedTerminalId,
		user
	} from '$lib/stores';

	import { uploadFile } from '$lib/apis/files';
	import { toast } from 'svelte-sonner';
	import { hasGeneratedFilesInHistory } from '$lib/utils/generated-files';

	import Controls from './Controls/Controls.svelte';
	import CallOverlay from './MessageInput/CallOverlay.svelte';
	import Drawer from '../common/Drawer.svelte';
	import Artifacts from './Artifacts.svelte';
	import Embeds from './ChatControls/Embeds.svelte';
	import FilePreview from './ChatControls/FilePreview.svelte';
	import FileNav from './FileNav.svelte';
	import PyodideFileNav from './PyodideFileNav.svelte';
	import Overview from './Overview.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	export let history;
	export let models = [];

	export let chatId = null;

	export let chatFiles = [];
	export let params = {};

	export let eventTarget: EventTarget;
	export let submitPrompt: Function;
	export let stopResponse: Function;
	export let showMessage: Function;
	export let files;
	export let modelId;

	export let codeInterpreterEnabled = false;

	export let pane: Pane | null = null;

	let largeScreen = false;
	let dragged = false;
	let minSize = 0;
	let paneReady = false;

	const COMPACT_CONTROLS_PANE_WIDTH_PX = 180;
	const COMPACT_CONTROLS_PANE_MAX_WIDTH_PX = 220;
	const PREVIEW_PANE_MIN_WIDTH_PX = 560;
	const PREVIEW_PANE_MAX_WIDTH_PX = 760;

	// Tab state for Controls+Files panel
	let activeTab: 'controls' | 'files' | 'overview' | 'preview' = savedTab;
	let wasFilePreviewOpen = false;
	let lastShowFileNavPath: string | null = null;
	let lastSelectedTerminalId: string | null = null;
	// svelte-ignore reactive_declaration_module_script_dependency
	$: {
		if (activeTab !== 'preview') {
			savedTab = activeTab;
		}
	}

	$: hasMessages = history?.messages && Object.keys(history.messages).length > 0;

	$: showControlsTab = $user?.role === 'admin' || ($user?.permissions?.chat?.controls ?? true);
	$: showFilesTab =
		!!$selectedTerminalId ||
		(codeInterpreterEnabled && $config?.code?.interpreter_engine !== 'jupyter');
	$: showOverviewTab = hasMessages;
	$: previewAvailable = hasGeneratedFilesInHistory(history);
	$: showPreviewTab = previewAvailable;
	$: isPreviewMode = $showFilePreview;

	// Tab fallback: if active tab becomes hidden, switch to next available
	$: if (!showOverviewTab && activeTab === 'overview') activeTab = 'controls';
	$: if (!showFilesTab && activeTab === 'files') activeTab = 'controls';
	$: if (!showPreviewTab && activeTab === 'preview') {
		if (showFilesTab) activeTab = 'files';
		else if (showControlsTab) activeTab = 'controls';
		else if (showOverviewTab) activeTab = 'overview';
	}
	$: if (activeTab === 'preview' && !$showFilePreview) {
		if (showFilesTab) activeTab = 'files';
		else if (showControlsTab) activeTab = 'controls';
		else if (showOverviewTab) activeTab = 'overview';
	}
	$: if (!showControlsTab && activeTab === 'controls') {
		if (showFilesTab) activeTab = 'files';
		else if (showOverviewTab) activeTab = 'overview';
	}

	// Auto-close if there are no visible tabs
	$: if (!showControlsTab && !showFilesTab && !showOverviewTab && !showPreviewTab) {
		showControls.set(false);
	}

	// Auto-switch to Files tab when display_file is triggered
	$: {
		if ($showFileNavPath && $showFileNavPath !== lastShowFileNavPath) {
			lastShowFileNavPath = $showFileNavPath;
			activeTab = 'files';
			showControls.set(true);
		} else if (!$showFileNavPath) {
			lastShowFileNavPath = null;
		}
	}

	$: {
		if ($showFilePreview && !wasFilePreviewOpen) {
			wasFilePreviewOpen = true;
			activeTab = 'preview';
			showControls.set(true);
		}

		if (!$showFilePreview && wasFilePreviewOpen) {
			wasFilePreviewOpen = false;
		}
	}

	// Auto-open Files tab when a terminal is selected
	$: {
		if ($selectedTerminalId && $selectedTerminalId !== lastSelectedTerminalId) {
			lastSelectedTerminalId = $selectedTerminalId;
			activeTab = 'files';
			showControls.set(true);
		} else if (!$selectedTerminalId) {
			lastSelectedTerminalId = null;
		}
	}

	// Attach a terminal file to the chat input
	const handleTerminalAttach = async (blob: Blob, name: string, contentType: string) => {
		const tempItemId = uuidv4();
		const fileItem = {
			type: 'file',
			file: '',
			id: null,
			url: '',
			name,
			collection_name: '',
			status: 'uploading',
			error: '',
			itemId: tempItemId,
			size: blob.size
		};

		files = [...files, fileItem];

		try {
			const file = new File([blob], name, { type: contentType || 'application/octet-stream' });
			const uploaded = await uploadFile(localStorage.token, file);
			if (!uploaded) throw new Error('Upload failed');

			const idx = files.findIndex((f) => f.itemId === tempItemId);
			if (idx !== -1) {
				files[idx] = {
					...fileItem,
					status: 'uploaded',
					file: uploaded,
					id: uploaded.id,
					url: `${uploaded.id}`,
					collection_name: uploaded?.meta?.collection_name
				};
				files = files;
			}
			toast.success($i18n.t('File attached to chat'));
		} catch (e) {
			files = files.filter((f) => f.itemId !== tempItemId);
			toast.error($i18n.t('Failed to attach file'));
		}
	};

	const getPaneWidthBounds = () => {
		const container = document.getElementById('chat-container');
		if (!container) {
			return {
				min: COMPACT_CONTROLS_PANE_WIDTH_PX,
				preferred: COMPACT_CONTROLS_PANE_WIDTH_PX,
				max: COMPACT_CONTROLS_PANE_MAX_WIDTH_PX
			};
		}

		if (activeTab === 'preview' || $showFilePreview) {
			const preferred = Math.min(
				Math.max(Math.floor(container.clientWidth * 0.42), PREVIEW_PANE_MIN_WIDTH_PX),
				PREVIEW_PANE_MAX_WIDTH_PX
			);
			return {
				min: PREVIEW_PANE_MIN_WIDTH_PX,
				preferred,
				max: PREVIEW_PANE_MAX_WIDTH_PX
			};
		}

		return {
			min: COMPACT_CONTROLS_PANE_WIDTH_PX,
			preferred: COMPACT_CONTROLS_PANE_WIDTH_PX,
			max: COMPACT_CONTROLS_PANE_MAX_WIDTH_PX
		};
	};

	const getPreferredPaneSize = () => {
		const container = document.getElementById('chat-container');
		if (!container) return minSize;

		const bounds = getPaneWidthBounds();
		return Math.max(minSize, Math.floor((bounds.preferred / container.clientWidth) * 100));
	};

	export const openPane = () => {
		const preferredMinSize = getPreferredPaneSize();
		if (parseInt(localStorage?.chatControlsSize)) {
			const container = document.getElementById('chat-container');
			const bounds = getPaneWidthBounds();
			const savedWidth = Math.min(parseInt(localStorage?.chatControlsSize), bounds.max);
			let size = Math.floor((savedWidth / container.clientWidth) * 100);
			pane.resize(Math.max(size, preferredMinSize));
		} else {
			pane.resize(preferredMinSize);
		}
	};

	const handleMediaQuery = async (e) => {
		if (e.matches) {
			largeScreen = true;
			if ($showCallOverlay) {
				showCallOverlay.set(false);
				await tick();
				showCallOverlay.set(true);
			}
		} else {
			largeScreen = false;
			if ($showCallOverlay) {
				showCallOverlay.set(false);
				await tick();
				showCallOverlay.set(true);
			}
			pane = null;
		}
	};

	const isControlsResizerInteraction = (event: MouseEvent): boolean => {
		const path = typeof event.composedPath === 'function' ? event.composedPath() : [];
		return path.some((node) => node instanceof HTMLElement && node.id === 'controls-resizer');
	};

	const onMouseDown = (event: MouseEvent) => {
		dragged = isControlsResizerInteraction(event);
	};
	const onMouseUp = () => {
		dragged = false;
	};

	onMount(() => {
		const mediaQuery = window.matchMedia('(min-width: 1024px)');
		mediaQuery.addEventListener('change', handleMediaQuery);
		handleMediaQuery(mediaQuery);

		let resizeObserver: ResizeObserver | null = null;
		let isDestroyed = false;

		// Wait for Svelte to render the Pane after largeScreen changed
		const init = async () => {
			await tick();

			if (isDestroyed) return;

			// If controls were persisted as open, set the pane to the saved size
			if ($showControls && pane) {
				openPane();
			}

			setTimeout(() => {
				paneReady = true;
			}, 0);

			const container = document.getElementById('chat-container') as HTMLElement;
			if (!container) return;

			minSize = Math.floor((COMPACT_CONTROLS_PANE_WIDTH_PX / container.clientWidth) * 100);
			resizeObserver = new ResizeObserver((entries) => {
				for (let entry of entries) {
					const width = entry.contentRect.width;
					minSize = Math.floor((COMPACT_CONTROLS_PANE_WIDTH_PX / width) * 100);
					const preferredMinSize = getPreferredPaneSize();
					if ($showControls) {
						if (pane && pane.isExpanded() && pane.getSize() < preferredMinSize) {
							pane.resize(preferredMinSize);
						} else {
							const bounds = getPaneWidthBounds();
							const savedWidth = Math.min(parseInt(localStorage?.chatControlsSize), bounds.max);
							let size = Math.floor((savedWidth / container.clientWidth) * 100);
							if (size < preferredMinSize && pane) pane.resize(preferredMinSize);
						}
					}
				}
			});
			resizeObserver.observe(container);
		};
		init();

		document.addEventListener('mousedown', onMouseDown);
		document.addEventListener('mouseup', onMouseUp);

		return () => {
			isDestroyed = true;
			paneReady = false;
			resizeObserver?.disconnect();
			if (!largeScreen) {
				showControls.set(false);
			}
			mediaQuery.removeEventListener('change', handleMediaQuery);
			document.removeEventListener('mousedown', onMouseDown);
			document.removeEventListener('mouseup', onMouseUp);
		};
	});

	const closeHandler = () => {
		if (!largeScreen) {
			showControls.set(false);
		}
		showArtifacts.set(false);
		showEmbeds.set(false);
		showFilePreview.set(false);
		if ($showCallOverlay) showCallOverlay.set(false);
	};

	$: if (paneReady && !chatId) closeHandler();
	$: if (paneReady && largeScreen && $showControls && $showFilePreview && pane?.isExpanded()) {
		const preferredSize = getPreferredPaneSize();
		if (pane.getSize() < preferredSize) {
			pane.resize(preferredSize);
		}
	}

	// Helper: is a "special" full-screen panel active?
	$: specialPanel = $showCallOverlay || $showArtifacts || $showEmbeds;
</script>

{#if !largeScreen}
	{#if $showControls}
		<Drawer
			show={$showControls}
			onClose={() => {
				closeHandler();
			}}
			className="min-h-[100dvh] !bg-white dark:!bg-gray-850"
		>
			<div class="h-[100dvh] flex flex-col">
				{#if $showCallOverlay}
					<div
						class="h-full max-h-[100dvh] bg-white text-gray-700 dark:bg-black dark:text-gray-300 flex justify-center"
					>
						<CallOverlay
							bind:files
							{submitPrompt}
							{stopResponse}
							{modelId}
							{chatId}
							{eventTarget}
							on:close={() => showControls.set(false)}
						/>
					</div>
				{:else if $showEmbeds}
					<Embeds />
				{:else if $showArtifacts}
					<Artifacts {history} />
				{:else}
					<!-- Controls + Files tabs -->
					<div class="flex flex-col h-full min-h-0">
						{#if !isPreviewMode}
							<!-- Tab bar -->
							<div class="flex items-center justify-between px-2 pt-2.5 pb-2 shrink-0">
								<div class="flex gap-1 min-w-0 overflow-x-auto scrollbar-hidden">
									{#if showControlsTab}
										<button
											class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {activeTab ===
											'controls'
												? 'bg-gray-100 dark:bg-gray-800 font-medium text-gray-900 dark:text-white'
												: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
											on:click={() => {
												if ($showFilePreview) showFilePreview.set(false);
												activeTab = 'controls';
											}}
										>
											控制
										</button>
									{/if}
									{#if showFilesTab}
										<button
											class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {activeTab ===
											'files'
												? 'bg-gray-100 dark:bg-gray-800 font-medium text-gray-900 dark:text-white'
												: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
											on:click={() => {
												if ($showFilePreview) showFilePreview.set(false);
												activeTab = 'files';
											}}
										>
											文件
										</button>
									{/if}
									{#if showOverviewTab}
										<button
											class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {activeTab ===
											'overview'
												? 'bg-gray-100 dark:bg-gray-800 font-medium text-gray-900 dark:text-white'
												: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
											on:click={() => {
												if ($showFilePreview) showFilePreview.set(false);
												activeTab = 'overview';
											}}
										>
											概览
										</button>
									{/if}
									{#if showPreviewTab}
										<button
											class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {activeTab ===
											'preview'
												? 'bg-gray-100 dark:bg-gray-800 font-medium text-gray-900 dark:text-white'
												: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
											on:click={() => {
												activeTab = 'preview';
												showFilePreview.set(true);
											}}
										>
											文件预览
										</button>
									{/if}
								</div>
								<button
									class="p-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition text-gray-500 dark:text-gray-400"
									on:click={() => showControls.set(false)}
									aria-label={$i18n.t('Close')}
								>
									<svg
										xmlns="http://www.w3.org/2000/svg"
										viewBox="0 0 24 24"
										fill="none"
										stroke="currentColor"
										stroke-width="1.5"
										class="size-4"
									>
										<path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12" />
									</svg>
								</button>
							</div>
						{/if}

						<div
							class="flex-1 min-h-0 {isPreviewMode || activeTab === 'overview'
								? 'h-full'
								: activeTab === 'controls'
									? 'overflow-y-auto px-3 pt-1'
									: ''}"
						>
							{#if $showFilePreview}
								<FilePreview {history} />
							{:else if activeTab === 'overview'}
								<Overview
									{history}
									onNodeClick={(e) => {
										const node = e.node;
										showMessage(node.data.message, true);
									}}
									onClose={() => showControls.set(false)}
								/>
							{:else if activeTab === 'files' && $selectedTerminalId}
								<FileNav onAttach={handleTerminalAttach} />
							{:else if activeTab === 'files' && codeInterpreterEnabled}
								<PyodideFileNav />
							{:else}
								<Controls embed={true} {models} bind:chatFiles bind:params />
							{/if}
						</div>
					</div>
				{/if}
			</div>
		</Drawer>
	{/if}
{:else}
	{#if $showControls}
		<PaneResizer
			class="relative flex items-center justify-center group border-l border-gray-50 dark:border-gray-850/30 hover:border-gray-200 dark:hover:border-gray-800 transition z-20"
			id="controls-resizer"
		>
			<div
				class="absolute -left-1.5 -right-1.5 -top-0 -bottom-0 z-20 cursor-col-resize bg-transparent"
			/>
		</PaneResizer>
	{/if}

	<Pane
		bind:pane
		defaultSize={0}
		onResize={(size) => {
			if ($showControls && pane.isExpanded()) {
				const preferredMinSize = getPreferredPaneSize();
				const container = document.getElementById('chat-container');
				if (!container) return;

				const bounds = getPaneWidthBounds();
				const maxSize = Math.floor((bounds.max / container.clientWidth) * 100);
				const clampedSize = Math.min(Math.max(size, preferredMinSize), maxSize);

				if (size < preferredMinSize) pane.resize(preferredMinSize);
				if (size > maxSize) pane.resize(maxSize);
				if (size < preferredMinSize) {
					localStorage.chatControlsSize = 0;
				} else {
					localStorage.chatControlsSize = Math.min(
						Math.floor((clampedSize / 100) * container.clientWidth),
						bounds.max
					);
				}
			}
		}}
		onCollapse={() => {
			if (paneReady) showControls.set(false);
		}}
		collapsible={true}
		class="z-10 bg-white dark:bg-gray-850"
	>
		{#if $showControls}
			<div class="flex max-h-full min-h-full">
				<div
					class="w-full {specialPanel && !$showCallOverlay
						? ' '
						: 'bg-white dark:shadow-lg dark:bg-gray-850'} z-40 pointer-events-auto {activeTab ===
						'files' || $showFilePreview
						? ''
						: 'overflow-y-auto'} scrollbar-hidden"
					id="controls-container"
				>
					{#if $showCallOverlay}
						<div class="w-full h-full flex justify-center">
							<CallOverlay
								bind:files
								{submitPrompt}
								{stopResponse}
								{modelId}
								{chatId}
								{eventTarget}
								on:close={() => showControls.set(false)}
							/>
						</div>
					{:else if $showEmbeds}
						<Embeds overlay={dragged} />
					{:else if $showArtifacts}
						<Artifacts {history} overlay={dragged} />
					{:else}
						<!-- Controls + Files tabs -->
						<div class="flex flex-col h-full min-h-0">
							{#if !isPreviewMode}
								<!-- Tab bar -->
								<div class="flex items-center justify-between px-2 pt-2.5 pb-2 shrink-0">
									<div class="flex gap-1 min-w-0 overflow-x-auto scrollbar-hidden">
										{#if showControlsTab}
											<button
												class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {activeTab ===
												'controls'
													? 'bg-gray-100 dark:bg-gray-800 font-medium text-gray-900 dark:text-white'
													: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
												on:click={() => {
													if ($showFilePreview) showFilePreview.set(false);
													activeTab = 'controls';
												}}
											>
												控制
											</button>
										{/if}
										{#if showFilesTab}
											<button
												class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {activeTab ===
												'files'
													? 'bg-gray-100 dark:bg-gray-800 font-medium text-gray-900 dark:text-white'
													: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
												on:click={() => {
													if ($showFilePreview) showFilePreview.set(false);
													activeTab = 'files';
												}}
											>
												文件
											</button>
										{/if}
										{#if showOverviewTab}
											<button
												class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {activeTab ===
												'overview'
													? 'bg-gray-100 dark:bg-gray-800 font-medium text-gray-900 dark:text-white'
													: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
												on:click={() => {
													if ($showFilePreview) showFilePreview.set(false);
													activeTab = 'overview';
												}}
											>
												概览
											</button>
										{/if}
										{#if showPreviewTab}
											<button
												class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {activeTab ===
												'preview'
													? 'bg-gray-100 dark:bg-gray-800 font-medium text-gray-900 dark:text-white'
													: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
												on:click={() => {
													activeTab = 'preview';
													showFilePreview.set(true);
												}}
											>
												文件预览
											</button>
										{/if}
									</div>
									<button
										class="p-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition text-gray-500 dark:text-gray-400"
										on:click={() => showControls.set(false)}
										aria-label={$i18n.t('Close')}
									>
										<svg
											xmlns="http://www.w3.org/2000/svg"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.5"
											class="size-4"
										>
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												d="M6 18 18 6M6 6l12 12"
											/>
										</svg>
									</button>
								</div>
							{/if}

							<div
								class="flex-1 min-h-0 {isPreviewMode || activeTab === 'overview'
									? 'h-full'
									: activeTab === 'controls'
										? 'overflow-y-auto px-3 pt-1'
										: ''}"
							>
								{#if $showFilePreview}
									<FilePreview {history} overlay={dragged} />
								{:else if activeTab === 'overview'}
									<Overview
										{history}
										onNodeClick={(e) => {
											const node = e.node;
											if (node?.data?.message?.favorite) {
												history.messages[node.data.message.id].favorite = true;
											} else {
												history.messages[node.data.message.id].favorite = null;
											}
											showMessage(node.data.message, true);
										}}
										onClose={() => showControls.set(false)}
									/>
								{:else if activeTab === 'files' && $selectedTerminalId}
									<FileNav onAttach={handleTerminalAttach} overlay={dragged} />
								{:else if activeTab === 'files' && codeInterpreterEnabled}
									<PyodideFileNav overlay={dragged} />
								{:else}
									<Controls embed={true} {models} bind:chatFiles bind:params />
								{/if}
							</div>
						</div>
					{/if}
				</div>
			</div>
		{/if}
	</Pane>
{/if}
