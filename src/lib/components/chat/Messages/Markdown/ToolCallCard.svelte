<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { downloadFileBlob } from '$lib/apis/terminal';
	import {
		selectedGeneratedFilePreviewId,
		selectedTerminalId,
		settings,
		showArtifacts,
		showCallOverlay,
		showControls,
		showEmbeds,
		showFilePreview,
		showOverview,
		terminalServers
	} from '$lib/stores';
	import { unescapeHtml } from '$lib/utils';
	import {
		type GeneratedFileItem,
		collectGeneratedFilesFromValue,
		parseNestedJSON,
		triggerGeneratedFileDownload,
		resolveToolCallStatus
	} from '$lib/utils/generated-files';
	import { openGeneratedFilePreview } from '$lib/utils/generated-file-preview';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import Wrench from '$lib/components/icons/Wrench.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import Document from '$lib/components/icons/Document.svelte';
	import CommandLine from '$lib/components/icons/CommandLine.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import { resolveToolDisplay } from '$lib/utils/tool-display';

	const i18n = getContext<Writable<i18nType>>('i18n');
	const TOOL_FILE_LIST_SUPPRESSED_TOOL_IDS = new Set([
		'write_file',
		'replace_file_content',
		'display_file',
		'write_structured_file',
		'gotenberg_convert',
		'pdf_create_document',
		'pdf_inspect_form',
		'pdf_fill_form_tool',
		'pdf_reformat_document',
		'xlsx_add_column_tool',
		'xlsx_insert_row_tool'
	]);

	export let token: any;

	type ToolFileItem = GeneratedFileItem;
	type ActiveTerminal = { url: string; key: string } | null;
	type ToolRunStatus = 'running' | 'success' | 'error' | 'timeout';

	const TOOL_STATUS_DISPLAY: Record<
		ToolRunStatus,
		{
			summary: string;
			badgeLabel: string;
			badgeClass: string;
			dotClass: string;
			spinnerClass?: string;
		}
	> = {
		running: {
			summary: '执行中',
			badgeLabel: 'Running',
			badgeClass:
				'border-blue-200 bg-blue-100 text-blue-700 dark:border-blue-900 dark:bg-blue-950/60 dark:text-blue-300',
			dotClass: 'bg-blue-500',
			spinnerClass: 'text-blue-600 dark:text-blue-400'
		},
		success: {
			summary: '已完成',
			badgeLabel: 'Completed',
			badgeClass:
				'border-emerald-200 bg-emerald-100 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300',
			dotClass: 'bg-emerald-500'
		},
		error: {
			summary: '执行失败',
			badgeLabel: 'Error',
			badgeClass:
				'border-rose-200 bg-rose-100 text-rose-700 dark:border-rose-900 dark:bg-rose-950/60 dark:text-rose-300',
			dotClass: 'bg-rose-500'
		},
		timeout: {
			summary: '执行超时',
			badgeLabel: 'Timeout',
			badgeClass:
				'border-amber-200 bg-amber-100 text-amber-700 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300',
			dotClass: 'bg-amber-500'
		}
	};

	const parseAttr = (value: unknown): unknown => {
		if (typeof value !== 'string') return value;
		const decoded = unescapeHtml(value);
		if (!decoded) return '';

		try {
			let parsed = JSON.parse(decoded);
			if (typeof parsed === 'string') {
				const trimmed = parsed.trim();
				if (
					(trimmed.startsWith('{') && trimmed.endsWith('}')) ||
					(trimmed.startsWith('[') && trimmed.endsWith(']'))
				) {
					try {
						parsed = JSON.parse(trimmed);
					} catch {
						return parsed;
					}
				}
			}
			return parsed;
		} catch {
			return decoded;
		}
	};

	const prettyValue = (value: unknown): string => {
		if (value === null || value === undefined) return '';
		if (typeof value === 'string') return value;
		try {
			return JSON.stringify(value, null, 2);
		} catch {
			return String(value);
		}
	};

	const normalizeFiles = (rawFiles: unknown): ToolFileItem[] => {
		return collectGeneratedFilesFromValue(rawFiles, 'tool');
	};

	const getToolMeta = (
		toolId: string | undefined,
		toolName: string | undefined,
		legacyName: string | undefined,
		args: Record<string, unknown> | null = null
	) => {
		const resolved = resolveToolDisplay({
			toolId,
			toolName,
			legacyName,
			parsedArgs: args
		});
		const category = resolved.category;
		const displayName = resolved.toolName;
		if (category === 'web') {
			return {
				toolId: resolved.toolId,
				icon: Search,
				label: displayName,
				iconClass: 'text-blue-600 dark:text-blue-400',
				bgClass: 'bg-blue-100 dark:bg-blue-950/60'
			};
		}
		if (category === 'file') {
			return {
				toolId: resolved.toolId,
				icon: Document,
				label: displayName,
				iconClass: 'text-emerald-600 dark:text-emerald-400',
				bgClass: 'bg-emerald-100 dark:bg-emerald-950/60'
			};
		}
		if (category === 'code') {
			return {
				toolId: resolved.toolId,
				icon: CommandLine,
				label: displayName,
				iconClass: 'text-purple-600 dark:text-purple-400',
				bgClass: 'bg-purple-100 dark:bg-purple-950/60'
			};
		}
		return {
			toolId: resolved.toolId,
			icon: Wrench,
			label: displayName,
			iconClass: 'text-gray-600 dark:text-gray-300',
			bgClass: 'bg-gray-100 dark:bg-gray-800'
		};
	};

	const getToolStatus = (value: any): ToolRunStatus => {
		const attrs = (value?.attributes ?? {}) as Record<string, string>;
		return resolveToolCallStatus(attrs);
	};

	let open = false;
	let systemTerminal: any = null;
	let directTerminal: any = null;
	let activeTerminal: ActiveTerminal = null;
	let showFilesSection = false;
	$: status = getToolStatus(token);
	$: statusDisplay = TOOL_STATUS_DISPLAY[status];
	$: done = status !== 'running';
	$: summary = status === 'success' ? token?.summary || statusDisplay.summary : statusDisplay.summary;

	$: argumentsParsed = parseAttr(token?.attributes?.arguments);
	$: resultParsed = parseNestedJSON(parseAttr(token?.attributes?.result));
	$: filesParsed = normalizeFiles([
		...normalizeFiles(parseNestedJSON(parseAttr(token?.attributes?.files))),
		...collectGeneratedFilesFromValue(resultParsed, 'tool')
	]);
	$: embedsParsed = parseAttr(token?.attributes?.embeds);
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

	$: argumentsText = prettyValue(argumentsParsed);
	$: resultText = prettyValue(resultParsed);
	$: embedsText = prettyValue(embedsParsed);
	$: showFilesSection =
		filesParsed.length > 0 && !TOOL_FILE_LIST_SUPPRESSED_TOOL_IDS.has(meta?.toolId ?? '');

	$: hasBody = !!argumentsText || !!resultText || showFilesSection || !!embedsText;
	$: if (!hasBody) {
		open = false;
	}

	$: argsRecord =
		argumentsParsed && typeof argumentsParsed === 'object' && !Array.isArray(argumentsParsed)
			? (argumentsParsed as Record<string, unknown>)
			: null;
	$: meta = getToolMeta(
		token?.attributes?.tool_id,
		token?.attributes?.tool_name,
		token?.attributes?.name,
		argsRecord
	);

	const openFile = (file: ToolFileItem) => {
		openGeneratedFilePreview(file.id, {
			showControls,
			showFilePreview,
			selectedGeneratedFilePreviewId,
			showOverview,
			showArtifacts,
			showEmbeds,
			showCallOverlay
		});
	};

	const downloadFile = async (file: ToolFileItem) => {
		if (file.downloadMode === 'terminal' && file.path && activeTerminal) {
			const result = await downloadFileBlob(activeTerminal.url, activeTerminal.key, file.path);
			if (!result) return;

			const objectUrl = URL.createObjectURL(result.blob);
			const link = document.createElement('a');
			link.href = objectUrl;
			link.download = result.filename || file.name;
			link.click();
			URL.revokeObjectURL(objectUrl);
			return;
		}

		if (file.url) {
			await triggerGeneratedFileDownload(file.url, file.name);
		}
	};
</script>

<div
	data-tool-call-container="true"
	class="my-2 overflow-hidden rounded-xl border border-gray-200/90 bg-gray-50/80 dark:border-gray-800 dark:bg-gray-900/70"
>
	<button
		type="button"
		class="flex w-full items-start gap-3 px-3 py-2 text-left"
		on:click={() => {
			if (hasBody) open = !open;
		}}
	>
		<div class="relative mt-0.5 shrink-0">
			<div class={`flex size-7 items-center justify-center rounded-full ${meta.bgClass}`}>
				{#if !done}
					<Spinner className={`size-3.5 ${statusDisplay.spinnerClass ?? ''}`} />
				{:else}
					<svelte:component this={meta.icon} className={`size-3.5 ${meta.iconClass}`} />
				{/if}
			</div>
			<span
				class={`absolute -bottom-0.5 -right-0.5 size-2 rounded-full border border-white dark:border-gray-900 ${statusDisplay.dotClass}`}
			></span>
		</div>

		<div class="min-w-0 flex-1">
			<div class="line-clamp-1 text-[13px] font-medium text-gray-900 dark:text-gray-100">
				{meta.label}
			</div>
			<div class="line-clamp-1 text-xs text-gray-500 dark:text-gray-400">{summary}</div>
		</div>

		<div class="flex items-center gap-2">
			<span class={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${statusDisplay.badgeClass}`}>
				{$i18n.t(statusDisplay.badgeLabel)}
			</span>

			{#if hasBody}
				<ChevronDown
					className={`size-3.5 text-gray-500 transition-transform ${open ? 'rotate-180' : ''}`}
				/>
			{/if}
		</div>
	</button>

	{#if open && hasBody}
		<div
			data-tool-call-content="true"
			class="space-y-2 border-t border-gray-200/80 px-3 pb-3 pt-2 dark:border-gray-800"
		>
			{#if argumentsText}
				<div
					class="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-950/70"
				>
					<div
						class="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
					>
						Input
					</div>
					<pre
						class="max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs text-gray-800 dark:text-gray-200">{argumentsText}</pre>
				</div>
			{/if}

			{#if resultText}
				<div
					class="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-950/70"
				>
					<div
						class="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
					>
						Result
					</div>
					<pre
						class="max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs text-gray-800 dark:text-gray-200">{resultText}</pre>
				</div>
			{/if}

			{#if showFilesSection}
				<div
					class="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-950/70"
				>
					<div
						class="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
					>
						Files
					</div>
					<div class="space-y-1.5">
						{#each filesParsed as file}
							<div
								class="flex items-center justify-between gap-2 rounded-md border border-gray-200 bg-gray-50 px-2 py-1.5 dark:border-gray-700 dark:bg-gray-900/70"
							>
								<div class="min-w-0 flex items-center gap-2">
									{#if file.isImage && file.url}
										<img
											src={file.url}
											alt={file.name}
											class="size-7 rounded-md border border-gray-200 object-cover dark:border-gray-700"
										/>
									{/if}
									<button
										type="button"
										class="line-clamp-1 text-xs font-medium text-gray-800 hover:text-blue-600 dark:text-gray-100 dark:hover:text-blue-400"
										on:click={() => {
											openFile(file);
										}}
									>
										{file.name}
									</button>
								</div>
								<button
									type="button"
									class="inline-flex size-7 shrink-0 items-center justify-center rounded-md border border-gray-200 bg-white text-gray-600 hover:bg-gray-100 hover:text-blue-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800 dark:hover:text-blue-400"
									title={$i18n.t('Download')}
									on:click={async () => {
										await downloadFile(file);
									}}
								>
									<Download className="size-3.5" />
								</button>
							</div>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	{/if}
</div>
