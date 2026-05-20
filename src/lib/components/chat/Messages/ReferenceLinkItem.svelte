<script lang="ts">
	import { getContext } from 'svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import ChevronRight from '$lib/components/icons/ChevronRight.svelte';
	import DocumentPage from '$lib/components/icons/DocumentPage.svelte';
	import GlobeAlt from '$lib/components/icons/GlobeAlt.svelte';
	import Note from '$lib/components/icons/Note.svelte';
	import WebSourceAvatar from './WebSourceAvatar.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	export let kind: 'knowledge' | 'note' | 'source' | 'web' = 'source';
	export let label = '';
	export let title = '';
	export let subtitle = '';
	export let url = '';
	export let index: number | null = null;
	export let loading = false;
	export let titleAttr = '';
	export let onClick: Function = () => {};

	$: resolvedLabel =
		label ||
		(kind === 'note'
			? $i18n.t('Note')
			: kind === 'web'
				? $i18n.t('Web')
				: kind === 'knowledge'
					? $i18n.t('Knowledge Base')
					: $i18n.t('Source'));

	$: resolvedTitle = title || resolvedLabel;
	$: resolvedTitleAttr =
		titleAttr || [resolvedLabel, resolvedTitle, subtitle].filter(Boolean).join(' · ');
</script>

<button
	class="w-full cursor-pointer rounded-2xl border border-gray-100 bg-white/80 px-3 py-2.5 text-left text-gray-500 transition hover:border-gray-200 hover:bg-gray-50 hover:text-gray-700 dark:border-gray-800 dark:bg-gray-900/70 dark:text-gray-400 dark:hover:border-gray-700 dark:hover:bg-gray-850 dark:hover:text-gray-200"
	type="button"
	title={resolvedTitleAttr}
	on:click|preventDefault|stopPropagation={() => {
		onClick();
	}}
>
	<div class={`flex items-center gap-3 ${loading ? 'shimmer' : ''}`}>
		<div
			class="flex size-9 shrink-0 items-center justify-center rounded-xl border border-white/70 bg-gray-100 text-gray-400 dark:border-gray-800 dark:bg-gray-850 dark:text-gray-500"
		>
			{#if loading}
				<Spinner className="size-4" />
			{:else if kind === 'web' && url}
				<WebSourceAvatar {url} title={resolvedTitle} className="size-9 rounded-xl" />
			{:else if kind === 'note'}
				<Note className="size-4" strokeWidth="1.8" />
			{:else if kind === 'web'}
				<GlobeAlt className="size-4" strokeWidth="1.8" />
			{:else}
				<DocumentPage className="size-4" strokeWidth="1.8" />
			{/if}
		</div>

		<div class="min-w-0 flex-1">
			<div class="flex items-center gap-2 text-[11px] font-medium text-gray-400 dark:text-gray-500">
				<span class="truncate">{resolvedLabel}</span>
				{#if index !== null}
					<span
						class="shrink-0 rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] font-semibold text-gray-500 dark:bg-gray-800 dark:text-gray-300"
					>
						{index}
					</span>
				{/if}
			</div>

			<div class="mt-0.5 min-w-0">
				<div class="line-clamp-1 font-semibold text-black dark:text-white">
					{resolvedTitle}
				</div>

				{#if loading}
					<div class="line-clamp-1 text-xs text-gray-400 dark:text-gray-500">...</div>
				{:else if subtitle}
					<div class="line-clamp-1 text-xs text-gray-500 dark:text-gray-400">{subtitle}</div>
				{/if}
			</div>
		</div>

		<div class="flex shrink-0 self-center text-gray-300 dark:text-gray-600">
			<ChevronRight strokeWidth="3.5" className="size-3.5" />
		</div>
	</div>
</button>
