<script lang="ts">
	import DOMPurify from 'dompurify';
	import { getContext } from 'svelte';
	import Modal from '$lib/components/common/Modal.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import FullHeightIframe from '$lib/components/common/FullHeightIframe.svelte';
	import Image from '$lib/components/common/Image.svelte';
	import Markdown from '$lib/components/chat/Messages/Markdown.svelte';
	import { getKnowflowFrontendConfig } from '$lib/apis/knowflow';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import { config, settings } from '$lib/stores';

	import XMark from '$lib/components/icons/XMark.svelte';
	import Textarea from '$lib/components/common/Textarea.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	const CONTENT_PREVIEW_LIMIT = 10000;
	const LEGACY_INLINE_IMAGE_RE = /<img\b[^>]*\bsrc=(['"])(.*?)\1[^>]*>/gi;
	const LEGACY_INLINE_TABLE_RE = /<table\b[\s\S]*?<\/table>/gi;
	let expandedDocs: Set<number> = new Set();

	type CitationSourceMeta = {
		title?: string | null;
		name?: string | null;
		url?: string | null;
	};

	type CitationDocumentMeta = {
		url?: string | null;
		embed_url?: string | null;
		html_content?: string | null;
		html?: boolean;
		file_id?: string | null;
		page?: number;
		name?: string | null;
		parameters?: unknown;
	};

	type CitationDocumentEntry = {
		source?: CitationSourceMeta | null;
		document?: string | null;
		metadata?: CitationDocumentMeta | null;
		distance?: number;
	};

	export let show = false;
	export let citation;
	export let showPercentage = false;
	export let showRelevance = true;

	let mergedDocuments: CitationDocumentEntry[] = [];

	function calculatePercentage(distance: number) {
		if (typeof distance !== 'number') return null;
		if (distance < 0) return 0;
		if (distance > 1) return 100;
		return Math.round(distance * 10000) / 100;
	}

	function getRelevanceColor(percentage: number) {
		if (percentage >= 80)
			return 'bg-green-200 dark:bg-green-800 text-green-800 dark:text-green-200';
		if (percentage >= 60)
			return 'bg-yellow-200 dark:bg-yellow-800 text-yellow-800 dark:text-yellow-200';
		if (percentage >= 40)
			return 'bg-orange-200 dark:bg-orange-800 text-orange-800 dark:text-orange-200';
		return 'bg-red-200 dark:bg-red-800 text-red-800 dark:text-red-200';
	}

	$: if (citation) {
		expandedDocs = new Set();
		mergedDocuments = citation.document?.map((c, i) => {
			return {
				source: citation.source,
				document: c,
				metadata: citation.metadata?.[i],
				distance: citation.distances?.[i]
			};
		});
		if (mergedDocuments.every((doc) => doc.distance !== undefined)) {
			mergedDocuments = mergedDocuments.sort(
				(a, b) => (b.distance ?? Infinity) - (a.distance ?? Infinity)
			);
		}
	}

	const decodeString = (str: string) => {
		try {
			return decodeURIComponent(str);
		} catch {
			return str;
		}
	};

	const getCitationHeading = (source: CitationSourceMeta | null | undefined) => {
		return decodeString(source?.title ?? source?.name ?? '');
	};

	const normalizeFileRef = (value: unknown): string | null => {
		if (typeof value !== 'string') {
			return null;
		}
		const normalized = value.trim();
		if (normalized === '') {
			return null;
		}
		const lowered = normalized.toLowerCase();
		if (lowered === 'null' || lowered === 'undefined') {
			return null;
		}
		return normalized;
	};

	const isImageRef = (value: string | null): boolean => {
		if (!value) return false;
		const normalized = value.trim().toLowerCase();
		if (normalized.startsWith('data:image/')) {
			return true;
		}
		try {
			const parsed = new URL(normalized, 'http://localhost');
			return /\.(png|jpe?g|gif|webp|bmp|svg|tiff?|avif)(?:$|[?#])/i.test(parsed.pathname);
		} catch {
			return /\.(png|jpe?g|gif|webp|bmp|svg|tiff?|avif)(?:$|[?#])/i.test(normalized);
		}
	};

	const dedupeStrings = (values: Array<string | null | undefined>): string[] => {
		const seen = new Set<string>();
		const items: string[] = [];

		for (const value of values) {
			const normalized = normalizeFileRef(value);
			if (!normalized || seen.has(normalized)) {
				continue;
			}

			seen.add(normalized);
			items.push(normalized);
		}

		return items;
	};

	const resolveKnowflowUrl = (value: string | null): string | null => {
		const normalized = normalizeFileRef(value);
		if (!normalized) {
			return null;
		}

		const lowered = normalized.toLowerCase();
		if (
			lowered.startsWith('data:') ||
			lowered.startsWith('blob:') ||
			lowered.startsWith('http://') ||
			lowered.startsWith('https://')
		) {
			return normalized;
		}

		const siteUrl = getKnowflowFrontendConfig($config).siteUrl?.trim();
		if (!siteUrl) {
			return normalized;
		}

		try {
			return new URL(normalized, `${siteUrl.replace(/\/+$/, '')}/`).toString();
		} catch {
			return normalized;
		}
	};

	const resolveKnowflowHtml = (value: string | null): string | null => {
		const html = normalizeFileRef(value);
		if (!html || !html.includes('<')) {
			return html;
		}

		return html.replace(
			/\b(src|href)\s*=\s*(['"])(.+?)\2/gi,
			(_match, attr, quote, rawUrl) => {
				const resolved = resolveKnowflowUrl(rawUrl);
				return resolved ? `${attr}=${quote}${resolved}${quote}` : `${attr}=${quote}${rawUrl}${quote}`;
			}
		);
	};

	const getDocumentPrimaryUrl = (doc: CitationDocumentEntry | null | undefined): string | null =>
		resolveKnowflowUrl(doc?.metadata?.url ?? null) ??
		resolveKnowflowUrl(doc?.metadata?.embed_url ?? null) ??
		resolveKnowflowUrl(doc?.source?.url ?? null);

	const getDocumentHtml = (doc: CitationDocumentEntry | null | undefined): string | null => {
		const metadataHtml = resolveKnowflowHtml(doc?.metadata?.html_content ?? null);
		if (metadataHtml) {
			return metadataHtml;
		}
		if (doc?.metadata?.html === true) {
			return resolveKnowflowHtml(doc?.document ?? null);
		}
		return null;
	};

	const getDocumentImageUrl = (doc: CitationDocumentEntry | null | undefined): string | null => {
		const primaryUrl = getDocumentPrimaryUrl(doc);
		return isImageRef(primaryUrl) ? primaryUrl : null;
	};

	const getLegacyInlineVisuals = (
		doc: CitationDocumentEntry | null | undefined
	): {
		content: string;
		images: string[];
		tables: string[];
	} => {
		const rawValue = normalizeFileRef(doc?.document) ?? '';
		const rawContent = rawValue.trim().replace(/\n\n+/g, '\n\n');

		if (!rawContent || doc?.metadata?.html === true) {
			return {
				content: rawContent,
				images: [],
				tables: []
			};
		}

		const images = dedupeStrings(
			Array.from(rawContent.matchAll(LEGACY_INLINE_IMAGE_RE), (match) =>
				resolveKnowflowUrl(match[2]) ?? match[2]
			)
		);
		const tables = dedupeStrings(
			Array.from(rawContent.matchAll(LEGACY_INLINE_TABLE_RE), (match) =>
				DOMPurify.sanitize(resolveKnowflowHtml(match[0]) ?? match[0])
			)
		);

		if (images.length === 0 && tables.length === 0) {
			return {
				content: rawContent,
				images: [],
				tables: []
			};
		}

		const content = rawContent
			.replace(LEGACY_INLINE_IMAGE_RE, '')
			.replace(LEGACY_INLINE_TABLE_RE, '')
			.trim()
			.replace(/\n\n+/g, '\n\n');

		return {
			content,
			images,
			tables
		};
	};

	const getTextFragmentUrl = (doc: CitationDocumentEntry | null | undefined): string | null => {
		const { metadata, source, document: content } = doc ?? {};
		const { file_id, page } = metadata ?? {};
		const sourceUrl = getDocumentPrimaryUrl(doc) ?? source?.url;
		const fileRef = normalizeFileRef(file_id);
		if (sourceUrl && isImageRef(sourceUrl)) return sourceUrl;

		const baseUrl = fileRef
			? `${WEBUI_API_BASE_URL}/files/${fileRef}/content${page !== undefined ? `#page=${page + 1}` : ''}`
			: sourceUrl?.includes('http')
				? sourceUrl
				: null;

		if (!baseUrl || !content) return baseUrl;

		// Extract first and last words for text fragment, filtering out URLs and emojis
		const words = content
			.trim()
			.replace(/\s+/g, ' ')
			.split(' ')
			.filter((w: string) => w.length > 0 && !/https?:\/\/|[\u{1F300}-\u{1F9FF}]/u.test(w));

		if (words.length === 0) return baseUrl;

		const clean = (w: string) => w.replace(/[^\w]/g, '');
		const first = clean(words[0]);
		const last = clean(words.at(-1));
		const fragment = words.length === 1 ? first : `${first},${last}`;

		return fragment ? `${baseUrl}#:~:text=${fragment}` : baseUrl;
	};
</script>

<Modal size="lg" bind:show>
	<div>
		<div class=" flex justify-between dark:text-gray-300 px-4.5 pt-3 pb-2">
			<div class=" text-lg font-medium self-center flex items-center">
				{#if citation?.source?.name || citation?.source?.title}
					{@const document = mergedDocuments?.[0]}
					{@const documentFileRef = normalizeFileRef(document?.metadata?.file_id)}
					{@const documentTargetUrl = getDocumentPrimaryUrl(document)}
					{#if documentFileRef || documentTargetUrl?.includes('http') || documentTargetUrl?.startsWith('data:')}
						<Tooltip
							className="w-fit"
							content={documentTargetUrl?.includes('http') || documentTargetUrl?.startsWith('data:')
								? $i18n.t('Open link')
								: $i18n.t('Open file')}
							placement="top-start"
							tippyOptions={{ duration: [500, 0] }}
						>
							<a
								class="hover:text-gray-500 dark:hover:text-gray-100 underline grow line-clamp-1"
								href={documentFileRef
									? `${WEBUI_API_BASE_URL}/files/${documentFileRef}/content${document?.metadata?.page !== undefined ? `#page=${document.metadata.page + 1}` : ''}`
									: documentTargetUrl
										? documentTargetUrl
										: `#`}
								target="_blank"
							>
								{getCitationHeading(citation?.source)}
							</a>
						</Tooltip>
					{:else}
						{getCitationHeading(citation?.source)}
					{/if}
				{:else}
					{$i18n.t('Citation')}
				{/if}
			</div>
			<button
				class="self-center"
				aria-label={$i18n.t('Close citation modal')}
				on:click={() => {
					show = false;
				}}
			>
				<XMark className={'size-5'} />
			</button>
		</div>

		<div class="flex flex-col md:flex-row w-full px-5 pb-5 md:space-x-4">
			<div
				class="flex flex-col w-full dark:text-gray-200 overflow-y-scroll max-h-[22rem] scrollbar-thin gap-1"
			>
				{#each mergedDocuments as document, documentIdx}
					{@const documentImageUrl = getDocumentImageUrl(document)}
					{@const documentHtml = getDocumentHtml(document)}
					{@const legacyInlineVisuals = getLegacyInlineVisuals(document)}
					{@const documentImageUrls = dedupeStrings([
						documentImageUrl,
						...legacyInlineVisuals.images
					])}
					<div class="flex flex-col w-full gap-2">
						{#if document.metadata?.parameters}
							<div>
								<div class="text-sm font-medium dark:text-gray-300 mb-1">
									{$i18n.t('Parameters')}
								</div>

								<Textarea readonly value={JSON.stringify(document.metadata.parameters, null, 2)}
								></Textarea>
							</div>
						{/if}

						{#each documentImageUrls as imageUrl}
							<div class="overflow-hidden rounded-2xl border border-gray-100 dark:border-gray-800">
								<Image
									src={imageUrl}
									alt={document.metadata?.name ?? citation?.source?.name ?? $i18n.t('Content')}
									imageClassName="max-h-[28rem] w-full object-contain bg-white dark:bg-gray-950"
								/>
							</div>
						{/each}

						{#each legacyInlineVisuals.tables as tableHtml}
							<FullHeightIframe
								src={tableHtml}
								title={$i18n.t('Content')}
								iframeClassName="w-full rounded-xl border border-gray-100 bg-white dark:border-gray-800 dark:bg-gray-950"
								allowScripts={false}
								allowForms={false}
								allowSameOrigin={false}
							/>
						{/each}

						<div>
							<div
								class=" text-sm font-medium dark:text-gray-300 flex items-center gap-2 w-fit mb-1"
							>
								{#if getDocumentPrimaryUrl(document)?.includes('http') || getDocumentPrimaryUrl(document)?.startsWith('data:')}
									{@const snippetUrl = getTextFragmentUrl(document)}
									{#if snippetUrl}
										<a
											href={snippetUrl}
											target="_blank"
											class="underline hover:text-gray-500 dark:hover:text-gray-100"
											>{$i18n.t('Content')}</a
										>
									{:else}
										{$i18n.t('Content')}
									{/if}
								{:else}
									{$i18n.t('Content')}
								{/if}

								{#if showRelevance && document.distance !== undefined}
									<Tooltip
										className="w-fit"
										content={$i18n.t('Relevance')}
										placement="top-start"
										tippyOptions={{ duration: [500, 0] }}
									>
										<div class="text-sm my-1 dark:text-gray-400 flex items-center gap-2 w-fit">
											{#if showPercentage}
												{@const percentage = calculatePercentage(document.distance)}

												{#if typeof percentage === 'number'}
													<span
														class={`px-1 rounded-sm font-medium ${getRelevanceColor(percentage)}`}
													>
														{percentage.toFixed(2)}%
													</span>
												{/if}
											{:else if typeof document?.distance === 'number'}
												<span class="text-gray-500 dark:text-gray-500">
													({(document?.distance ?? 0).toFixed(4)})
												</span>
											{/if}
										</div>
									</Tooltip>
								{/if}

								{#if Number.isInteger(document?.metadata?.page)}
									<span class="text-sm text-gray-500 dark:text-gray-400">
										({$i18n.t('page')}
										{document.metadata.page + 1})
									</span>
								{/if}
							</div>

							{#if document.metadata?.html && documentHtml}
								<FullHeightIframe
									src={documentHtml}
									title={$i18n.t('Content')}
									iframeClassName="w-full rounded-xl"
									allowForms={true}
									allowSameOrigin={$settings?.iframeSandboxAllowSameOrigin ?? false}
									allowPopups={true}
								/>
							{:else}
								{@const rawContent = legacyInlineVisuals.content}
								{@const isTruncated =
									($settings?.renderMarkdownInPreviews ?? true) &&
									rawContent.length > CONTENT_PREVIEW_LIMIT &&
									!expandedDocs.has(documentIdx)}
								{#if $settings?.renderMarkdownInPreviews ?? true}
									<div class="text-sm prose dark:prose-invert max-w-full">
										<Markdown
											content={isTruncated
												? rawContent.slice(0, CONTENT_PREVIEW_LIMIT)
												: rawContent}
											id="citation-{documentIdx}"
										/>
									</div>
									{#if isTruncated}
										<button
											class="mt-1 text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition"
											on:click={() => {
												expandedDocs.add(documentIdx);
												expandedDocs = expandedDocs;
											}}
										>
											{$i18n.t('Show all ({{COUNT}} characters)', {
												COUNT: rawContent.length.toLocaleString()
											})}
										</button>
									{/if}
								{:else}
									<pre class="text-sm dark:text-gray-400 whitespace-pre-line">{rawContent}</pre>
								{/if}
							{/if}
						</div>
					</div>
				{/each}
			</div>
		</div>
	</div>
</Modal>
