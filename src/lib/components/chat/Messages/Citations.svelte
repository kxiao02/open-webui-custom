<script lang="ts">
	import { getContext } from 'svelte';
	import { embed, showControls, showEmbeds, showFilePreview } from '$lib/stores';

	import CitationModal from './Citations/CitationModal.svelte';
	import ReferenceLinkItem from './ReferenceLinkItem.svelte';
	import WebSourceAvatar from './WebSourceAvatar.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	export let id = '';
	export let chatId = '';

	export let sources: any = [];
	export let readOnly = false;

	let citations: any[] = [];
	let showPercentage = false;
	let showRelevance = true;

	let citationModal: any = null;

	let showCitations = false;
	let showCitationModal = false;

	let selectedCitation: any = null;

	export const showSourceModal = (sourceId: string | number) => {
		let index;
		let suffix = null;
		let sourceIdentifier = null;

		if (typeof sourceId === 'string') {
			const output = sourceId.split('#');
			index = parseInt(output[0]) - 1;
			sourceIdentifier = output[0];

			if (output.length > 1) {
				suffix = output[1];
			}
		} else {
			index = sourceId - 1;
		}

		if (!Number.isInteger(index) || index < 0 || index >= citations.length) {
			if (sourceIdentifier) {
				index = citations.findIndex((citation) => {
					const citationId = citation?.id?.toString?.() ?? '';
					const sourceMetaId = citation?.source?.id?.toString?.() ?? '';
					const sourceMetaName = citation?.source?.name?.toString?.() ?? '';
					return (
						citationId === sourceIdentifier ||
						sourceMetaId === sourceIdentifier ||
						sourceMetaName === sourceIdentifier
					);
				});
			}
		}

		if (citations[index]) {
			if (citations[index]?.source?.embed_url) {
				const embedUrl = citations[index].source.embed_url;
				if (embedUrl) {
					if (readOnly) {
						// Open in new tab if readOnly
						window.open(embedUrl, '_blank');
						return;
					} else {
						showControls.set(true);
						showEmbeds.set(true);
						showFilePreview.set(false);
						embed.set({
							url: embedUrl,
							title: citations[index]?.source?.name || 'Embedded Content',
							source: citations[index],
							chatId: chatId,
							messageId: id,
							sourceId: sourceId
						});
					}
				} else {
					selectedCitation = citations[index];
					showCitationModal = true;
				}
			} else {
				selectedCitation = citations[index];
				showCitationModal = true;
			}
		}
	};

	function calculateShowRelevance(sources: any[]) {
		const distances = sources.flatMap((citation) => citation.distances ?? []);
		const inRange = distances.filter((d) => d !== undefined && d >= -1 && d <= 1).length;
		const outOfRange = distances.filter((d) => d !== undefined && (d < -1 || d > 1)).length;

		if (distances.length === 0) {
			return false;
		}

		if (
			(inRange === distances.length - 1 && outOfRange === 1) ||
			(outOfRange === distances.length - 1 && inRange === 1)
		) {
			return false;
		}

		return true;
	}

	function shouldShowPercentage(sources: any[]) {
		const distances = sources.flatMap((citation) => citation.distances ?? []);
		return distances.every((d) => d !== undefined && d >= -1 && d <= 1);
	}

	const toArray = (value: any) => {
		if (Array.isArray(value)) {
			return value;
		}
		if (value === undefined || value === null) {
			return [];
		}
		return [value];
	};

	const compactString = (value: any) => (typeof value === 'string' ? value.trim() : '');

	const getSourceIdentity = (source: any, metadata: any = {}) => {
		const sourceMeta = source?.source && typeof source.source === 'object' ? source.source : {};
		return (
			compactString(metadata?.source) ||
			compactString(metadata?.url) ||
			compactString(sourceMeta?.id) ||
			compactString(sourceMeta?.url) ||
			compactString(sourceMeta?.name) ||
			compactString(source?.id) ||
			compactString(source?.url) ||
			compactString(source?.name)
		);
	};

	const normalizeSourceEntry = (rawSource: any): any | null => {
		if (!rawSource || typeof rawSource !== 'object') {
			return null;
		}

		const source = rawSource?.data && typeof rawSource.data === 'object' ? rawSource.data : rawSource;
		if (source?.type === 'code_execution') {
			return null;
		}
		const rawSourceMeta = source?.source && typeof source.source === 'object' ? source.source : {};
		const sourceMeta = {
			...rawSourceMeta,
			...(source?.id && !rawSourceMeta.id ? { id: source.id } : {}),
			...(source?.name && !rawSourceMeta.name ? { name: source.name } : {}),
			...(source?.title && !rawSourceMeta.title ? { title: source.title } : {}),
			...(source?.url && !rawSourceMeta.url ? { url: source.url } : {}),
			...(source?.type && !rawSourceMeta.type ? { type: source.type } : {})
		};

		let documents = toArray(source?.document ?? source?.documents).filter(
			(document) => document !== undefined && document !== null
		);
		let metadata = toArray(source?.metadata ?? source?.metadatas).filter(
			(item) => item && typeof item === 'object'
		);
		const distances = toArray(source?.distances ?? source?.distance).filter(
			(distance) => distance !== undefined && distance !== null
		);

		const identity = getSourceIdentity({ ...source, source: sourceMeta }, metadata[0]);
		if (documents.length === 0 && identity) {
			documents = [compactString(source?.content) || compactString(source?.text) || ''];
		}
		if (metadata.length === 0 && identity) {
			metadata = [
				{
					source: sourceMeta?.url ?? sourceMeta?.id ?? identity,
					name: sourceMeta?.title ?? sourceMeta?.name ?? identity,
					...(sourceMeta?.url ? { url: sourceMeta.url } : {})
				}
			];
		}

		if (documents.length === 0 && metadata.length === 0 && !identity) {
			return null;
		}

		return {
			...source,
			source: sourceMeta,
			document: documents,
			metadata,
			...(distances.length > 0 ? { distances } : {})
		};
	};

	const normalizeSources = (value: any): any[] => {
		return toArray(value)
			.flatMap((rawSource: any): any[] => {
				if (!rawSource || typeof rawSource !== 'object') {
					return [];
				}

				const nestedSources: any[] = ['sources', 'citations', 'references'].flatMap((key) =>
					normalizeSources(rawSource?.[key])
				);
				if (nestedSources.length > 0) {
					return nestedSources;
				}

				const normalizedSource = normalizeSourceEntry(rawSource);
				return normalizedSource ? [normalizedSource] : [];
			})
			.filter(Boolean);
	};

	$: {
		citations = normalizeSources(sources).reduce((acc: any[], source: any) => {
			if (Object.keys(source).length === 0) {
				return acc;
			}

			source.document.forEach((document: any, index: number) => {
				const metadata = source?.metadata?.[index];
				const distance = source?.distances?.[index];

				// Within the same citation there could be multiple documents
				const id = getSourceIdentity(source, metadata) || 'N/A';
				let _source = source?.source;

				if (metadata?.name) {
					_source = { ..._source, name: metadata.name };
				}

				if (id.startsWith('http://') || id.startsWith('https://')) {
					_source = { ..._source, title: _source?.name ?? metadata?.name ?? id, name: id, url: id };
				}

				const existingSource = acc.find((item: any) => item.id === id);

				if (existingSource) {
					existingSource.document.push(document);
					existingSource.metadata.push(metadata);
					if (distance !== undefined) existingSource.distances.push(distance);
				} else {
					acc.push({
						id: id,
						source: _source,
						document: [document],
						metadata: metadata ? [metadata] : [],
						distances: distance !== undefined ? [distance] : []
					});
				}
			});

			return acc;
		}, []);

		showRelevance = calculateShowRelevance(citations);
		showPercentage = shouldShowPercentage(citations);
	}

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

	const getCitationKind = (citation: any) => {
		const sourceUrl = citation?.source?.url ?? '';
		const sourceName = citation?.source?.name ?? '';
		return isHttpUrl(sourceUrl) || isHttpUrl(sourceName) ? 'web' : 'knowledge';
	};

	const getCitationTitle = (citation: any) => {
		const sourceTitle = citation?.source?.title ?? citation?.metadata?.[0]?.name ?? '';
		if (sourceTitle && !isHttpUrl(sourceTitle)) {
			return decodeString(sourceTitle);
		}

		const fallback = citation?.source?.url ?? citation?.source?.name ?? citation?.id ?? 'N/A';
		return isHttpUrl(fallback) ? getDomain(fallback) : decodeString(fallback);
	};

	const getCitationSubtitle = (citation: any) => {
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

{#if citations.length > 0}
	{@const urlCitations = citations.filter((c) => c?.source?.name?.startsWith('http'))}
	<div class=" py-1 -mx-0.5 w-full flex gap-1 items-center flex-wrap">
		<button
			class="text-xs font-medium text-gray-600 dark:text-gray-300 px-3.5 h-8 rounded-full hover:bg-gray-100 dark:hover:bg-gray-800 transition flex items-center gap-1 border border-gray-50 dark:border-gray-850/30"
			aria-label={citations.length === 1
				? $i18n.t('Toggle 1 source')
				: $i18n.t('Toggle {{COUNT}} sources', { COUNT: citations.length })}
			aria-expanded={showCitations}
			on:click={() => {
				showCitations = !showCitations;
			}}
		>
			{#if urlCitations.length > 0}
					<div class="flex -space-x-1 items-center">
						{#each urlCitations.slice(0, 3) as citation, idx}
							<WebSourceAvatar
								url={citation?.source?.url ?? citation?.source?.name ?? ''}
								title={citation?.source?.title ?? citation?.source?.name ?? ''}
								className="size-4 rounded-full shrink-0 bg-white dark:bg-gray-900"
							/>
						{/each}
					</div>
				{/if}
			<div>
				{#if citations.length === 1}
					{$i18n.t('1 Source')}
				{:else}
					{$i18n.t('{{COUNT}} Sources', {
						COUNT: citations.length
					})}
				{/if}
			</div>
		</button>
	</div>
{/if}

{#if showCitations}
	<div class="py-1.5">
		<div class="text-xs gap-2 flex flex-col">
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
{/if}
