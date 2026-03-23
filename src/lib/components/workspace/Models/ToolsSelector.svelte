<script lang="ts">
	import Checkbox from '$lib/components/common/Checkbox.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { getContext } from 'svelte';

	export let tools = [];

	let _tools: Record<string, any> = {};

	export let selectedToolIds = [];

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	let showConfirm = false;
	let pendingToolId = '';
	let pendingToolName = '';

	const setToolSelected = (toolId: string, selected: boolean) => {
		if (!_tools?.[toolId]) return;
		_tools = {
			..._tools,
			[toolId]: {
				..._tools[toolId],
				selected
			}
		};
	};

	const applySelection = (toolId: string, selected: boolean) => {
		setToolSelected(toolId, selected);
		selectedToolIds = Object.keys(_tools).filter((t) => _tools[t].selected);
	};

	$: if (tools) {
		_tools = tools.reduce((acc, tool) => {
			acc[tool.id] = {
				...tool,
				selected: selectedToolIds.includes(tool.id)
			};

			return acc;
		}, {});
	}
</script>

<div>
	<div class="flex w-full justify-between mb-1">
		<div class=" self-center text-xs font-medium text-gray-500">{$i18n.t('Tools')}</div>
	</div>

	<div class="flex flex-col mb-1">
		{#if tools.length > 0}
			<div class=" flex items-center flex-wrap">
				{#each Object.keys(_tools) as tool, toolIdx}
					<div class=" flex items-center gap-2 mr-3">
						<div class="self-center flex items-center">
							<Checkbox
								state={_tools[tool].selected ? 'checked' : 'unchecked'}
								on:change={(e) => {
									if (showConfirm) return;
									const nextSelected = e.detail === 'checked';
									if (nextSelected && !_tools[tool].selected) {
										setToolSelected(tool, true);
										pendingToolId = tool;
										pendingToolName = _tools[tool]?.name ?? tool;
										showConfirm = true;
										return;
									}
									applySelection(tool, nextSelected);
								}}
							/>
						</div>

						<Tooltip content={_tools[tool]?.meta?.description ?? _tools[tool].id}>
							<div class=" py-0.5 text-sm w-full capitalize font-medium">
								{_tools[tool].name}
							</div>
						</Tooltip>
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<div class=" text-xs dark:text-gray-700">
		{$i18n.t('To select toolkits here, add them to the "Tools" workspace first.')}
	</div>
</div>

<ConfirmDialog
	bind:show={showConfirm}
	title={$i18n.t('Add tool?')}
	confirmLabel={$i18n.t('Add')}
	on:confirm={() => {
		if (pendingToolId) {
			applySelection(pendingToolId, true);
		}
		pendingToolId = '';
		pendingToolName = '';
	}}
	on:cancel={() => {
		if (pendingToolId) {
			setToolSelected(pendingToolId, false);
		}
		pendingToolId = '';
		pendingToolName = '';
	}}
>
	<div class="text-sm text-gray-500">
		<div class="bg-yellow-500/20 text-yellow-700 dark:text-yellow-200 rounded-lg px-4 py-3">
			<div>{$i18n.t('You are about to add a tool to this model:')}</div>
			<div class="mt-1 font-medium text-yellow-900 dark:text-yellow-100">
				{pendingToolName || $i18n.t('Selected Tool')}
			</div>
		</div>
		<div class="mt-3">
			{$i18n.t('Adding tools allows the model to execute tool code during chat sessions.')}
		</div>
	</div>
</ConfirmDialog>
