<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { getContext } from 'svelte';
	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	import TagItem from './TagItem.svelte';
	const dispatch = createEventDispatcher();

	type Tag = {
		name: string;
		[key: string]: any;
	};

	export let tags: Tag[] = [];
	export let disabled = false;
</script>

{#each tags as tag}
	<TagItem
		{tag}
		{disabled}
		onDelete={() => {
			dispatch('delete', tag.name);
		}}
	/>
{/each}
