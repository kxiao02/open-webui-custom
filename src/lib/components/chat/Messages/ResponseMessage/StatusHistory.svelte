<script>
	import { getContext } from 'svelte';
	const i18n = getContext('i18n');

	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import StatusItem from './StatusHistory/StatusItem.svelte';

	export let statusHistory = [];
	export let expand = true;

	let showHistory = true;
	let history = [];
	let latestStatus = null;
	let previousExpand = expand;

	$: if (
		statusHistory.length !== history.length ||
		JSON.stringify(statusHistory) !== JSON.stringify(history)
	) {
		history = statusHistory ?? [];
	}

	$: latestStatus = history?.length ? history.at(-1) : null;
	$: if (expand !== previousExpand) {
		showHistory = !!expand;
		previousExpand = expand;
	}
</script>

{#if history && history.length > 0 && latestStatus?.hidden !== true}
	<div class="mb-2 w-full overflow-hidden rounded-xl border border-gray-200/90 bg-gray-50/80 dark:border-gray-800 dark:bg-gray-900/70">
		<button
			type="button"
			class="flex w-full items-center justify-between gap-2 border-b border-gray-200/80 px-3 py-2 text-left dark:border-gray-800"
			on:click={() => {
				showHistory = !showHistory;
			}}
		>
			<div class="min-w-0">
				<div class="text-[11px] font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-300">
					{$i18n.t('Process')}
				</div>
				{#if latestStatus}
					<div class="mt-0.5 line-clamp-1 text-xs text-gray-500 dark:text-gray-400">
						{latestStatus?.description ?? ''}
					</div>
				{/if}
			</div>

			<ChevronDown
				className={`size-3.5 shrink-0 text-gray-500 transition-transform ${showHistory
					? 'rotate-180'
					: ''}`}
			/>
		</button>

		{#if showHistory}
			<div class="px-2 py-2">
				{#if history.length > 1}
					{#each history as status, idx}
						<div class="mb-1 flex items-stretch gap-2">
							<div>
								<div class="mb-1.5 px-1 pt-3">
									<span class="relative flex size-1.5 items-center justify-center rounded-full">
										<span class="relative inline-flex size-1.5 rounded-full bg-gray-500 dark:bg-gray-400"></span>
									</span>
								</div>
								{#if idx !== history.length - 1}
									<div class="ml-[6.5px] h-[calc(100%-14px)] w-[0.5px] bg-gray-300 dark:bg-gray-700" />
								{/if}
							</div>

							<StatusItem {status} done={true} />
						</div>
					{/each}
				{:else}
					<div class="flex items-stretch gap-2">
						<div class="mb-1.5 px-1 pt-3">
							<span class="relative flex size-1.5 items-center justify-center rounded-full">
								<span class="relative inline-flex size-1.5 rounded-full bg-gray-500 dark:bg-gray-400"></span>
							</span>
						</div>
						<StatusItem status={latestStatus} done={true} />
					</div>
				{/if}
			</div>
		{/if}
	</div>
{/if}
