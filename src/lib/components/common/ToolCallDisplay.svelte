<script context="module" lang="ts">
	const MIN_VISIBLE_RUNNING_MS = 900;
	type ToolVisualTiming = { firstSeenAt: number };
	const toolVisualTimingByKey = new Map<string, ToolVisualTiming>();
</script>

<script lang="ts">
	import { decode } from 'html-entities';
	import { v4 as uuidv4 } from 'uuid';

	import { getContext, onDestroy } from 'svelte';
	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';

	import ChevronDown from '../icons/ChevronDown.svelte';
	import Markdown from '../chat/Messages/Markdown.svelte';
	import Image from './Image.svelte';
	import FullHeightIframe from './FullHeightIframe.svelte';
	import { downloadFileBlob } from '$lib/apis/terminal';
	import {
		selectedGeneratedFilePreviewId,
		settings,
		showArtifacts,
		showCallOverlay,
		showControls,
		showEmbeds,
		showFilePreview,
		showOverview,
		selectedTerminalId,
		terminalServers
	} from '$lib/stores';
	import { openGeneratedFilePreview } from '$lib/utils/generated-file-preview';
	import {
		getToolCallArtifactEvidence,
		isFileGeneratingToolId,
		resolveToolCallStatus
	} from '$lib/utils/generated-files';
	import { normalizeToolId, resolveToolDisplay } from '$lib/utils/tool-display';

	export let id: string = '';
	export let attributes: {
		type?: string;
		id?: string;
		call_key?: string;
		name?: string;
		tool_id?: string;
		tool_name?: string;
		arguments?: string;
		result?: string;
		files?: string;
		embeds?: string;
		done?: string;
		status?: string;
	} = {};

	export let open = false;
	export let className = '';
	export let embedded = false;
	export let disableVisualStatusDelay = false;

	const RESULT_PREVIEW_LIMIT = 10000;
	let expandedResult = false;
	let visualStatusTick = 0;
	let visualStatusTimer: ReturnType<typeof setTimeout> | null = null;

	$: if (!open) expandedResult = false;
	export let buttonClassName = '';

	const componentId = id || uuidv4();

	function parseJSONString(str: string) {
		try {
			return parseJSONString(JSON.parse(str));
		} catch (e) {
			return str;
		}
	}

	function formatJSONString(str: string) {
		try {
			const parsed = parseJSONString(str);
			if (typeof parsed === 'object') {
				return JSON.stringify(parsed, null, 2);
			} else {
				return String(parsed);
			}
		} catch (e) {
			return str;
		}
	}

	function parseArguments(str: string): Record<string, unknown> | null {
		try {
			const parsed = parseJSONString(str);
			if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
				return parsed as Record<string, unknown>;
			}
			return null;
		} catch {
			return null;
		}
	}

	function getStatusMessage(status: string): string {
		if (status === 'success') return '已完成';
		if (status === 'timeout') return '已超时';
		if (status === 'error') return '运行失败';
		return '执行中';
	}

	type GeneratedTerminalFile = {
		name: string;
		path: string;
	};

	function getStringField(record: Record<string, unknown> | null, keys: string[]): string {
		for (const key of keys) {
			const value = record?.[key];
			if (typeof value === 'string' && value.trim()) {
				return value.trim();
			}
		}
		return '';
	}

	function getGeneratedTerminalFile(
		toolName: string | undefined,
		parsedResult: unknown
	): GeneratedTerminalFile | null {
		if (!parsedResult || typeof parsedResult !== 'object' || Array.isArray(parsedResult)) {
			return null;
		}

		const normalizedToolId = normalizeToolId(toolName);
		if (
			!['write_file', 'replace_file_content', 'display_file', 'edit_file'].includes(
				normalizedToolId
			)
		) {
			return null;
		}

		const record = parsedResult as Record<string, unknown>;
		if (record.success === false) return null;

		const path = getStringField(record, ['path', 'output_path', 'target_path', 'file_path']);
		if (!path) return null;

		const name =
			getStringField(record, ['filename', 'fileName', 'name']) ||
			path.split('/').pop()?.split('\\').pop()?.trim() ||
			'generated-file';

		return { name, path };
	}

	type ActiveTerminal = { url: string; key: string } | null;

	$: systemTerminal = $selectedTerminalId
		? (($terminalServers ?? []).find((terminal: any) => terminal.id === $selectedTerminalId) ??
			null)
		: (($terminalServers ?? [])[0] ?? null);
	$: directTerminal =
		($settings?.terminalServers ?? []).find(
			(server: any) => server.url === $selectedTerminalId && server.enabled
		) ?? null;
	$: activeTerminal = (
		directTerminal
			? { url: directTerminal.url, key: directTerminal.api_key }
			: systemTerminal
				? { url: systemTerminal.url, key: systemTerminal.key }
				: null
	) as ActiveTerminal;

	async function downloadResultFile(file: GeneratedTerminalFile) {
		if (!activeTerminal) return;
		const result = await downloadFileBlob(activeTerminal.url, activeTerminal.key, file.path);
		if (!result) return;

		const url = URL.createObjectURL(result.blob);
		const link = document.createElement('a');
		link.href = url;
		link.download = result.filename || file.name;
		link.click();
		URL.revokeObjectURL(url);
	}

	function openResultFile(file: GeneratedTerminalFile) {
		openGeneratedFilePreview(`tool:${file.path}:${file.name}`, {
			showControls,
			showFilePreview,
			selectedGeneratedFilePreviewId,
			showOverview,
			showArtifacts,
			showEmbeds,
			showCallOverlay
		});
	}

	function clearVisualStatusTimer() {
		if (visualStatusTimer) {
			clearTimeout(visualStatusTimer);
			visualStatusTimer = null;
		}
	}

	function scheduleVisualStatusRefresh(delayMs: number) {
		if (delayMs <= 0 || visualStatusTimer) return;
		visualStatusTimer = setTimeout(() => {
			visualStatusTimer = null;
			visualStatusTick += 1;
		}, delayMs);
	}

	function getToolVisualKey(): string {
		const callKey = (attributes?.call_key || '').trim();
		if (callKey) return `call_key:${callKey}`;
		const toolCallId = (attributes?.id || '').trim();
		if (toolCallId) return `id:${toolCallId}`;
		const name = (attributes?.tool_id || attributes?.name || '').trim();
		const args = (attributes?.arguments || '').trim();
		if (name || args) return `name_args:${name}|${args}`;
		return id || componentId;
	}

	function getEffectiveStatus(
		rawStatus: string,
		visualKey: string,
		allowDelay: boolean
	): string {
		if (!visualKey) return rawStatus;
		if (!allowDelay) {
			clearVisualStatusTimer();
			return rawStatus;
		}

		const now = Date.now();
		let timing = toolVisualTimingByKey.get(visualKey);
		if (!timing) {
			timing = { firstSeenAt: now };
			toolVisualTimingByKey.set(visualKey, timing);
		}

		if (rawStatus === 'running') {
			clearVisualStatusTimer();
			return rawStatus;
		}

		const elapsedMs = now - timing.firstSeenAt;
		if (elapsedMs < MIN_VISIBLE_RUNNING_MS) {
			scheduleVisualStatusRefresh(MIN_VISIBLE_RUNNING_MS - elapsedMs);
			return 'running';
		}

		clearVisualStatusTimer();
		return rawStatus;
	}

	onDestroy(() => {
		clearVisualStatusTimer();
	});

	$: args = decode(attributes?.arguments ?? '');
	$: result = decode(attributes?.result ?? '');
	$: files = parseJSONString(decode(attributes?.files ?? ''));
	$: embeds = parseJSONString(decode(attributes?.embeds ?? ''));
	$: parsedArgs = parseArguments(args);
	$: parsedResult = parseJSONString(result);
	$: toolDisplay = resolveToolDisplay({
		toolId: attributes?.tool_id,
		toolName: attributes?.tool_name,
		legacyName: attributes?.name,
		parsedArgs
	});
	$: displayName = toolDisplay.toolName;
	$: normalizedToolId = toolDisplay.toolId || normalizeToolId(attributes?.name);
	$: normalizedAttrs = { ...(attributes ?? {}), tool_id: normalizedToolId || attributes?.tool_id || '' };
	$: toolVisualKey = getToolVisualKey();
	$: visualStatusTick;
	$: hasFiles = Array.isArray(files) && files.length > 0;
	$: terminalResultFile = getGeneratedTerminalFile(normalizedToolId, parsedResult);
	$: supportsArtifactInference = isFileGeneratingToolId(normalizedToolId);
	$: hasResultArtifacts = supportsArtifactInference && getToolCallArtifactEvidence(normalizedAttrs);
	$: hasArtifactEvidence = hasFiles || Boolean(terminalResultFile) || hasResultArtifacts;
	$: statusCandidate = resolveToolCallStatus(normalizedAttrs);
	$: status = getEffectiveStatus(
		statusCandidate,
		toolVisualKey,
		!disableVisualStatusDelay && !hasArtifactEvidence
	);
	$: isTerminal = status !== 'running';
	$: isExecuting = status === 'running';
	$: statusMessage = getStatusMessage(status);
	$: hasEmbeds = embeds && Array.isArray(embeds) && embeds.length > 0;
	$: canExpand = !hasEmbeds && (Boolean(args) || Boolean(result) || hasFiles);
	$: if (!canExpand) open = false;
</script>

<div {id} class={className}>
	<div
		class={`w-full overflow-hidden border border-gray-200/90 dark:border-gray-800 ${
			embedded
				? 'rounded-lg bg-white/80 dark:bg-gray-950/30'
				: 'rounded-xl bg-gray-50/80 dark:bg-gray-900/70'
		}`}
	>
		<button
			type="button"
			class={`flex w-full items-center justify-between gap-2 border-b border-gray-200/80 px-3 py-2 text-left dark:border-gray-800 ${buttonClassName} ${
				canExpand ? '' : 'cursor-default'
			} ${embedded ? 'bg-white/60 dark:bg-transparent' : ''}`}
			on:click={() => {
				if (canExpand) {
					open = !open;
				}
			}}
		>
			<div class="min-w-0">
				<div
					class="text-[11px] font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-300"
				>
					{displayName}
				</div>
				<div
					class="mt-0.5 line-clamp-1 text-xs text-gray-500 dark:text-gray-400 {isExecuting
						? 'shimmer'
						: ''}"
				>
					{statusMessage}
				</div>
			</div>

			{#if canExpand}
				<ChevronDown
					className={`size-3.5 shrink-0 text-gray-500 transition-transform ${
						open ? 'rotate-180' : ''
					}`}
				/>
			{/if}
		</button>

		{#if terminalResultFile}
			<div
				class={`flex flex-wrap items-center gap-1.5 px-3 pb-2 text-[11px] ${
					canExpand ? '' : 'pt-0'
				}`}
			>
				<button
					type="button"
					class="rounded-md border border-gray-200 bg-white px-2 py-0.5 text-gray-600 transition hover:bg-gray-100 hover:text-blue-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800 dark:hover:text-blue-400"
					on:click={() => {
						openResultFile(terminalResultFile);
					}}
				>
					查看文件
				</button>
				{#if activeTerminal}
					<button
						type="button"
						class="rounded-md border border-gray-200 bg-white px-2 py-0.5 text-gray-600 transition hover:bg-gray-100 hover:text-blue-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800 dark:hover:text-blue-400"
						on:click={async () => {
							await downloadResultFile(terminalResultFile);
						}}
					>
						下载文件
					</button>
				{/if}
			</div>
		{/if}

		{#if (open && canExpand) || hasEmbeds}
			<div transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}>
				<div class={`space-y-3 ${embedded ? 'px-3 py-2' : 'px-2 py-2'}`}>
					{#if hasEmbeds}
						{#each embeds as embed, idx}
							<div class="my-2" id={`${componentId}-tool-call-embed-${idx}`}>
								<FullHeightIframe
									src={embed}
									{args}
									allowScripts={true}
									allowForms={$settings?.iframeSandboxAllowForms ?? false}
									allowSameOrigin={$settings?.iframeSandboxAllowSameOrigin ?? false}
									allowPopups={true}
								/>
							</div>
						{/each}
					{:else}
						<div class="flex items-stretch gap-2">
							<div>
								<div class={`mb-1.5 px-1 ${embedded ? 'pt-2.5' : 'pt-3'}`}>
									<span class="relative flex size-1.5 items-center justify-center rounded-full">
										<span
											class="relative inline-flex size-1.5 rounded-full bg-gray-500 dark:bg-gray-400"
										></span>
									</span>
								</div>
							</div>

							<div class="flex-1 space-y-3">
								<div
									class="{isExecuting
										? 'shimmer'
										: ''} text-gray-500 dark:text-gray-500 text-base line-clamp-1 text-wrap"
								>
									{displayName}
								</div>

								<!-- Input -->
								{#if args}
									<div>
										<div
											class="text-[10px] uppercase tracking-wider font-medium text-gray-400 dark:text-gray-500 mb-1.5 px-1"
										>
											{$i18n.t('Input')}
										</div>

										{#if parsedArgs}
											<div class="px-1 space-y-0.5">
												{#each Object.entries(parsedArgs) as [key, value]}
													<div class="flex gap-2 text-xs py-0.5">
														<span class="font-medium text-gray-600 dark:text-gray-400 shrink-0"
															>{key}</span
														>
														<span class="text-gray-800 dark:text-gray-200 break-all"
															>{typeof value === 'object' ? JSON.stringify(value) : value}</span
														>
													</div>
												{/each}
											</div>
										{:else}
											<div class="tool-call-body w-full max-w-none!">
												<Markdown
													id={`${componentId}-tool-call-args`}
													content={`\`\`\`json\n${formatJSONString(args)}\n\`\`\``}
												/>
											</div>
										{/if}
									</div>
								{/if}

								<!-- Output -->
								{#if isTerminal && result}
									<div>
										<div
											class="text-[10px] uppercase tracking-wider font-medium text-gray-400 dark:text-gray-500 mb-1.5 px-1"
										>
											{status === 'success' ? $i18n.t('Output') : '错误信息'}
										</div>
										<div class="w-full max-w-none!">
											{#if typeof parsedResult === 'object' && parsedResult !== null}
												<Markdown
													id={`${componentId}-tool-call-result`}
													content={`\`\`\`json\n${JSON.stringify(parsedResult, null, 2)}\n\`\`\``}
												/>
											{:else}
												{@const resultStr = String(parsedResult)}
												{@const isTruncated =
													resultStr.length > RESULT_PREVIEW_LIMIT && !expandedResult}
												<pre
													class="text-xs text-gray-600 dark:text-gray-300 whitespace-pre-wrap break-words font-mono">{isTruncated
														? resultStr.slice(0, RESULT_PREVIEW_LIMIT)
														: resultStr}</pre>
												{#if isTruncated}
													<button
														class="mt-1 text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition"
														on:click|stopPropagation={() => {
															expandedResult = true;
														}}
													>
														{$i18n.t('Show all ({{COUNT}} characters)', {
															COUNT: resultStr.length.toLocaleString()
														})}
													</button>
												{/if}
											{/if}
										</div>
									</div>
								{/if}
							</div>
						</div>
					{/if}
				</div>
			</div>
		{/if}
	</div>

	<!-- Files display (images etc.) when done -->
	{#if isTerminal}
		{#if typeof files === 'object'}
			{#each files ?? [] as file, idx}
				{#if typeof file === 'string'}
					{#if file.startsWith('data:image/')}
						<Image id={`${componentId}-tool-call-result-${idx}`} src={file} alt="Image" />
					{/if}
				{:else if typeof file === 'object'}
					{#if (file.type === 'image' || (file?.content_type ?? '').startsWith('image/')) && file.url}
						<Image id={`${componentId}-tool-call-result-${idx}`} src={file.url} alt="Image" />
					{/if}
				{/if}
			{/each}
		{/if}
	{/if}
</div>
