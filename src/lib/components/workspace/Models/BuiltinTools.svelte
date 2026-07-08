<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import Checkbox from '$lib/components/common/Checkbox.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { marked } from 'marked';
	import { getBuiltinToolList } from '$lib/apis/tools';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');
	type BuiltinToolCatalogItem = {
		id: string;
		name: string;
		meta?: {
			description?: string;
			available?: boolean;
		};
	};

	export let builtinTools: Record<string, boolean> = {};
	let builtinCatalog: BuiltinToolCatalogItem[] = [];
	let internetSearchTool: BuiltinToolCatalogItem | null = null;

	$: {
		internetSearchTool = builtinCatalog.find((tool) => tool.id === 'web_search') ?? null;
		if (internetSearchTool && !(internetSearchTool.id in builtinTools)) {
			builtinTools = {
				...builtinTools,
				[internetSearchTool.id]: true
			};
		}
	}

	onMount(async () => {
		builtinCatalog = await getBuiltinToolList(localStorage.token);
	});
</script>

{#if internetSearchTool}
	<div>
		<div class="flex w-full justify-between mb-1">
			<div class="self-center text-xs font-medium text-gray-500">{$i18n.t('Internet Search')}</div>
		</div>
		<div class="flex items-center mt-2 flex-wrap">
			<div class="flex items-center gap-2 mr-3">
				<Checkbox
					state={builtinTools[internetSearchTool.id] !== false ? 'checked' : 'unchecked'}
					on:change={(e) => {
						builtinTools = {
							...builtinTools,
							[internetSearchTool.id]: e.detail === 'checked'
						};
					}}
				/>

				<div class="py-0.5 text-sm">
					<Tooltip
						content={marked.parse(
							`${$i18n.t(internetSearchTool.meta?.description ?? internetSearchTool.name)}${internetSearchTool.meta?.available === false ? `\n\n${$i18n.t('Currently disabled at the system level.')}` : ''}`
						)}
					>
						{$i18n.t(internetSearchTool.name)}
					</Tooltip>
				</div>
			</div>
		</div>
	</div>
{/if}
