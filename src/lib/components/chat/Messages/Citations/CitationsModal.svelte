<script lang="ts">
	import { getContext, onMount, tick } from 'svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	import Modal from '$lib/components/common/Modal.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import CitationModal from './CitationModal.svelte';
	import ReferenceLinkItem from '../ReferenceLinkItem.svelte';

	export let id = '';
	export let show = false;
	export let citations = [];
	export let showPercentage = false;
	export let showRelevance = true;

	let showCitationModal = false;
	let selectedCitation: any = null;

	export const showCitation = (citation) => {
		selectedCitation = citation;
		showCitationModal = true;
	};

	const decodeString = (str: string) => {
		try {
			return decodeURIComponent(str);
		} catch (e) {
			return str;
		}
	};

	const isHttpUrl = (value: string = '') => /^https?:\/\//i.test(value);

	const getDomain = (value: string = '') => {
		try {
			return new URL(value).hostname.replace(/^www\./, '');
		} catch {
			return value.replace(/^https?:\/\//i, '').split(/[/?#]/)[0];
		}
	};

	const getCitationKind = (citation) => {
		const sourceUrl = citation?.source?.url ?? '';
		const sourceName = citation?.source?.name ?? '';
		return isHttpUrl(sourceUrl) || isHttpUrl(sourceName) ? 'web' : 'knowledge';
	};

	const getCitationTitle = (citation) => {
		const sourceTitle = citation?.source?.title ?? citation?.metadata?.[0]?.name ?? '';
		if (sourceTitle && !isHttpUrl(sourceTitle)) {
			return decodeString(sourceTitle);
		}

		const fallback = citation?.source?.url ?? citation?.source?.name ?? citation?.id ?? 'N/A';
		return isHttpUrl(fallback) ? getDomain(fallback) : decodeString(fallback);
	};

	const getCitationSubtitle = (citation) => {
		if (getCitationKind(citation) === 'web') {
			return decodeString(citation?.source?.url ?? citation?.source?.name ?? '');
		}

		const sourceName = decodeString(citation?.source?.name ?? '');
		const title = getCitationTitle(citation);
		return sourceName && sourceName !== title ? sourceName : '';
	};
</script>

<CitationModal
	bind:show={showCitationModal}
	citation={selectedCitation}
	{showPercentage}
	{showRelevance}
/>

<Modal size="lg" bind:show>
	<div>
		<div class=" flex justify-between dark:text-gray-300 px-5 pt-4 pb-2">
			<div class=" text-lg font-medium self-center capitalize">
				{$i18n.t('Citations')}
			</div>
			<button
				class="self-center"
				on:click={() => {
					show = false;
				}}
			>
				<XMark className={'size-5'} />
			</button>
		</div>

		<div class="flex flex-col md:flex-row w-full px-6 pb-5 md:space-x-4">
			<div
				class="flex flex-col w-full dark:text-gray-200 overflow-y-scroll max-h-[22rem] scrollbar-hidden text-left text-sm gap-2"
			>
				{#each citations as citation, idx}
					<div id={`source-${id}-${idx + 1}`}>
						<ReferenceLinkItem
							kind={getCitationKind(citation)}
							label={getCitationKind(citation) === 'web' ? $i18n.t('Web') : $i18n.t('Knowledge Base')}
							title={getCitationTitle(citation)}
							subtitle={getCitationSubtitle(citation)}
							index={idx + 1}
							titleAttr={decodeString(citation?.source?.title ?? citation?.source?.name ?? '')}
							onClick={() => {
								showCitationModal = true;
								selectedCitation = citation;
							}}
						/>
					</div>
				{/each}
			</div>
		</div>
	</div>
</Modal>
