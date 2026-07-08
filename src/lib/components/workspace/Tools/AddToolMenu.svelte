<script lang="ts">
	import { DropdownMenu } from 'bits-ui';
	import { flyAndScale } from '$lib/utils/transitions';
	import { getContext } from 'svelte';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Pencil from '$lib/components/icons/Pencil.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	export let createHandler: Function;
	export let onClose: Function = () => {};

	let show = false;
</script>

<Dropdown
	bind:show
	on:change={(e) => {
		if (e.detail === false) {
			onClose();
		}
	}}
>
	<Tooltip content={$i18n.t('Create')}>
		<slot />
	</Tooltip>

	<div slot="content">
		<DropdownMenu.Content
			class="w-full max-w-[190px] rounded-2xl px-1 py-1 border border-gray-100  dark:border-gray-800 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg"
			sideOffset={6}
			side="bottom"
			align="start"
			transition={flyAndScale}
		>
			<button
				class="flex gap-2 items-center px-3 py-1.5 text-sm cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl w-full"
				on:click={async () => {
					createHandler();
					show = false;
				}}
			>
				<div class=" self-center mr-2">
					<Pencil />
				</div>
				<div class=" self-center truncate">{$i18n.t('New Tool')}</div>
			</button>
		</DropdownMenu.Content>
	</div>
</Dropdown>
