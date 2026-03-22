<script lang="ts">
	import { getContext } from 'svelte';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import { showControls, showFilePreview } from '$lib/stores';

	import XMark from '$lib/components/icons/XMark.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	export let history;
	export let overlay = false;

	type PreviewFile = {
		id: string;
		name: string;
		url: string;
		size?: number;
		type?: string;
		contentType?: string;
		source: string;
		timestamp?: number;
	};

	let files: PreviewFile[] = [];
	let selectedFileId = '';
	let selectedFile: PreviewFile | null = null;

	let previewText = '';
	let previewLoading = false;
	let previewError = '';

	const normalizeFileRef = (value: unknown): string | null => {
		if (typeof value !== 'string') return null;
		const normalized = value.trim();
		if (!normalized) return null;
		const lowered = normalized.toLowerCase();
		if (lowered === 'null' || lowered === 'undefined') return null;
		return normalized;
	};

	const inferFileName = (value: string, fallback = 'generated-file') => {
		const sanitized = (value || '').split('?')[0];
		const pathPart = sanitized.split('/').pop() || sanitized;
		const windowsPathPart = pathPart.split('\\').pop() || pathPart;
		return windowsPathPart.trim() || fallback;
	};

	const normalizeOpenWebUiFileUrl = (value: string): string => {
		if (!value) return value;

		let output = value;
		if (output.includes('/v1/files/') && !output.includes('/openai/v1/files/')) {
			output = output.replace(/(^|[^/])\/v1\/files\//g, '$1/openai/v1/files/');
		}

		if (output.startsWith('http') || output.startsWith('data:') || output.startsWith('/')) {
			return output;
		}

		return `${WEBUI_API_BASE_URL}/files/${output}/content`;
	};

	const collectHistoryFiles = () => {
		const result: PreviewFile[] = [];
		const messages = Object.values(history?.messages ?? {}) as Array<Record<string, any>>;

		for (const message of messages) {
			const source = message?.role === 'assistant' ? 'assistant' : message?.role || 'message';
			const timestamp = message?.timestamp;

			if (Array.isArray(message?.files)) {
				for (const file of message.files) {
					const ref = normalizeFileRef(file?.url) ?? normalizeFileRef(file?.id);
					if (!ref) continue;

					result.push({
						id: `${source}:${ref}:${file?.name || ''}`,
						name:
							(typeof file?.name === 'string' && file.name) ||
							inferFileName(ref, 'attachment'),
						url: normalizeOpenWebUiFileUrl(ref),
						size: typeof file?.size === 'number' ? file.size : undefined,
						type: typeof file?.type === 'string' ? file.type : undefined,
						contentType:
							typeof file?.content_type === 'string' ? file.content_type : undefined,
						source,
						timestamp
					});
				}
			}

		}

		const deduped = new Map<string, PreviewFile>();
		for (const file of result) {
			const key = `${file.url}|${file.name}`;
			const existing = deduped.get(key);
			deduped.set(key, { ...existing, ...file });
		}

		return Array.from(deduped.values()).sort((a, b) => (b.timestamp ?? 0) - (a.timestamp ?? 0));
	};

	const isImageFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = file.name.split('.').pop()?.toLowerCase() ?? '';
		return (
			file.url.startsWith('data:image/') ||
			(file.contentType ?? '').startsWith('image/') ||
			['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext)
		);
	};

	const isPdfFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = file.name.split('.').pop()?.toLowerCase() ?? '';
		return (file.contentType ?? '') === 'application/pdf' || ext === 'pdf';
	};

	const isHtmlFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = file.name.split('.').pop()?.toLowerCase() ?? '';
		return (
			(file.contentType ?? '').includes('text/html') || ext === 'html' || ext === 'htm'
		);
	};

	const isTextLikeFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = file.name.split('.').pop()?.toLowerCase() ?? '';
		if ((file.contentType ?? '').startsWith('text/')) return true;
		return [
			'txt',
			'md',
			'csv',
			'json',
			'xml',
			'yaml',
			'yml',
			'py',
			'js',
			'ts',
			'sql',
			'log'
		].includes(ext);
	};

	const decodeDataUriText = (value: string): string => {
		try {
			const [meta, body] = value.split(',', 2);
			if (!body) return '';
			if (meta.includes(';base64')) {
				return atob(body);
			}
			return decodeURIComponent(body);
		} catch {
			return '';
		}
	};

	const loadPreviewText = async () => {
		if (!selectedFile || !isTextLikeFile(selectedFile)) {
			previewText = '';
			previewError = '';
			previewLoading = false;
			return;
		}

		if (selectedFile.url.startsWith('data:')) {
			previewText = decodeDataUriText(selectedFile.url);
			previewError = previewText ? '' : 'Failed to decode inline content.';
			previewLoading = false;
			return;
		}

		previewLoading = true;
		previewError = '';
		previewText = '';

		try {
			const response = await fetch(selectedFile.url);
			if (!response.ok) {
				throw new Error(`HTTP ${response.status}`);
			}
			previewText = await response.text();
		} catch (error) {
			console.error(error);
			previewError = 'Failed to load file preview.';
		} finally {
			previewLoading = false;
		}
	};

	$: files = collectHistoryFiles();
	$: if (files.length > 0 && !files.some((file) => file.id === selectedFileId)) {
		selectedFileId = files[0].id;
	}
	$: selectedFile = files.find((file) => file.id === selectedFileId) ?? null;
	$: if (selectedFile) {
		loadPreviewText();
	}
</script>

<div class="h-full w-full flex flex-col bg-white dark:bg-gray-850">
	<div
		class="pointer-events-auto z-20 flex justify-between items-center py-3 px-3 font-primary text-gray-900 dark:text-white"
	>
		<div class="flex min-w-0 items-center gap-2">
			<div class="text-sm font-semibold">{$i18n.t('File Preview')}</div>
			<div class="text-xs text-gray-500 dark:text-gray-400">{files.length}</div>
		</div>

		<button
			class="self-center pointer-events-auto p-1 rounded-full bg-white dark:bg-gray-850"
			on:click={() => {
				showFilePreview.set(false);
				showControls.set(false);
			}}
		>
			<XMark className="size-3.5 text-gray-900 dark:text-white" />
		</button>
	</div>

	{#if overlay}
		<div class="absolute top-0 left-0 right-0 bottom-0 z-10"></div>
	{/if}

	<div class="min-h-0 flex-1 px-2 pb-2">
		{#if files.length === 0}
			<div
				class="h-full rounded-xl border border-gray-100 bg-gray-50/60 p-4 text-xs text-gray-500 dark:border-gray-800 dark:bg-gray-900/60 dark:text-gray-400"
			>
				{$i18n.t('No generated or attached files found in this conversation.')}
			</div>
		{:else}
			<div class="h-full min-h-0 grid grid-cols-[minmax(180px,0.38fr)_1fr] gap-2">
				<div
					class="min-h-0 overflow-y-auto rounded-xl border border-gray-100 bg-gray-50/70 p-1.5 dark:border-gray-800 dark:bg-gray-900/60"
				>
					{#each files as file}
						<button
							type="button"
							class="w-full rounded-lg px-2 py-2 text-left transition {selectedFileId === file.id
								? 'bg-white text-gray-900 shadow-sm dark:bg-gray-800 dark:text-gray-100'
								: 'text-gray-600 hover:bg-white/80 dark:text-gray-300 dark:hover:bg-gray-800/70'}"
							on:click={() => {
								selectedFileId = file.id;
							}}
						>
							<div class="line-clamp-1 text-xs font-medium">{file.name}</div>
							<div class="mt-0.5 text-[11px] opacity-75">
								{file.source}
							</div>
						</button>
					{/each}
				</div>

				<div class="min-h-0 rounded-xl border border-gray-100 bg-white dark:border-gray-800 dark:bg-gray-900">
					{#if selectedFile}
						<div class="flex h-full min-h-0 flex-col">
							<div
								class="flex items-center justify-between gap-2 border-b border-gray-100 px-3 py-2 dark:border-gray-800"
							>
								<div class="min-w-0">
									<div class="line-clamp-1 text-sm font-medium text-gray-900 dark:text-gray-100">
										{selectedFile.name}
									</div>
								</div>
								<div class="flex items-center gap-1">
									<a
										href={selectedFile.url}
										target="_blank"
										rel="noreferrer"
										class="inline-flex items-center gap-1 rounded-md border border-gray-200 px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-800"
									>
										<Download className="size-3.5" />
										{$i18n.t('Download')}
									</a>
								</div>
							</div>

							<div class="min-h-0 flex-1 overflow-auto p-2">
								{#if isImageFile(selectedFile)}
									<img
										src={selectedFile.url}
										alt={selectedFile.name}
										class="h-full max-h-full w-full object-contain rounded-lg"
									/>
								{:else if isPdfFile(selectedFile) || isHtmlFile(selectedFile)}
									<iframe
										title={selectedFile.name}
										src={selectedFile.url}
										class="h-full min-h-[320px] w-full rounded-lg border-0"
									/>
								{:else if isTextLikeFile(selectedFile)}
									{#if previewLoading}
										<div class="h-full flex items-center justify-center text-gray-500 dark:text-gray-400">
											<Spinner className="size-4 mr-2" />
											<span class="text-xs">{$i18n.t('Loading...')}</span>
										</div>
									{:else if previewError}
										<div class="text-xs text-rose-600 dark:text-rose-400">{previewError}</div>
									{:else}
										<pre class="m-0 whitespace-pre-wrap break-all rounded-lg bg-gray-50 p-3 text-xs text-gray-700 dark:bg-gray-850 dark:text-gray-200">{previewText}</pre>
									{/if}
								{:else}
									<div class="h-full flex items-center justify-center text-xs text-gray-500 dark:text-gray-400">
										{$i18n.t('Preview is not available for this file type.')}
									</div>
								{/if}
							</div>
						</div>
					{/if}
				</div>
			</div>
		{/if}
	</div>
</div>
