<script>
	import { onDestroy, onMount, tick, getContext } from 'svelte';
	const i18n = /** @type {import('$lib/i18n').I18nStore} */ (getContext('i18n'));

	import Markdown from './Markdown.svelte';
	import {
		artifactCode,
		chatId,
		mobile,
		settings,
		showArtifacts,
		showControls,
		showEmbeds,
		showOverview,
		showFilePreview
	} from '$lib/stores';
	import FloatingButtons from '../ContentRenderer/FloatingButtons.svelte';
	import { createMessagesList } from '$lib/utils';

	export let id;
	export let content;

	export let history;
	export let messageId;

	export let selectedModels = [];

	export let done = true;
	export let model = null;
	/** @type {any} */
	export let sources = null;

	export let save = false;
	export let preview = false;
	export let floatingButtons = true;

	export let editCodeBlock = true;
	export let topPadding = false;

	export let onSave = (e) => {};
	export let onSourceClick = (e) => {};
	export let onTaskClick = (e) => {};
	export let onAddMessages = (e) => {};

	let contentContainerElement;
	let floatingButtonsElement;

	/** @type {string[]} */
	let sourceIds = [];
	$: getSourceIds(sources);

	/** @param {any} value */
	const toArray = (value) => {
		if (Array.isArray(value)) return value;
		if (value === undefined || value === null) return [];
		return [value];
	};

	/**
	 * @param {any} source
	 * @param {any} metadata
	 * @returns {string}
	 */
	const sourceIdentity = (source, metadata = {}) => {
		const sourceMeta = source?.source && typeof source.source === 'object' ? source.source : {};
		return String(
			metadata?.source ??
				metadata?.url ??
				sourceMeta?.id ??
				sourceMeta?.url ??
				sourceMeta?.name ??
				source?.id ??
				source?.url ??
				source?.name ??
				'N/A'
		);
	};

	/**
	 * @param {any} value
	 * @returns {any[]}
	 */
	const normalizeSourceList = (value) => {
		return toArray(value).flatMap((rawSource) => {
			if (!rawSource || typeof rawSource !== 'object') {
				return [];
			}

			const nestedSources = ['sources', 'citations', 'references'].flatMap((key) =>
				normalizeSourceList(rawSource?.[key])
			);
			if (nestedSources.length > 0) {
				return nestedSources;
			}

			const source =
				rawSource?.data && typeof rawSource.data === 'object' ? rawSource.data : rawSource;
			return source?.type === 'code_execution' ? [] : [source];
		});
	};

	/** @param {any} sources */
	const getSourceIds = (sources) => {
		const indexBySourceId = new Map();
		/** @type {string[]} */
		const result = [];
		for (const source of normalizeSourceList(sources)) {
			const documents = toArray(source.document ?? source.documents);
			const metadataItems = toArray(source.metadata ?? source.metadatas).filter(
				(metadata) => metadata && typeof metadata === 'object'
			);
			const sourceCount = Math.max(
				documents.length,
				metadataItems.length,
				sourceIdentity(source) !== 'N/A' ? 1 : 0
			);

			for (let index = 0; index < sourceCount; index++) {
				const metadata = metadataItems[index];
				const sourceId = sourceIdentity(source, metadata);
				if (indexBySourceId.has(sourceId)) {
					continue;
				}
				indexBySourceId.set(sourceId, result.length);

				if (model?.info?.meta?.capabilities?.citations == false) {
					result.push('N/A');
					continue;
				}

				if (metadata?.name) {
					result.push(metadata.name);
				} else if (sourceId.startsWith('http://') || sourceId.startsWith('https://')) {
					result.push(sourceId);
				} else {
					result.push(source?.source?.name ?? sourceId);
				}
			}
		}
		sourceIds = result;
	};

	const updateButtonPosition = (event) => {
		const buttonsContainerElement = document.getElementById(`floating-buttons-${id}`);
		if (
			!contentContainerElement?.contains(event.target) &&
			!buttonsContainerElement?.contains(event.target)
		) {
			closeFloatingButtons();
			return;
		}

		setTimeout(async () => {
			await tick();

			if (!contentContainerElement?.contains(event.target)) return;

			let selection = window.getSelection();

			if (selection.toString().trim().length > 0) {
				const range = selection.getRangeAt(0);
				const rect = range.getBoundingClientRect();

				const parentRect = contentContainerElement.getBoundingClientRect();

				// Adjust based on parent rect
				const top = rect.bottom - parentRect.top;
				const left = rect.left - parentRect.left;

				if (buttonsContainerElement) {
					buttonsContainerElement.style.display = 'block';

					// Calculate space available on the right
					const spaceOnRight = parentRect.width - left;
					let halfScreenWidth = $mobile ? window.innerWidth / 2 : window.innerWidth / 3;

					if (spaceOnRight < halfScreenWidth) {
						const right = parentRect.right - rect.right;
						buttonsContainerElement.style.right = `${right}px`;
						buttonsContainerElement.style.left = 'auto'; // Reset left
					} else {
						// Enough space, position using 'left'
						buttonsContainerElement.style.left = `${left}px`;
						buttonsContainerElement.style.right = 'auto'; // Reset right
					}
					buttonsContainerElement.style.top = `${top + 5}px`; // +5 to add some spacing
				}
			} else {
				closeFloatingButtons();
			}
		}, 0);
	};

	const closeFloatingButtons = () => {
		const buttonsContainerElement = document.getElementById(`floating-buttons-${id}`);
		if (buttonsContainerElement) {
			buttonsContainerElement.style.display = 'none';
		}

		if (floatingButtonsElement) {
			// check if closeHandler is defined

			if (typeof floatingButtonsElement?.closeHandler === 'function') {
				// call the closeHandler function
				floatingButtonsElement?.closeHandler();
			}
		}
	};

	const keydownHandler = (e) => {
		if (e.key === 'Escape') {
			closeFloatingButtons();
		}
	};

	onMount(() => {
		if (floatingButtons) {
			contentContainerElement?.addEventListener('mouseup', updateButtonPosition);
			document.addEventListener('mouseup', updateButtonPosition);
			document.addEventListener('keydown', keydownHandler);
		}
	});

	onDestroy(() => {
		if (floatingButtons) {
			contentContainerElement?.removeEventListener('mouseup', updateButtonPosition);
			document.removeEventListener('mouseup', updateButtonPosition);
			document.removeEventListener('keydown', keydownHandler);
		}
	});
</script>

<div bind:this={contentContainerElement}>
	<Markdown
		{id}
		{content}
		{model}
		{save}
		{preview}
		{done}
		{editCodeBlock}
		{topPadding}
		{sourceIds}
		{onSourceClick}
		{onTaskClick}
		{onSave}
		onUpdate={async (token) => {
			const { lang, text: code } = token;

			if (
				($settings?.detectArtifacts ?? true) &&
				(['html', 'svg'].includes(lang) || (lang === 'xml' && code.includes('svg'))) &&
				!$mobile &&
				$chatId
			) {
				await tick();
				showFilePreview.set(false);
				showArtifacts.set(true);
				showControls.set(true);
			}
		}}
		onPreview={async (value) => {
			console.log('Preview', value);
			await artifactCode.set(value);
			await showControls.set(true);
			await showArtifacts.set(true);
			await showEmbeds.set(false);
			await showFilePreview.set(false);
		}}
	/>
</div>

{#if floatingButtons}
	<FloatingButtons
		bind:this={floatingButtonsElement}
		{id}
		{messageId}
		actions={$settings?.floatingActionButtons ?? []}
		model={(selectedModels ?? []).includes(model?.id)
			? model?.id
			: (selectedModels ?? []).length > 0
				? selectedModels.at(0)
				: (model?.id ?? null)}
		messages={createMessagesList(history, messageId)}
		onAdd={({ modelId, parentId, messages }) => {
			console.log(modelId, parentId, messages);
			onAddMessages({ modelId, parentId, messages });
			closeFloatingButtons();
		}}
	/>
{/if}
