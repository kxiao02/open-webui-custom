<script lang="ts">
	import { slide } from 'svelte/transition';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import Check from '$lib/components/icons/Check.svelte';
	import CheckCircle from '$lib/components/icons/CheckCircle.svelte';
	import Computer from '$lib/components/icons/Computer.svelte';
	import LightBulb from '$lib/components/icons/LightBulb.svelte';
	import QueueList from '$lib/components/icons/QueueList.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import WrenchSolid from '$lib/components/icons/WrenchSolid.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import { resolveToolCallStatus } from '$lib/utils/generated-files';
	import { resolveToolDisplay } from '$lib/utils/tool-display';

	type WorkflowStatus = 'running' | 'success' | 'error' | 'timeout';

	type MessageStatus = {
		done?: boolean;
		action?: string;
		description?: string;
		status?: string;
		hidden?: boolean;
		urls?: string[];
		items?: unknown[];
		query?: string;
		queries?: string[];
		count?: number;
	};

	type ProcessToolCallItem = { key: string; attrs: Record<string, string> };
	type ProcessReasoningItem = {
		key: string;
		text: string;
		done: boolean;
		duration?: number;
	};
	type DetailItem = {
		label: string;
		value: string;
		code?: boolean;
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

	type WorkflowStep = {
		key: string;
		kind: 'thinking' | 'retrieval' | 'task' | 'tool';
		title: string;
		summary: string;
		status: WorkflowStatus;
		reasoning?: ProcessReasoningItem;
		messageStatus?: MessageStatus;
		toolItem?: ProcessToolCallItem;
	};

	export let statusHistory: MessageStatus[] = [];
	export let taskItems: ProcessToolCallItem[] = [];
	export let toolItems: ProcessToolCallItem[] = [];
	export let reasoningItems: ProcessReasoningItem[] = [];
	export let done = false;
	export let finalResponseVisible = false;

	let workflowOpen = true;
	let userWorkflowToggled = false;
	let previousFinalState = false;
	let expandedStepKey = '';
	let previousActiveStepKey = '';

	const RETRIEVAL_STATUS_ACTIONS = new Set([
		'knowledge_search',
		'queries_generated',
		'sources_retrieved',
		'web_search',
		'web_search_queries_generated'
	]);

	const parseArguments = (value: string | undefined): Record<string, unknown> | null => {
		if (!value) return null;
		try {
			const parsed = JSON.parse(value);
			return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
				? (parsed as Record<string, unknown>)
				: null;
		} catch {
			return null;
		}
	};

	const parseJSONValue = (value: string | undefined): unknown => {
		if (!value) return '';
		try {
			return JSON.parse(value);
		} catch {
			return value;
		}
	};

	const formatDetailValue = (value: unknown): string => {
		if (value === undefined || value === null) return '';
		if (typeof value === 'string') {
			const trimmed = value.trim();
			if (!trimmed) return '';
			const parsed = parseJSONValue(trimmed);
			if (parsed !== trimmed) return formatDetailValue(parsed);
			return trimmed;
		}
		if (typeof value === 'number' || typeof value === 'boolean') return String(value);
		try {
			return JSON.stringify(value, null, 2);
		} catch {
			return String(value);
		}
	};

	const truncateDetailValue = (value: string, limit = 1800): string =>
		value.length > limit ? `${value.slice(0, limit).trimEnd()}\n...` : value;

	const pushDetail = (
		items: DetailItem[],
		label: string,
		value: unknown,
		options: { code?: boolean; limit?: number } = {}
	) => {
		const formatted = truncateDetailValue(formatDetailValue(value), options.limit ?? 1800);
		if (!formatted) return;
		items.push({ label, value: formatted, code: options.code });
	};

	const asRecord = (value: unknown): Record<string, unknown> | null => {
		if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
		return value as Record<string, unknown>;
	};

	const getStringField = (record: Record<string, unknown> | null, keys: string[]): string => {
		for (const key of keys) {
			const value = record?.[key];
			if (typeof value === 'string' && value.trim()) return value.trim();
		}
		return '';
	};

	const normalizeTodoStatus = (value: unknown): TodoStatus => {
		if (typeof value !== 'string') return 'pending';
		const normalized = value.trim().toLowerCase();
		if (['completed', 'complete', 'done', 'success', 'finished'].includes(normalized)) {
			return 'completed';
		}
		if (['in_progress', 'in-progress', 'running', 'active', 'current'].includes(normalized)) {
			return 'in_progress';
		}
		return 'pending';
	};

	const getTodoContent = (record: Record<string, unknown> | null): string =>
		getStringField(record, ['content', 'name', 'title', 'task', 'description']);

	const normalizeTodoText = (text: string): string =>
		text
			.replace(/[“”]/g, '"')
			.replace(/[‘’]/g, "'")
			.replace(/：/g, ':')
			.trim();

	const getTodoFieldFromTextBlock = (block: string, keys: string[]): string => {
		for (const key of keys) {
			const singleQuoted = block.match(new RegExp(`['"]${key}['"]\\s*:\\s*'([^']*)'`));
			if (singleQuoted?.[1]?.trim()) return singleQuoted[1].trim();

			const doubleQuoted = block.match(new RegExp(`['"]${key}['"]\\s*:\\s*"([^"]*)"`));
			if (doubleQuoted?.[1]?.trim()) return doubleQuoted[1].trim();
		}

		return '';
	};

	const extractTodoListText = (text: string): string => {
		const normalized = normalizeTodoText(text);
		const successMatch = normalized.match(/updated todo list to\s*(\[[\s\S]*\])/i);
		if (successMatch?.[1]) return successMatch[1];

		const kwargsMatch = normalized.match(/['"]todos['"]\s*:\s*(\[[\s\S]*?\])\s*}/i);
		if (kwargsMatch?.[1]) return kwargsMatch[1];

		return '';
	};

	const parseTodoItems = (value: unknown): TodoItem[] => {
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
	};

	const parseTodoItemsFromText = (value: unknown): TodoItem[] => {
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
	};

	const buildTodoPayload = (item: ProcessToolCallItem | undefined): TodoPayload | null => {
		if (!item) return null;

		const attrs = item.attrs ?? {};
		const parsedArgs = asRecord(parseJSONValue(attrs.arguments));
		const parsedResult = parseJSONValue(attrs.result);
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
				getStringField(resultRecord, ['title', 'name']) ||
				'当前任务清单',
			todos,
			counts
		};
	};

	const getTodoSummary = (payload: TodoPayload | null, status: WorkflowStatus): string => {
		if (!payload) return '';

		const segments = [`${payload.todos.length} 项`];
		if (payload.counts.in_progress > 0) segments.push(`${payload.counts.in_progress} 进行中`);
		if (payload.counts.pending > 0) segments.push(`${payload.counts.pending} 待处理`);
		if (payload.counts.completed > 0) segments.push(`${payload.counts.completed} 已完成`);
		if (status === 'running') segments.push('同步中');
		if (status === 'error') segments.push('更新失败');
		if (status === 'timeout') segments.push('更新超时');

		return segments.join(' · ');
	};

	const isVisibleMessageStatus = (status: MessageStatus | null | undefined): boolean =>
		Boolean(status) && status?.hidden !== true;

	const getDisplayStatusHistory = (items: MessageStatus[]): MessageStatus[] => {
		const visible = (items ?? []).filter(
			(status) => isVisibleMessageStatus(status) && status?.action !== 'chat'
		);
		const hasSpecificStatus = visible.some((status) => status?.action !== 'chat');
		return visible.filter((status) => !hasSpecificStatus || status?.action !== 'chat');
	};

	const getMessageStatusDone = (
		status: MessageStatus,
		index: number,
		items: MessageStatus[],
		currentDone = done
	): boolean => index < items.length - 1 || Boolean(status?.done) || currentDone;

	const getMessageStatusState = (
		status: MessageStatus,
		index: number,
		items: MessageStatus[],
		currentDone = done
	): WorkflowStatus => {
		const normalized = String(status?.status ?? '').toLowerCase();
		if (['error', 'failed', 'failure'].includes(normalized)) return 'error';
		if (['timeout', 'timed_out', 'timed-out'].includes(normalized)) return 'timeout';
		return getMessageStatusDone(status, index, items, currentDone) ? 'success' : 'running';
	};

	const getRetrievalTitle = (status: MessageStatus): string => {
		if (status?.action === 'web_search' || status?.action === 'web_search_queries_generated') {
			return '网络搜索';
		}
		if (status?.action === 'knowledge_search' || status?.action === 'queries_generated') {
			return '资料检索';
		}
		if (status?.action === 'sources_retrieved') {
			return '来源整理';
		}
		return '处理步骤';
	};

	const getStatusSummary = (status: MessageStatus | null | undefined): string => {
		const description = typeof status?.description === 'string' ? status.description.trim() : '';
		if (description) {
			if (description.includes('{{count}}')) {
				return description.replace('{{count}}', String((status?.urls || status?.items || []).length));
			}
			if (description.includes('{{searchQuery}}')) {
				return description.replace('{{searchQuery}}', status?.query ?? '');
			}
			return description;
		}

		if (status?.action === 'knowledge_search') {
			return status?.query ? `检索：${status.query}` : '检索知识库';
		}
		if (status?.action === 'queries_generated' || status?.action === 'web_search_queries_generated') {
			return Array.isArray(status?.queries) && status.queries.length > 0
				? `${status.queries.length} 个查询`
				: '生成检索查询';
		}
		if (status?.action === 'sources_retrieved') {
			if (status?.count === 0) return '未检索到来源';
			if (status?.count === 1) return '已检索 1 个来源';
			if (typeof status?.count === 'number') return `已检索 ${status.count} 个来源`;
			return '已检索来源';
		}
		if (status?.action === 'web_search') {
			return '搜索网页';
		}

		return status?.action ? '处理进度' : '';
	};

	const getToolName = (item: ProcessToolCallItem): string => {
		const attrs = item.attrs ?? {};
		const display = resolveToolDisplay({
			toolId: attrs.tool_id,
			toolName: attrs.tool_name,
			legacyName: attrs.name,
			parsedArgs: parseArguments(attrs.arguments)
		});
		return display.toolName || attrs.tool_name || attrs.tool_id || attrs.name || '工具调用';
	};

	const getToolStatus = (item: ProcessToolCallItem): WorkflowStatus =>
		resolveToolCallStatus(item.attrs ?? {}) as WorkflowStatus;

	const getToolSummary = (item: ProcessToolCallItem): string => {
		const status = getToolStatus(item);
		if (status === 'running') return '执行中';
		if (status === 'error') return '运行失败';
		if (status === 'timeout') return '已超时';
		return '已完成';
	};

	const getTaskSummary = (item: ProcessToolCallItem): string => {
		const status = getToolStatus(item);
		const todoSummary = getTodoSummary(buildTodoPayload(item), status);
		if (todoSummary) return todoSummary;
		if (status === 'running') return '同步任务清单';
		if (status === 'error') return '任务清单更新失败';
		if (status === 'timeout') return '任务清单更新超时';
		return '任务清单已更新';
	};

	const getTerminalAwareStatus = (status: WorkflowStatus): WorkflowStatus => status;

	const isRetrievalStatus = (status: MessageStatus): boolean =>
		RETRIEVAL_STATUS_ACTIONS.has(status?.action ?? '');

	const getSteps = (
		currentStatusHistory: MessageStatus[] = statusHistory,
		currentTaskItems: ProcessToolCallItem[] = taskItems,
		currentToolItems: ProcessToolCallItem[] = toolItems,
		currentReasoningItems: ProcessReasoningItem[] = reasoningItems,
		currentDone = done,
		currentFinalResponseVisible = finalResponseVisible
	): WorkflowStep[] => {
		const steps: WorkflowStep[] = [];

		for (const [index, item] of currentReasoningItems.entries()) {
			const itemDone = item.done || (currentDone && currentFinalResponseVisible);
			steps.push({
				key: `thinking:${item.key || index}`,
				kind: 'thinking',
				title: '思考',
				summary: itemDone
					? item.duration !== undefined
						? `用时 ${Math.round(item.duration)} 秒`
						: '已完成'
					: '推理中',
				status: itemDone ? 'success' : 'running',
				reasoning: item
			});
		}

		const displayStatuses = getDisplayStatusHistory(currentStatusHistory);
		for (const [index, status] of displayStatuses.entries()) {
			steps.push({
				key: `retrieval:${status.action ?? 'status'}:${index}:${getStatusSummary(status)}`,
				kind: isRetrievalStatus(status) ? 'retrieval' : 'tool',
				title: getRetrievalTitle(status),
				summary: getStatusSummary(status),
				status: getMessageStatusState(status, index, displayStatuses, currentDone),
				messageStatus: status
			});
		}

		for (const item of currentTaskItems) {
			const status = getTerminalAwareStatus(getToolStatus(item));
			const todoSummary = getTodoSummary(buildTodoPayload(item), status);
			steps.push({
				key: `task:${item.key}`,
				kind: 'task',
				title: '任务追踪',
				summary: todoSummary || (status === 'success' ? '任务清单已更新' : getTaskSummary(item)),
				status,
				toolItem: item
			});
		}

		for (const item of currentToolItems) {
			const status = getTerminalAwareStatus(getToolStatus(item));
			steps.push({
				key: `tool:${item.key}`,
				kind: 'tool',
				title: getToolName(item),
				summary: status === 'success' ? '已完成' : getToolSummary(item),
				status,
				toolItem: item
			});
		}

		return steps;
	};

	const getStatusLabel = (status: WorkflowStatus): string => {
		if (status === 'success') return '完成';
		if (status === 'error') return '失败';
		if (status === 'timeout') return '超时';
		return '运行中';
	};

	const getStatusClass = (status: WorkflowStatus): string => {
		if (status === 'success') return 'bg-emerald-500 dark:bg-emerald-400';
		if (status === 'error') return 'bg-rose-500 dark:bg-rose-400';
		if (status === 'timeout') return 'bg-amber-500 dark:bg-amber-400';
		return 'bg-blue-500 dark:bg-blue-400';
	};

	const getBadgeClass = (status: WorkflowStatus): string => {
		if (status === 'success') {
			return 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/70 dark:bg-emerald-950/30 dark:text-emerald-300';
		}
		if (status === 'error') {
			return 'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900/70 dark:bg-rose-950/30 dark:text-rose-300';
		}
		if (status === 'timeout') {
			return 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/70 dark:bg-amber-950/30 dark:text-amber-300';
		}
		return 'border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900/70 dark:bg-blue-950/30 dark:text-blue-300';
	};

	const getHeaderStatusLabel = (status: WorkflowStatus): string => {
		if (status === 'success') return `${steps.length} 个步骤`;
		return getStatusLabel(status);
	};

	const getStepIcon = (kind: WorkflowStep['kind']) => {
		if (kind === 'thinking') return LightBulb;
		if (kind === 'retrieval') return Search;
		if (kind === 'task') return QueueList;
		return WrenchSolid;
	};

	const getStepDetailItems = (step: WorkflowStep): DetailItem[] => {
		const items: DetailItem[] = [];

		if (step.reasoning) {
			pushDetail(items, '推理内容', step.reasoning.text, { code: true, limit: 2400 });
			pushDetail(items, '耗时', step.reasoning.duration !== undefined ? `${Math.round(step.reasoning.duration)} 秒` : '');
		}

		if (step.messageStatus) {
			pushDetail(items, '动作', step.messageStatus.action);
			pushDetail(items, '说明', getStatusSummary(step.messageStatus));
			pushDetail(items, '查询', step.messageStatus.query);
			pushDetail(items, '查询组', step.messageStatus.queries, { code: true });
			pushDetail(items, '来源数量', step.messageStatus.count);
			pushDetail(items, '链接', step.messageStatus.urls, { code: true });
			pushDetail(items, '条目', step.messageStatus.items, { code: true });
		}

		if (step.toolItem) {
			const attrs = step.toolItem.attrs ?? {};
			const args = parseJSONValue(attrs.arguments);
			pushDetail(items, '工具', getToolName(step.toolItem));
			pushDetail(items, '状态', getStatusLabel(step.status));
			pushDetail(items, '调用 ID', attrs.call_key || attrs.id);
			if (step.kind !== 'task') {
				pushDetail(items, '参数', args, { code: true, limit: 2400 });
			}
			if (step.kind !== 'task' || !buildTodoPayload(step.toolItem)) {
				pushDetail(items, '结果', attrs.result, { code: true, limit: 2400 });
			}
			pushDetail(items, '文件', attrs.files, { code: true });
			pushDetail(items, '嵌入内容', attrs.embeds, { code: true });
		}

		return items;
	};

	const toggleStepExpansion = (key: string) => {
		expandedStepKey = expandedStepKey === key ? '' : key;
	};

	$: steps = getSteps(
		statusHistory,
		taskItems,
		toolItems,
		reasoningItems,
		done,
		finalResponseVisible
	);
	$: anyRunning = steps.some((step) => step.status === 'running');
	$: activeStepIndex = Math.max(
		0,
		steps.findIndex((step) => step.status === 'running') >= 0
			? steps.findIndex((step) => step.status === 'running')
			: steps.length - 1
	);
	$: activeStep = steps[activeStepIndex] ?? null;
	$: if (workflowOpen && activeStep?.key && activeStep.key !== previousActiveStepKey) {
		expandedStepKey = activeStep.key;
		previousActiveStepKey = activeStep.key;
	}
	$: finalState = done && finalResponseVisible;
	$: if (finalState !== previousFinalState) {
		if (finalState) {
			workflowOpen = false;
			userWorkflowToggled = false;
		} else if (!userWorkflowToggled) {
			workflowOpen = true;
		}
		previousFinalState = finalState;
	}
	$: if (expandedStepKey && !steps.some((step) => step.key === expandedStepKey)) {
		expandedStepKey = '';
	}
	$: if (steps.length === 0) {
		previousActiveStepKey = '';
	}
</script>

{#if steps.length > 0}
	<div class="workflow-timeline mb-3 w-full overflow-hidden rounded-xl border border-gray-200/80 bg-white/75 text-gray-800 shadow-xs dark:border-gray-800/80 dark:bg-gray-950/45 dark:text-gray-100">
		<button
			type="button"
			class="flex w-full items-center justify-between gap-3 px-3 py-2 text-left transition hover:bg-gray-50/80 dark:hover:bg-gray-900/60"
			on:click={() => {
				userWorkflowToggled = true;
				workflowOpen = !workflowOpen;
			}}
		>
			<div class="min-w-0 flex items-center gap-2.5">
				<div
					class="flex size-7 shrink-0 items-center justify-center rounded-lg border border-gray-200 bg-gray-50 text-gray-500 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300"
				>
					<Computer className="size-3.5" strokeWidth="1.8" />
				</div>
				<div class="min-w-0">
					<div class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
						工作流
					</div>
					<div class="mt-0.5 line-clamp-1 text-xs text-gray-500 dark:text-gray-400">
						{#if activeStep}
							{activeStep.title} · {activeStep.summary}
						{:else}
							{steps.length} 个步骤
						{/if}
					</div>
				</div>
			</div>

			<div class="flex shrink-0 items-center gap-2">
				<span
					class={`inline-flex min-h-5 items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${getBadgeClass(
						anyRunning ? 'running' : activeStep?.status ?? 'success'
					)}`}
				>
					{#if !anyRunning && activeStep?.status === 'success'}
						<Check className="size-3" strokeWidth="3" />
					{/if}
					{getHeaderStatusLabel(anyRunning ? 'running' : activeStep?.status ?? 'success')}
				</span>
				<ChevronDown
					className={`size-3.5 text-gray-500 transition-transform ${workflowOpen ? 'rotate-180' : ''}`}
					strokeWidth="2.5"
				/>
			</div>
		</button>

		{#if workflowOpen}
			<div
				class="border-t border-gray-200/70 px-3 py-2 dark:border-gray-800/80"
				transition:slide={{ duration: 180 }}
			>
				<div class="relative">
					<div
						class="pointer-events-none absolute bottom-4 left-[13px] top-4 w-px bg-gray-200 dark:bg-gray-800"
					></div>

					<div class="space-y-1.5">
						{#each steps as step, index (step.key)}
							{@const StepIcon = getStepIcon(step.kind)}
							{@const isActiveStep = index === activeStepIndex}
							{@const isExpandedStep = expandedStepKey === step.key}
							<div class="relative flex gap-2.5 pl-8">
								<div
									class={`workflow-dot absolute left-[8.5px] top-3.5 size-2.5 rounded-full ring-4 ring-white dark:ring-gray-950 ${getStatusClass(
										step.status
									)} ${step.status === 'running' ? 'workflow-dot-running' : ''}`}
								></div>

								<div class="min-w-0 flex-1">
									<button
										type="button"
										aria-expanded={isExpandedStep}
										class={`workflow-step-button flex min-h-8 w-full items-center justify-between gap-3 rounded-lg px-2 py-1.5 text-left transition hover:bg-gray-50 dark:hover:bg-gray-900/70 ${
											isActiveStep
												? `bg-gray-50 text-gray-900 dark:bg-gray-900/70 dark:text-gray-100 ${step.status === 'running' ? 'workflow-step-running' : ''}`
												: 'text-gray-600 dark:text-gray-400'
										}`}
										on:click={() => toggleStepExpansion(step.key)}
									>
										<div class="min-w-0 flex items-center gap-2">
											<svelte:component
												this={StepIcon}
												className="size-3.5 shrink-0"
												strokeWidth="2"
											/>
											<div class="min-w-0">
												<div class="line-clamp-1 text-xs font-medium">{step.title}</div>
												<div class="line-clamp-1 text-[11px] text-gray-500 dark:text-gray-500">
													{step.summary}
												</div>
											</div>
										</div>

										<div class="flex shrink-0 items-center gap-1.5">
											<span
												class={`inline-flex min-h-5 items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${getBadgeClass(
													step.status
												)}`}
											>
												{#if step.status === 'success'}
													<Check className="size-3" strokeWidth="3" />
												{:else if step.status === 'error'}
													<XMark className="size-3" strokeWidth="3" />
												{:else}
													{getStatusLabel(step.status)}
												{/if}
											</span>
											<ChevronDown
												className={`size-3 text-gray-400 transition-transform ${isExpandedStep ? 'rotate-180' : ''}`}
												strokeWidth="2.5"
											/>
										</div>
									</button>

									{#if isExpandedStep}
										{@const detailItems = getStepDetailItems(step)}
										{@const todoPayload = step.kind === 'task' ? buildTodoPayload(step.toolItem) : null}
										<div
											class="mx-2 mb-1 mt-1 rounded-lg border border-gray-200/80 bg-white/80 px-3 py-2 text-xs text-gray-600 dark:border-gray-800 dark:bg-gray-950/60 dark:text-gray-300"
											transition:slide={{ duration: 170 }}
										>
											{#if todoPayload}
												<div class="mb-2 flex flex-wrap items-center justify-between gap-2">
													<div class="min-w-0">
														<div class="text-[10px] font-medium uppercase tracking-wide text-gray-400 dark:text-gray-500">
															任务清单
														</div>
														<div class="mt-0.5 line-clamp-1 text-[11px] font-medium text-gray-700 dark:text-gray-200">
															{todoPayload.title}
														</div>
													</div>

													<div class="flex shrink-0 flex-wrap gap-1">
														{#if todoPayload.counts.in_progress > 0}
															<span class="rounded-md bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
																{todoPayload.counts.in_progress} 进行中
															</span>
														{/if}
														{#if todoPayload.counts.pending > 0}
															<span class="rounded-md bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300">
																{todoPayload.counts.pending} 待处理
															</span>
														{/if}
														{#if todoPayload.counts.completed > 0}
															<span class="rounded-md bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
																{todoPayload.counts.completed} 已完成
															</span>
														{/if}
													</div>
												</div>

												<div class="mb-2 space-y-1">
													{#each todoPayload.todos as todo (todo.id)}
														<div class="flex min-w-0 items-start gap-2 rounded-md bg-gray-50/80 px-2 py-1.5 dark:bg-gray-900/70">
															<div class="mt-0.5 shrink-0">
																{#if todo.status === 'completed'}
																	<CheckCircle className="size-3.5 text-emerald-500" />
																{:else if todo.status === 'in_progress'}
																	<span class="mt-1 inline-flex size-2 rounded-full bg-amber-500 {step.status === 'running' ? 'animate-pulse' : ''}"></span>
																{:else}
																	<span class="mt-1 inline-flex size-2 rounded-full bg-gray-400 dark:bg-gray-500"></span>
																{/if}
															</div>
															<div
																class={`min-w-0 flex-1 break-words text-[11px] leading-5 ${
																	todo.status === 'completed'
																		? 'text-gray-400 line-through dark:text-gray-500'
																		: 'text-gray-700 dark:text-gray-200'
																}`}
															>
																{todo.content}
															</div>
														</div>
													{/each}
												</div>
											{/if}

											{#if detailItems.length > 0}
												<div class="space-y-2">
													{#each detailItems as item, detailIndex (`${item.label}:${detailIndex}`)}
														<div class="min-w-0">
															<div class="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-gray-400 dark:text-gray-500">
																{item.label}
															</div>
															{#if item.code}
																<pre
																	class="max-h-44 overflow-auto whitespace-pre-wrap break-words rounded-md bg-gray-50 px-2 py-1.5 text-[11px] leading-5 text-gray-600 dark:bg-gray-900/80 dark:text-gray-300"
																>{item.value}</pre>
															{:else}
																<div class="break-words text-[11px] leading-5">{item.value}</div>
															{/if}
														</div>
													{/each}
												</div>
											{:else}
												<div class="text-[11px] text-gray-400 dark:text-gray-500">
													暂无更多执行细节
												</div>
											{/if}
										</div>
									{/if}
								</div>
							</div>
						{/each}
					</div>
				</div>
			</div>
		{/if}
	</div>
{/if}

<style>
	.workflow-timeline {
		overflow-anchor: none;
	}

	.workflow-step-button {
		position: relative;
		overflow: hidden;
	}

	.workflow-step-running::after {
		content: '';
		position: absolute;
		inset: 0;
		pointer-events: none;
		background: linear-gradient(
			100deg,
			transparent 0%,
			rgba(59, 130, 246, 0.08) 34%,
			rgba(16, 185, 129, 0.1) 50%,
			rgba(59, 130, 246, 0.08) 66%,
			transparent 100%
		);
		background-size: 220% 100%;
		animation: workflow-wave 1.5s ease-in-out infinite;
	}

	.workflow-dot-running {
		animation: workflow-dot-roll 1.2s ease-in-out infinite;
	}

	.workflow-dot-running::after {
		content: '';
		position: absolute;
		inset: -6px;
		border-radius: 9999px;
		border: 1px solid currentColor;
		opacity: 0.28;
		animation: workflow-ripple 1.2s ease-out infinite;
	}

	@keyframes workflow-wave {
		0% {
			background-position: 140% 0;
		}
		100% {
			background-position: -80% 0;
		}
	}

	@keyframes workflow-dot-roll {
		0%,
		100% {
			transform: translateY(0) scale(0.92);
		}
		50% {
			transform: translateY(-1px) scale(1.08);
		}
	}

	@keyframes workflow-ripple {
		0% {
			transform: scale(0.65);
			opacity: 0.36;
		}
		100% {
			transform: scale(1.35);
			opacity: 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.workflow-step-running::after,
		.workflow-dot-running,
		.workflow-dot-running::after {
			animation: none;
		}
	}
</style>
