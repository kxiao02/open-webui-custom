<script>
	import { getContext } from 'svelte';
	const i18n = /** @type {import('$lib/i18n').I18nStore} */ (getContext('i18n'));

	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import StatusItem from './StatusHistory/StatusItem.svelte';

	export let statusHistory = [];
	export let expand = true;

	let showHistory = true;
	let history = [];
	let displayHistory = [];
	let latestStatus = null;
	let latestStatusSummary = '';
	let sectionTitle = '';
	let showSearchIcon = false;
	let previousExpand = expand;

	const RETRIEVAL_STATUS_ACTIONS = new Set([
		'knowledge_search',
		'queries_generated',
		'sources_retrieved'
	]);
	const SEARCH_STATUS_ACTIONS = new Set(['web_search', 'web_search_queries_generated']);

	const getStatusSummary = (status) => {
		const description = typeof status?.description === 'string' ? status.description.trim() : '';
		if (description) return description;

		if (status?.action === 'knowledge_search') {
			return status?.query ? `正在检索知识库：${status.query}` : '正在检索知识库';
		}
		if (status?.action === 'queries_generated' || status?.action === 'web_search_queries_generated') {
			return '正在生成检索查询';
		}
		if (status?.action === 'sources_retrieved') {
			if (status?.count === 0) return '未检索到来源';
			if (status?.count === 1) return '已检索 1 个来源';
			if (typeof status?.count === 'number') return `已检索 ${status.count} 个来源`;
			return '已检索来源';
		}
		if (status?.action === 'web_search') {
			return '正在搜索网页';
		}

		return status?.action ? '正在处理' : '';
	};

	const getSectionTitle = (items) => {
		const actions = items.map((item) => item?.status?.action).filter(Boolean);
		if (actions.some((action) => RETRIEVAL_STATUS_ACTIONS.has(action))) {
			return '资料检索';
		}
		if (actions.some((action) => SEARCH_STATUS_ACTIONS.has(action))) {
			return '搜索进度';
		}
		return '处理进度';
	};

	const shouldShowSearchIcon = (items) => {
		const actions = items.map((item) => item?.status?.action).filter(Boolean);
		return actions.some(
			(action) => RETRIEVAL_STATUS_ACTIONS.has(action) || SEARCH_STATUS_ACTIONS.has(action)
		);
	};

	$: if (
		statusHistory.length !== history.length ||
		JSON.stringify(statusHistory) !== JSON.stringify(history)
	) {
		history = statusHistory ?? [];
	}

	$: displayHistory = (history ?? [])
		.map((status, index) => ({
			status,
			done: index < (history?.length ?? 0) - 1 || Boolean(status?.done)
		}))
		.filter((item) => item.status?.hidden !== true)
		.filter((item, _index, items) => {
			const hasSpecificStatus = items.some((entry) => entry.status?.action !== 'chat');
			return !hasSpecificStatus || item.status?.action !== 'chat';
		});
	$: latestStatus = displayHistory?.length ? displayHistory.at(-1)?.status : null;
	$: latestStatusSummary = getStatusSummary(latestStatus);
	$: sectionTitle = getSectionTitle(displayHistory ?? []);
	$: showSearchIcon = shouldShowSearchIcon(displayHistory ?? []);
	$: if (expand !== previousExpand) {
		showHistory = !!expand;
		previousExpand = expand;
	}
</script>

{#if displayHistory && displayHistory.length > 0}
	<div class="mb-2 w-full overflow-hidden rounded-xl border border-gray-200/90 bg-gray-50/80 dark:border-gray-800 dark:bg-gray-900/70">
		<button
			type="button"
			class="flex w-full items-center justify-between gap-2 border-b border-gray-200/80 px-3 py-2 text-left dark:border-gray-800"
			on:click={() => {
				showHistory = !showHistory;
			}}
		>
			<div class="min-w-0 flex items-center gap-2.5">
				{#if showSearchIcon}
					<div
						class="flex size-7 shrink-0 items-center justify-center rounded-full bg-gray-200/70 text-gray-600 dark:bg-gray-800 dark:text-gray-200"
					>
						<Search className="size-3.5" strokeWidth="2.5" />
					</div>
				{/if}
				<div class="min-w-0">
					<div
						class="text-[11px] font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-300"
					>
						{sectionTitle}
					</div>
					{#if latestStatusSummary}
						<div class="mt-0.5 line-clamp-1 text-xs text-gray-500 dark:text-gray-400">
							{latestStatusSummary}
						</div>
					{/if}
				</div>
			</div>

			<ChevronDown
				className={`size-3.5 shrink-0 text-gray-500 transition-transform ${showHistory
					? 'rotate-180'
					: ''}`}
			/>
		</button>

		{#if showHistory}
			<div class="px-2 py-2">
				{#if displayHistory.length > 1}
					{#each displayHistory as item, idx}
						<div class="mb-1 flex items-stretch gap-2">
							<div>
								<div class="mb-1.5 px-1 pt-3">
									<span class="relative flex size-1.5 items-center justify-center rounded-full">
										<span class="relative inline-flex size-1.5 rounded-full bg-gray-500 dark:bg-gray-400"></span>
									</span>
								</div>
								{#if idx !== displayHistory.length - 1}
									<div class="ml-[6.5px] h-[calc(100%-14px)] w-[0.5px] bg-gray-300 dark:bg-gray-700" />
								{/if}
							</div>

							<StatusItem status={item.status} done={item.done} />
						</div>
					{/each}
				{:else}
					<div class="flex items-stretch gap-2">
						<div class="mb-1.5 px-1 pt-3">
							<span class="relative flex size-1.5 items-center justify-center rounded-full">
								<span class="relative inline-flex size-1.5 rounded-full bg-gray-500 dark:bg-gray-400"></span>
							</span>
						</div>
						<StatusItem
							status={displayHistory[0]?.status ?? latestStatus}
							done={Boolean(displayHistory[0]?.done ?? latestStatus?.done)}
						/>
					</div>
				{/if}
			</div>
		{/if}
	</div>
{/if}
