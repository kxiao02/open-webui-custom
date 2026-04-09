<script lang="ts">
	import { LinkPreview } from 'bits-ui';
	import { decodeString } from '$lib/utils';
	import Source from './Source.svelte';

	export let id;
	export let token;
	export let sourceIds = [];
	export let onClick: Function = () => {};

	let containerElement;
	let openPreview = false;

	// Helper function to return only the domain from a URL
	function getDomain(url: string): string {
		const domain = url.replace('http://', '').replace('https://', '').split(/[/?#]/)[0];

		if (domain.startsWith('www.')) {
			return domain.slice(4);
		}
		return domain;
	}

	// Helper function to check if text is a URL and return the domain
	function formattedTitle(title: string): string {
		if (title.startsWith('http')) {
			return getDomain(title);
		}

		return title;
	}

	const getDisplayTitle = (title: string) => {
		if (!title) return 'N/A';
		if (title.length > 30) {
			return title.slice(0, 15) + '...' + title.slice(-10);
		}
		return title;
	};

	const getSourceTitleByIdentifier = (identifier: string | number) => {
		const rawIndex =
			typeof identifier === 'string' ? parseInt(identifier.split('#')[0], 10) : identifier;
		if (!Number.isFinite(rawIndex) || rawIndex <= 0) return 'N/A';
		return sourceIds[rawIndex - 1] ?? 'N/A';
	};
</script>

{#if sourceIds}
	{#if (token?.ids ?? []).length == 1}
		{@const id = token.ids[0]}
		{@const identifier = token.citationIdentifiers ? token.citationIdentifiers[0] : id}
		<Source id={identifier} title={getSourceTitleByIdentifier(identifier)} {onClick} />
	{:else}
		{@const firstIdentifier = token.citationIdentifiers
			? token.citationIdentifiers[0]
			: token.ids[0]}
		{@const firstTitle = getSourceTitleByIdentifier(firstIdentifier)}
		<LinkPreview.Root openDelay={0} bind:open={openPreview}>
			<LinkPreview.Trigger>
				<button
					aria-label={`${getDisplayTitle(formattedTitle(decodeString(firstTitle)))} +${(token?.ids ?? []).length - 1} more sources`}
					class="text-[10px] w-fit translate-y-[2px] px-2 py-0.5 dark:bg-white/5 dark:text-white/80 dark:hover:text-white bg-gray-50 text-black/80 hover:text-black transition rounded-xl"
					on:click={() => {
						openPreview = !openPreview;
					}}
				>
					<span class="line-clamp-1">
						{getDisplayTitle(formattedTitle(decodeString(firstTitle)))}
						<span class="dark:text-white/50 text-black/50">+{(token?.ids ?? []).length - 1}</span>
					</span>
				</button>
			</LinkPreview.Trigger>
			<LinkPreview.Content
				class="z-[999]"
				align="start"
				strategy="fixed"
				sideOffset={6}
				el={containerElement}
			>
				<div class="bg-gray-50 dark:bg-gray-850 rounded-xl p-1 cursor-pointer">
					{#each token.citationIdentifiers ?? token.ids as identifier}
						<div class="">
							<Source id={identifier} title={getSourceTitleByIdentifier(identifier)} {onClick} />
						</div>
					{/each}
				</div>
			</LinkPreview.Content>
		</LinkPreview.Root>
	{/if}
{:else}
	<span>{token.raw}</span>
{/if}
