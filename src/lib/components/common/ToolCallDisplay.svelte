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
	import CheckCircle from '../icons/CheckCircle.svelte';
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
		collectGeneratedFilesFromValue,
		getToolCallArtifactEvidence,
		isFileGeneratingToolId,
		resolveToolCallStatus
	} from '$lib/utils/generated-files';
	import {
		isHiddenHelperToolCall,
		normalizeToolId,
		resolveToolDisplay
	} from '$lib/utils/tool-display';

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
	const TODO_RESULT_MESSAGE_SUPPRESSIONS = new Set([
		'updated todo list',
		'todo list updated',
		'updated todos'
	]);
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

	type TodoStatus = 'pending' | 'in_progress' | 'completed';
	type TodoItem = {
		id: string;
		content: string;
		status: TodoStatus;
	};
	type TodoPayload = {
		title: string;
		todos: TodoItem[];
		counts: Record<TodoStatus, number>;
	};
	type TodoGroup = {
		status: TodoStatus;
		label: string;
		items: TodoItem[];
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

	function asRecord(value: unknown): Record<string, unknown> | null {
		if (!value || typeof value !== 'object' || Array.isArray(value)) {
			return null;
		}
		return value as Record<string, unknown>;
	}

	function normalizeTodoStatus(value: unknown): TodoStatus {
		if (typeof value !== 'string') return 'pending';
		const normalized = value.trim().toLowerCase();
		if (['completed', 'complete', 'done', 'success', 'finished'].includes(normalized)) {
			return 'completed';
		}
		if (['in_progress', 'in-progress', 'running', 'active', 'current'].includes(normalized)) {
			return 'in_progress';
		}
		return 'pending';
	}

	function getTodoContent(record: Record<string, unknown> | null): string {
		return getStringField(record, ['content', 'name', 'title', 'task', 'description']);
	}

	function normalizeTodoText(text: string): string {
		return text
			.replace(/[“”]/g, '"')
			.replace(/[‘’]/g, "'")
			.replace(/：/g, ':')
			.trim();
	}

	function getTodoFieldFromTextBlock(block: string, keys: string[]): string {
		for (const key of keys) {
			const singleQuoted = block.match(new RegExp(`['"]${key}['"]\\s*:\\s*'([^']*)'`));
			if (singleQuoted?.[1]?.trim()) {
				return singleQuoted[1].trim();
			}

			const doubleQuoted = block.match(new RegExp(`['"]${key}['"]\\s*:\\s*"([^"]*)"`));
			if (doubleQuoted?.[1]?.trim()) {
				return doubleQuoted[1].trim();
			}
		}

		return '';
	}

	function extractTodoListText(text: string): string {
		const normalized = normalizeTodoText(text);
		const successMatch = normalized.match(/updated todo list to\s*(\[[\s\S]*\])/i);
		if (successMatch?.[1]) {
			return successMatch[1];
		}

		const kwargsMatch = normalized.match(/['"]todos['"]\s*:\s*(\[[\s\S]*?\])\s*}/i);
		if (kwargsMatch?.[1]) {
			return kwargsMatch[1];
		}

		return '';
	}

	function parseTodoItemsFromText(value: unknown): TodoItem[] {
		if (typeof value !== 'string' || !value.trim()) return [];

		const listText = extractTodoListText(value);
		if (!listText) return [];

		const itemBlocks = Array.from(listText.matchAll(/\{[^{}]*\}/g)).map((match) => match[0]);
		return itemBlocks
			.map((block, index) => {
				const content = getTodoFieldFromTextBlock(block, [
					'content',
					'name',
					'title',
					'task',
					'description'
				]);
				if (!content) return null;

				return {
					id: getTodoFieldFromTextBlock(block, ['id']) || `todo-text-${index}`,
					content,
					status: normalizeTodoStatus(getTodoFieldFromTextBlock(block, ['status']))
				};
			})
			.filter((item): item is TodoItem => Boolean(item));
	}

	function parseTodoItems(value: unknown): TodoItem[] {
		if (!Array.isArray(value)) return [];

		return value
			.map((entry, index) => {
				const record = asRecord(entry);
				const content = getTodoContent(record);
				if (!content) return null;
				return {
					id: getStringField(record, ['id']) || `todo-${index}`,
					content,
					status: normalizeTodoStatus(record?.status)
				};
			})
			.filter((item): item is TodoItem => Boolean(item));
	}

	function buildTodoPayload(parsedArgs: Record<string, unknown> | null, parsedResult: unknown): TodoPayload | null {
		const resultRecord = asRecord(parsedResult);
		const candidateTodoLists = [
			parseTodoItems(parsedArgs?.todos),
			parseTodoItems(resultRecord?.todos),
			parseTodoItems(resultRecord?.tasks),
			parseTodoItems(resultRecord?.items),
			parseTodoItemsFromText(parsedResult)
		];
		const todos = candidateTodoLists.find((items) => items.length > 0) ?? [];

		if (todos.length === 0) return null;

		const counts: Record<TodoStatus, number> = {
			pending: 0,
			in_progress: 0,
			completed: 0
		};

		for (const todo of todos) {
			counts[todo.status] += 1;
		}

		return {
			title:
				getStringField(parsedArgs, ['title', 'name']) ||
				getStringField(resultRecord, ['title', 'name']),
			todos,
			counts
		};
	}

	function getTodoGroups(payload: TodoPayload | null): TodoGroup[] {
		if (!payload) return [];

		return [
			{ status: 'in_progress', label: '进行中', items: payload.todos.filter((todo) => todo.status === 'in_progress') },
			{ status: 'pending', label: '待处理', items: payload.todos.filter((todo) => todo.status === 'pending') },
			{ status: 'completed', label: '已完成', items: payload.todos.filter((todo) => todo.status === 'completed') }
		].filter((group) => group.items.length > 0);
	}

	function getTodoSummary(payload: TodoPayload | null, status: string): string {
		if (!payload) return '';

		const segments = [`${payload.todos.length} 项任务`];
		if (payload.counts.in_progress > 0) {
			segments.push(`${payload.counts.in_progress} 进行中`);
		}
		if (payload.counts.pending > 0) {
			segments.push(`${payload.counts.pending} 待处理`);
		}
		if (payload.counts.completed > 0) {
			segments.push(`${payload.counts.completed} 已完成`);
		}
		if (status === 'running') {
			segments.push('同步中');
		}
		if (status === 'error') {
			segments.push('更新失败');
		}
		if (status === 'timeout') {
			segments.push('更新超时');
		}

		return segments.join(' · ');
	}

	function getTodoResultMessage(parsedResult: unknown): string {
		if (typeof parsedResult === 'string') {
			return parsedResult.trim();
		}
		const record = asRecord(parsedResult);
		return getStringField(record, ['error', 'detail', 'message', 'summary', 'content', 'result']);
	}

	function shouldShowTodoResultMessage(message: string, status: string): boolean {
		if (!message) return false;
		if (status !== 'success') return true;
		const normalized = message.trim().toLowerCase();
		if (TODO_RESULT_MESSAGE_SUPPRESSIONS.has(normalized)) return false;
		return !normalized.startsWith('updated todo list to [');
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
	$: parsedFiles = collectGeneratedFilesFromValue(files, 'tool');
	$: hasAnyFiles = parsedFiles.length > 0;
	$: suppressInlineVisuals = isHiddenHelperToolCall({
		toolId: normalizedToolId,
		toolName: attributes?.tool_name,
		legacyName: attributes?.name,
		parsedArgs
	});
	$: visibleFiles = suppressInlineVisuals ? [] : parsedFiles;
	$: imageFiles = visibleFiles.filter((file) => file.isImage && file.url);
	$: visibleEmbeds =
		suppressInlineVisuals || !Array.isArray(embeds)
			? []
			: embeds.filter((embed): embed is string => typeof embed === 'string' && embed.trim().length > 0);
	$: hasFiles = visibleFiles.length > 0;
	$: terminalResultFile = getGeneratedTerminalFile(normalizedToolId, parsedResult);
	$: supportsArtifactInference = isFileGeneratingToolId(normalizedToolId);
	$: hasResultArtifacts = supportsArtifactInference && getToolCallArtifactEvidence(normalizedAttrs);
	$: hasArtifactEvidence = hasAnyFiles || Boolean(terminalResultFile) || hasResultArtifacts;
	$: statusCandidate = resolveToolCallStatus(normalizedAttrs);
	$: status = getEffectiveStatus(
		statusCandidate,
		toolVisualKey,
		!disableVisualStatusDelay && !hasArtifactEvidence
	);
	$: isTerminal = status !== 'running';
	$: isExecuting = status === 'running';
	$: statusMessage = getStatusMessage(status);
	$: todoPayload = normalizedToolId === 'write_todos' ? buildTodoPayload(parsedArgs, parsedResult) : null;
	$: todoGroups = getTodoGroups(todoPayload);
	$: todoSummary = getTodoSummary(todoPayload, status);
	$: todoResultMessage = getTodoResultMessage(parsedResult);
	$: showTodoResultMessage = shouldShowTodoResultMessage(todoResultMessage, status);
	$: hasTodoUI = Boolean(todoPayload && todoPayload.todos.length > 0);
	$: secondaryMessage = hasTodoUI && todoSummary ? todoSummary : statusMessage;
	$: hasEmbeds = visibleEmbeds.length > 0;
	$: canExpand = !hasEmbeds && (hasTodoUI || Boolean(args) || Boolean(result) || hasFiles);
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
					{secondaryMessage}
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
						{#each visibleEmbeds as embed, idx}
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

									{#if hasTodoUI}
										<div class="overflow-hidden rounded-2xl border border-sky-200/80 bg-white/90 dark:border-sky-900/60 dark:bg-gray-950/70">
											<div class="flex flex-wrap items-start justify-between gap-2 border-b border-sky-100/80 px-3 py-2 dark:border-sky-900/40">
												<div class="min-w-0">
													<div class="text-[10px] font-semibold uppercase tracking-[0.18em] text-sky-600 dark:text-sky-300">
														任务追踪
													</div>
													<div class="mt-1 text-sm font-medium text-gray-900 dark:text-gray-100">
														{todoPayload?.title || '当前任务清单'}
													</div>
												</div>
												<div class="flex flex-wrap items-center gap-1.5">
													<span class="rounded-full bg-sky-100 px-2 py-0.5 text-[11px] font-medium text-sky-700 dark:bg-sky-950/60 dark:text-sky-300">
														{todoPayload?.todos.length ?? 0} 项
													</span>
													{#if todoPayload && todoPayload.counts.in_progress > 0}
														<span class="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:bg-amber-950/60 dark:text-amber-300">
															{todoPayload.counts.in_progress} 进行中
														</span>
													{/if}
													{#if todoPayload && todoPayload.counts.pending > 0}
														<span class="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300">
															{todoPayload.counts.pending} 待处理
														</span>
													{/if}
													{#if todoPayload && todoPayload.counts.completed > 0}
														<span class="rounded-full bg-emerald-100 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300">
															{todoPayload.counts.completed} 已完成
														</span>
													{/if}
												</div>
											</div>

											<div class="space-y-3 px-3 py-3">
												{#each todoGroups as group}
													<div class="space-y-1.5">
														<div class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
															{group.label}
														</div>

														<div class="space-y-1.5">
															{#each group.items as todo}
																<div class="flex items-start gap-2 rounded-xl border border-gray-200/80 bg-gray-50/90 px-2.5 py-2 dark:border-gray-800 dark:bg-gray-900/70">
																	<div class="mt-0.5 shrink-0">
																		{#if todo.status === 'completed'}
																			<CheckCircle className="size-4 text-emerald-500" />
																		{:else if todo.status === 'in_progress'}
																			<span class="mt-1 inline-flex size-2 rounded-full bg-amber-500 animate-pulse"></span>
																		{:else}
																			<span class="mt-1 inline-flex size-2 rounded-full bg-gray-400 dark:bg-gray-500"></span>
																		{/if}
																	</div>

																	<div class="min-w-0 flex-1">
																		<div class="text-sm leading-6 text-gray-800 dark:text-gray-100 {todo.status === 'completed' ? 'line-through text-gray-500 dark:text-gray-400' : ''}">
																			{todo.content}
																		</div>
																	</div>
																</div>
															{/each}
														</div>
													</div>
												{/each}
											</div>
										</div>

										{#if isTerminal && showTodoResultMessage}
											<div>
												<div class="text-[10px] uppercase tracking-wider font-medium text-gray-400 dark:text-gray-500 mb-1.5 px-1">
													{status === 'success' ? '更新说明' : '错误信息'}
												</div>
												<div class="rounded-xl border border-gray-200/80 bg-white/90 px-3 py-2 text-xs whitespace-pre-wrap break-words text-gray-700 dark:border-gray-800 dark:bg-gray-950/70 dark:text-gray-200">
													{todoResultMessage}
												</div>
											</div>
										{/if}
									{:else}
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
		{#if imageFiles.length > 0}
			{#each imageFiles as file, idx}
				{#if file.url}
					<Image id={`${componentId}-tool-call-result-${idx}`} src={file.url} alt={file.name} />
				{/if}
				{/each}
		{/if}
	{/if}
</div>
