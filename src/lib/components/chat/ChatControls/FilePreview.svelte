<script lang="ts">
	import type { WorkBook } from 'xlsx';
	import { getContext, onDestroy, tick } from 'svelte';
	import DOMPurify from 'dompurify';
	import { downloadFileBlob, readFile } from '$lib/apis/terminal';
	import {
		selectedGeneratedFilePreviewId,
		selectedTerminalId,
		settings,
		showControls,
		showFilePreview,
		terminalServers
	} from '$lib/stores';
	import {
		type GeneratedFileItem,
		collectGeneratedFilesFromHistory,
		triggerGeneratedFileDownload
	} from '$lib/utils/generated-files';
	import { initMermaid, renderMermaidDiagram } from '$lib/utils';
	import {
		isMarkdownPreviewContentType,
		isMarkdownPreviewPath,
		renderMarkdownPreviewHtml
	} from '$lib/utils/markdownPreview';

	import XMark from '$lib/components/icons/XMark.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import FullHeightIframe from '$lib/components/common/FullHeightIframe.svelte';
	import CodeBlock from '$lib/components/chat/Messages/CodeBlock.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	export let history;
	export let overlay = false;

	type PreviewFile = GeneratedFileItem;
	type SystemTerminalServer = { id?: string; url?: string; key?: string };
	type DirectTerminalServer = { url?: string; api_key?: string; key?: string; enabled?: boolean };

	let files: PreviewFile[] = [];
	let selectedFileId = '';
	let selectedFile: PreviewFile | null = null;

	let previewText = '';
	let previewDocxHtml = '';
	let previewOfficeHtml = '';
	let previewOfficeSlides: string[] = [];
	let previewLoading = false;
	let previewError = '';
	let previewObjectUrl = '';
	let previewObjectUrlKey = '';
	let previewLoadKey = '';
	let previewRequestId = 0;
	let historyFilesKey = '';
	let filesRefreshTimer: ReturnType<typeof setTimeout> | null = null;
	let initializedFiles = false;
	let lastExternalPreviewId: string | null = null;

	type ActiveTerminal = { url: string; key: string } | null;
	let systemTerminal: SystemTerminalServer | null = null;
	let directTerminal: DirectTerminalServer | null = null;
	let activeTerminal: ActiveTerminal = null;
	let excelWorkbook: WorkBook | null = null;
	let excelSheetNames: string[] = [];
	let selectedExcelSheet = '';
	let currentSlide = 0;
	let markdownEl: HTMLDivElement;
	let mermaidInstance: Awaited<ReturnType<typeof initMermaid>> | null = null;

	const getFileExt = (file: PreviewFile | null): string => {
		const candidate = (file?.name || file?.path || file?.url || '').split(/[?#]/, 1)[0];
		return candidate.split('.').pop()?.toLowerCase() ?? '';
	};

	const isImageFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = getFileExt(file);
		return (
			(file.url ?? '').startsWith('data:image/') ||
			file.isImage === true ||
			(file.contentType ?? '').startsWith('image/') ||
			['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext)
		);
	};

	const isPdfFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = getFileExt(file);
		return (file.contentType ?? '') === 'application/pdf' || ext === 'pdf';
	};

	const isHtmlFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = getFileExt(file);
		return (file.contentType ?? '').includes('text/html') || ext === 'html' || ext === 'htm';
	};

	const isMarkdownFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		return (
			isMarkdownPreviewPath(file.name || file.path || file.url || null) ||
			isMarkdownPreviewContentType(file.contentType)
		);
	};

	const isTextLikeFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = getFileExt(file);
		if ((file.contentType ?? '').startsWith('text/')) return true;
		if (isMarkdownFile(file)) return true;
		return [
			'txt',
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

	const isPythonFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = getFileExt(file);
		return (
			ext === 'py' || ext === 'pyw' || (file.contentType ?? '').toLowerCase().includes('python')
		);
	};

	const getFileSourceLabel = (file: PreviewFile): string => {
		if (file.downloadMode === 'terminal') return '终端';
		if (file.source === 'tool') return '工具';
		if (file.source === 'assistant') return '回复';
		return '文件';
	};

	const isDocxFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = getFileExt(file);
		return (
			(file.contentType ?? '') ===
				'application/vnd.openxmlformats-officedocument.wordprocessingml.document' || ext === 'docx'
		);
	};

	const isXlsxFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = getFileExt(file);
		return (
			(file.contentType ?? '') ===
				'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' || ext === 'xlsx'
		);
	};

	const isPptxFile = (file: PreviewFile | null): boolean => {
		if (!file) return false;
		const ext = getFileExt(file);
		return (
			(file.contentType ?? '') ===
				'application/vnd.openxmlformats-officedocument.presentationml.presentation' ||
			ext === 'pptx'
		);
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

	const renderMermaidBlocks = async (el: HTMLDivElement) => {
		if (!el) return;
		const codeEls = el.querySelectorAll('code.language-mermaid');
		if (codeEls.length === 0) return;

		if (!mermaidInstance) {
			mermaidInstance = await initMermaid();
		}

		for (const codeEl of codeEls) {
			const pre = codeEl.parentElement;
			if (!pre || pre.tagName !== 'PRE' || pre.dataset.mermaidRendered) continue;
			pre.dataset.mermaidRendered = 'true';

			try {
				const svg = await renderMermaidDiagram(mermaidInstance, codeEl.textContent ?? '');
				if (svg) {
					const wrapper = document.createElement('div');
					wrapper.className = 'mermaid-diagram flex justify-center py-2';
					wrapper.innerHTML = svg;
					pre.replaceWith(wrapper);
				}
			} catch (error) {
				console.error('Mermaid render error:', error);
			}
		}
	};

	const revokePreviewObjectUrl = () => {
		if (previewObjectUrl) {
			URL.revokeObjectURL(previewObjectUrl);
			previewObjectUrl = '';
			previewObjectUrlKey = '';
		}
	};

	const setPreviewObjectUrl = (blob: Blob, key: string) => {
		revokePreviewObjectUrl();
		previewObjectUrl = URL.createObjectURL(blob);
		previewObjectUrlKey = key;
	};

	const getSelectedFileUrl = () =>
		previewObjectUrl && previewObjectUrlKey === previewLoadKey
			? previewObjectUrl
			: (selectedFile?.url ?? '');

	const clearOfficePreview = () => {
		previewDocxHtml = '';
		previewOfficeHtml = '';
		previewOfficeSlides = [];
		excelWorkbook = null;
		excelSheetNames = [];
		selectedExcelSheet = '';
		currentSlide = 0;
	};

	const clearPreviewContent = () => {
		previewText = '';
		previewError = '';
		previewLoading = false;
		clearOfficePreview();
		revokePreviewObjectUrl();
	};

	const getPreviewErrorMessage = (file: PreviewFile | null): string => {
		if (isDocxFile(file)) return 'Failed to load DOCX preview.';
		if (isXlsxFile(file)) return 'Failed to load XLSX preview.';
		if (isPptxFile(file)) return 'Failed to load PPTX preview.';
		return 'Failed to load file preview.';
	};

	const renderExcelSheet = async (sheet: string, requestId?: number) => {
		if (!excelWorkbook || !sheet) return;
		const { excelToTable } = await import('$lib/utils/excelToTable');
		if (requestId !== undefined && requestId !== previewRequestId) return;
		const result = await excelToTable(excelWorkbook.Sheets[sheet]);
		if (requestId !== undefined && requestId !== previewRequestId) return;
		selectedExcelSheet = sheet;
		previewOfficeHtml = result.html;
	};

	const loadOfficePreview = async (
		file: PreviewFile,
		arrayBuffer: ArrayBuffer,
		requestId: number
	) => {
		clearOfficePreview();
		revokePreviewObjectUrl();
		previewText = '';

		if (isDocxFile(file)) {
			const mammothModule = await import('mammoth');
			const mammoth = mammothModule.default ?? mammothModule;
			if (requestId !== previewRequestId) return;
			const result = await mammoth.convertToHtml({ arrayBuffer });
			if (requestId !== previewRequestId) return;
			previewDocxHtml = DOMPurify.sanitize(result.value);
			return;
		}

		if (isXlsxFile(file)) {
			const XLSX = await import('xlsx');
			if (requestId !== previewRequestId) return;
			excelWorkbook = XLSX.read(new Uint8Array(arrayBuffer), { type: 'array' });
			excelSheetNames = excelWorkbook.SheetNames;
			if (excelSheetNames.length > 0) {
				await renderExcelSheet(excelSheetNames[0], requestId);
			}
			return;
		}

		if (isPptxFile(file)) {
			const { pptxToImages } = await import('$lib/utils/pptxToHtml');
			if (requestId !== previewRequestId) return;
			const result = await pptxToImages(arrayBuffer);
			if (requestId !== previewRequestId) return;
			previewOfficeSlides = result.images;
			currentSlide = 0;
		}
	};

	const clearFilesRefreshTimer = () => {
		if (!filesRefreshTimer) return;
		clearTimeout(filesRefreshTimer);
		filesRefreshTimer = null;
	};

	const buildHistoryFilesKey = (historyData: unknown): string => {
		if (!historyData || typeof historyData !== 'object') return '';
		const messages = Object.values(
			(historyData as { messages?: Record<string, Record<string, unknown>> }).messages ?? {}
		) as Array<Record<string, unknown>>;

		return messages
			.map((message) => {
				const content = typeof message.content === 'string' ? message.content : '';
				const messageFiles = Array.isArray(message.files)
					? (message.files as Array<Record<string, unknown>>)
					: [];
				const filesKey = messageFiles
					.map((file) =>
						[
							file?.id ?? '',
							file?.url ?? '',
							file?.path ?? '',
							file?.name ?? '',
							file?.filename ?? '',
							file?.fileName ?? '',
							file?.size ?? ''
						].join(':')
					)
					.join('|');

				return [
					message.id ?? '',
					message.timestamp ?? '',
					messageFiles.length,
					filesKey,
					content.includes('type="tool_calls"') ? content.length : 0
				].join(':');
			})
			.join('||');
	};

	const refreshFiles = () => {
		files = collectGeneratedFilesFromHistory(history);
	};

	const scheduleFilesRefresh = (immediate = false) => {
		if (immediate) {
			clearFilesRefreshTimer();
			refreshFiles();
			return;
		}

		if (filesRefreshTimer) return;
		filesRefreshTimer = setTimeout(() => {
			filesRefreshTimer = null;
			refreshFiles();
		}, 120);
	};

	const buildPreviewLoadKey = (file: PreviewFile | null, terminal: ActiveTerminal): string => {
		if (!file) return '';
		return [file.id, file.downloadMode, file.url ?? '', file.path ?? '', terminal?.url ?? ''].join(
			'|'
		);
	};

	const downloadSelectedFile = async () => {
		if (!selectedFile) return;

		if (selectedFile.downloadMode === 'terminal' && selectedFile.path && activeTerminal) {
			const result = await downloadFileBlob(
				activeTerminal.url,
				activeTerminal.key,
				selectedFile.path
			);
			if (!result) {
				previewError = 'Failed to download file.';
				return;
			}

			const objectUrl = URL.createObjectURL(result.blob);
			const link = document.createElement('a');
			link.href = objectUrl;
			link.download = result.filename || selectedFile.name;
			link.click();
			URL.revokeObjectURL(objectUrl);
			return;
		}

		if (!selectedFile.url) return;
		await triggerGeneratedFileDownload(selectedFile.url, selectedFile.name);
	};

	const loadPreviewContent = async () => {
		const requestId = ++previewRequestId;
		const file = selectedFile;
		const terminal = activeTerminal;
		const targetLoadKey = buildPreviewLoadKey(file, terminal);

		if (!file) {
			clearPreviewContent();
			return;
		}

		if (file.downloadMode === 'terminal' && file.path) {
			if (!terminal) {
				clearPreviewContent();
				previewError = 'Terminal connection is unavailable.';
				return;
			}

			previewLoading = true;
			previewError = '';

			try {
				if (isTextLikeFile(file)) {
					const text = await readFile(terminal.url, terminal.key, file.path);
					if (text === null) {
						throw new Error('Failed to read terminal file');
					}
					if (requestId !== previewRequestId) return;
					revokePreviewObjectUrl();
					previewText = text;
					clearOfficePreview();
					previewError = '';
				} else {
					const result = await downloadFileBlob(terminal.url, terminal.key, file.path);
					if (!result) {
						throw new Error('Failed to download terminal file');
					}
					if (requestId !== previewRequestId) return;

					if (isDocxFile(file) || isXlsxFile(file) || isPptxFile(file)) {
						const arrayBuffer = await result.blob.arrayBuffer();
						if (requestId !== previewRequestId) return;
						await loadOfficePreview(file, arrayBuffer, requestId);
						if (requestId !== previewRequestId) return;
						previewError = '';
					} else {
						previewText = '';
						clearOfficePreview();
						previewError = '';
						setPreviewObjectUrl(result.blob, targetLoadKey);
					}
				}
			} catch (error) {
				console.error(error);
				clearPreviewContent();
				previewError = getPreviewErrorMessage(file);
			} finally {
				if (requestId === previewRequestId) {
					previewLoading = false;
				}
			}

			return;
		}

		if (isTextLikeFile(file) && (file.url ?? '').startsWith('data:')) {
			revokePreviewObjectUrl();
			previewText = decodeDataUriText(file.url ?? '');
			clearOfficePreview();
			previewError = previewText ? '' : 'Failed to decode inline content.';
			previewLoading = false;
			return;
		}

		if (
			!isTextLikeFile(file) &&
			!isDocxFile(file) &&
			!isXlsxFile(file) &&
			!isPptxFile(file) &&
			!isImageFile(file) &&
			!isPdfFile(file) &&
			!isHtmlFile(file)
		) {
			clearPreviewContent();
			return;
		}

		if (isImageFile(file) || isPdfFile(file) || isHtmlFile(file)) {
			revokePreviewObjectUrl();
			previewText = '';
			clearOfficePreview();
			previewError = '';
			previewLoading = false;
			return;
		}

		previewLoading = true;
		previewError = '';
		try {
			const response = await fetch(file.url ?? '');
			if (!response.ok) {
				throw new Error(`HTTP ${response.status}`);
			}
			if (requestId !== previewRequestId) return;

			if (isDocxFile(file) || isXlsxFile(file) || isPptxFile(file)) {
				const arrayBuffer = await response.arrayBuffer();
				if (requestId !== previewRequestId) return;
				await loadOfficePreview(file, arrayBuffer, requestId);
				if (requestId !== previewRequestId) return;
				previewError = '';
			} else {
				const text = await response.text();
				if (requestId !== previewRequestId) return;
				revokePreviewObjectUrl();
				previewText = text;
				clearOfficePreview();
				previewError = '';
			}
		} catch (error) {
			console.error(error);
			clearPreviewContent();
			previewError = getPreviewErrorMessage(file);
		} finally {
			if (requestId === previewRequestId) {
				previewLoading = false;
			}
		}
	};

	$: systemTerminal = $selectedTerminalId
		? (($terminalServers ?? []).find(
				(terminal: SystemTerminalServer) => terminal.id === $selectedTerminalId
			) ?? null)
		: (($terminalServers ?? [])[0] as SystemTerminalServer | null);
	$: directTerminal =
		($settings?.terminalServers ?? []).find(
			(server: DirectTerminalServer) => server.url === $selectedTerminalId && server.enabled
		) ?? null;
	$: activeTerminal = (
		directTerminal
			? { url: directTerminal.url, key: directTerminal.api_key }
			: systemTerminal
				? { url: systemTerminal.url, key: systemTerminal.key }
				: null
	) as ActiveTerminal;

	$: {
		const nextHistoryFilesKey = buildHistoryFilesKey(history);
		if (!initializedFiles || nextHistoryFilesKey !== historyFilesKey) {
			const immediate = !initializedFiles;
			initializedFiles = true;
			historyFilesKey = nextHistoryFilesKey;
			scheduleFilesRefresh(immediate);
		}
	}
	$: {
		const nextExternalPreviewId = $selectedGeneratedFilePreviewId;
		if (
			nextExternalPreviewId !== lastExternalPreviewId &&
			nextExternalPreviewId &&
			files.some((file) => file.id === nextExternalPreviewId)
		) {
			lastExternalPreviewId = nextExternalPreviewId;
			selectedFileId = nextExternalPreviewId;
		} else if (!nextExternalPreviewId) {
			lastExternalPreviewId = null;
		}
	}
	$: if (files.length > 0 && !files.some((file) => file.id === selectedFileId)) {
		selectedFileId =
			($selectedGeneratedFilePreviewId &&
				files.some((file) => file.id === $selectedGeneratedFilePreviewId) &&
				$selectedGeneratedFilePreviewId) ||
			files[0].id;
	}
	$: selectedFile = files.find((file) => file.id === selectedFileId) ?? null;
	$: renderedMarkdownHtml =
		isMarkdownFile(selectedFile) && previewText ? renderMarkdownPreviewHtml(previewText) : '';
	$: if (renderedMarkdownHtml && markdownEl) {
		tick().then(() => renderMermaidBlocks(markdownEl));
	}
	$: {
		const nextPreviewLoadKey = buildPreviewLoadKey(selectedFile, activeTerminal);
		if (selectedFile && nextPreviewLoadKey !== previewLoadKey) {
			previewLoadKey = nextPreviewLoadKey;
			loadPreviewContent();
		} else if (!selectedFile && previewLoadKey) {
			previewLoadKey = '';
			previewRequestId += 1;
			clearPreviewContent();
		}
	}

	onDestroy(() => {
		clearFilesRefreshTimer();
		previewRequestId += 1;
		revokePreviewObjectUrl();
	});
</script>

<div class="h-full w-full flex flex-col bg-white dark:bg-gray-850">
	<div
		class="pointer-events-auto z-20 flex justify-between items-center py-3 px-3 font-primary text-gray-900 dark:text-white"
	>
		<div class="flex min-w-0 items-center gap-2">
			<div class="text-sm font-semibold">文件预览</div>
			<div class="text-xs text-gray-500 dark:text-gray-400">{files.length}</div>
		</div>

		<button
			class="self-center pointer-events-auto p-1 rounded-full bg-white dark:bg-gray-850"
			on:click={() => {
				selectedGeneratedFilePreviewId.set(null);
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
				当前对话中没有可预览的生成文件或附件。
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
								lastExternalPreviewId = file.id;
								selectedGeneratedFilePreviewId.set(file.id);
							}}
						>
							<div class="line-clamp-1 text-xs font-medium">{file.name}</div>
							<div class="mt-0.5 text-[11px] opacity-75">
								{getFileSourceLabel(file)}
							</div>
						</button>
					{/each}
				</div>

				<div
					class="min-h-0 rounded-xl border border-gray-100 bg-white dark:border-gray-800 dark:bg-gray-900"
				>
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
									<button
										type="button"
										class="inline-flex items-center gap-1 rounded-md border border-gray-200 px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-800"
										on:click={downloadSelectedFile}
									>
										<Download className="size-3.5" />
										{$i18n.t('Download')}
									</button>
								</div>
							</div>

							<div class="min-h-0 flex-1 overflow-auto p-2">
								{#if (isImageFile(selectedFile) || isPdfFile(selectedFile) || isHtmlFile(selectedFile)) && previewLoading}
									<div
										class="h-full flex items-center justify-center text-gray-500 dark:text-gray-400"
									>
										<Spinner className="size-4 mr-2" />
										<span class="text-xs">{$i18n.t('Loading...')}</span>
									</div>
								{:else if isImageFile(selectedFile)}
									<img
										src={getSelectedFileUrl()}
										alt={selectedFile.name}
										class="h-full max-h-full w-full object-contain rounded-lg"
									/>
								{:else if isPdfFile(selectedFile)}
									<iframe
										title={selectedFile.name}
										src={getSelectedFileUrl()}
										class="h-full min-h-[320px] w-full rounded-lg border-0"
									></iframe>
								{:else if isHtmlFile(selectedFile)}
									<FullHeightIframe
										title={selectedFile.name}
										src={getSelectedFileUrl()}
										iframeClassName="h-full min-h-[320px] w-full rounded-lg border-0"
										allowForms={$settings?.iframeSandboxAllowForms ?? false}
										allowSameOrigin={true}
										allowPopups={true}
										useSandbox={false}
									/>
								{:else if isTextLikeFile(selectedFile)}
									{#if previewLoading}
										<div
											class="h-full flex items-center justify-center text-gray-500 dark:text-gray-400"
										>
											<Spinner className="size-4 mr-2" />
											<span class="text-xs">{$i18n.t('Loading...')}</span>
										</div>
									{:else if previewError}
										<div class="text-xs text-rose-600 dark:text-rose-400">{previewError}</div>
									{:else if isMarkdownFile(selectedFile)}
										<div
											bind:this={markdownEl}
											class="prose max-w-full rounded-lg bg-gray-50 p-3 text-sm text-gray-700 dark:bg-gray-850 dark:text-gray-200 dark:prose-invert"
										>
											<!-- eslint-disable-next-line svelte/no-at-html-tags -->
											{@html renderedMarkdownHtml}
										</div>
									{:else if isPythonFile(selectedFile)}
										<CodeBlock
											id="generated-file-preview-{selectedFile.id}"
											lang="python"
											code={previewText}
											token={{ text: previewText, raw: `\`\`\`python\n${previewText}\n\`\`\`` }}
											edit={false}
											run={true}
											save={false}
											className="h-full"
											editorClassName="rounded-b-2xl"
										/>
									{:else}
										<pre
											class="m-0 whitespace-pre-wrap break-all rounded-lg bg-gray-50 p-3 text-xs text-gray-700 dark:bg-gray-850 dark:text-gray-200">{previewText}</pre>
									{/if}
								{:else if isDocxFile(selectedFile)}
									{#if previewLoading}
										<div
											class="h-full flex items-center justify-center text-gray-500 dark:text-gray-400"
										>
											<Spinner className="size-4 mr-2" />
											<span class="text-xs">{$i18n.t('Loading...')}</span>
										</div>
									{:else if previewError}
										<div class="text-xs text-rose-600 dark:text-rose-400">{previewError}</div>
									{:else if previewDocxHtml}
										<div
											class="office-preview max-h-full overflow-auto rounded-lg bg-gray-50 p-3 prose text-sm text-gray-700 dark:bg-gray-850 dark:text-gray-200 dark:prose-invert max-w-full"
										>
											<!-- eslint-disable-next-line svelte/no-at-html-tags -->
											{@html previewDocxHtml}
										</div>
									{:else}
										<div
											class="h-full flex items-center justify-center text-xs text-gray-500 dark:text-gray-400"
										>
											{$i18n.t('Preview is not available for this file type.')}
										</div>
									{/if}
								{:else if isXlsxFile(selectedFile)}
									{#if previewLoading}
										<div
											class="h-full flex items-center justify-center text-gray-500 dark:text-gray-400"
										>
											<Spinner className="size-4 mr-2" />
											<span class="text-xs">{$i18n.t('Loading...')}</span>
										</div>
									{:else if previewError}
										<div class="text-xs text-rose-600 dark:text-rose-400">{previewError}</div>
									{:else if previewOfficeHtml}
										<div class="flex h-full min-h-0 flex-col">
											<div
												class="office-preview max-h-full flex-1 overflow-auto rounded-lg bg-gray-50 p-3 text-sm text-gray-700 dark:bg-gray-850 dark:text-gray-200"
											>
												<!-- eslint-disable-next-line svelte/no-at-html-tags -->
												{@html previewOfficeHtml}
											</div>
											{#if excelSheetNames.length > 1}
												<div
													class="mt-2 flex items-center gap-1 overflow-x-auto border-t border-gray-100 px-1 pt-2 dark:border-gray-800"
												>
													{#each excelSheetNames as sheet}
														<button
															type="button"
															class="shrink-0 rounded-md px-3 py-1 text-xs transition-colors {selectedExcelSheet ===
															sheet
																? 'bg-gray-200 text-gray-800 dark:bg-gray-700 dark:text-gray-200 font-medium'
																: 'text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800'}"
															on:click={() => renderExcelSheet(sheet)}
														>
															{sheet}
														</button>
													{/each}
												</div>
											{/if}
										</div>
									{:else}
										<div
											class="h-full flex items-center justify-center text-xs text-gray-500 dark:text-gray-400"
										>
											{$i18n.t('Preview is not available for this file type.')}
										</div>
									{/if}
								{:else if isPptxFile(selectedFile)}
									{#if previewLoading}
										<div
											class="h-full flex items-center justify-center text-gray-500 dark:text-gray-400"
										>
											<Spinner className="size-4 mr-2" />
											<span class="text-xs">{$i18n.t('Loading...')}</span>
										</div>
									{:else if previewError}
										<div class="text-xs text-rose-600 dark:text-rose-400">{previewError}</div>
									{:else if previewOfficeSlides.length > 0}
										<div class="flex h-full min-h-0 flex-col">
											<div
												class="flex min-h-0 flex-1 items-center justify-center overflow-auto rounded-lg bg-gray-50 p-3 dark:bg-gray-850"
											>
												<img
													src={previewOfficeSlides[currentSlide]}
													alt="Slide {currentSlide + 1}"
													class="max-h-full max-w-full rounded-md object-contain shadow-lg"
													draggable="false"
												/>
											</div>
											{#if previewOfficeSlides.length > 1}
												<div
													class="mt-2 flex items-center justify-center gap-3 text-xs text-gray-500 dark:text-gray-400"
												>
													<button
														type="button"
														class="rounded-md p-1 hover:bg-gray-100 disabled:opacity-30 dark:hover:bg-gray-800"
														disabled={currentSlide === 0}
														aria-label={$i18n.t('Previous slide')}
														on:click={() => (currentSlide = Math.max(0, currentSlide - 1))}
													>
														<svg
															xmlns="http://www.w3.org/2000/svg"
															viewBox="0 0 20 20"
															fill="currentColor"
															class="size-4"
														>
															<path
																fill-rule="evenodd"
																d="M11.78 5.22a.75.75 0 0 1 0 1.06L8.06 10l3.72 3.72a.75.75 0 1 1-1.06 1.06l-4.25-4.25a.75.75 0 0 1 0-1.06l4.25-4.25a.75.75 0 0 1 1.06 0Z"
																clip-rule="evenodd"
															/>
														</svg>
													</button>
													<span>{currentSlide + 1} / {previewOfficeSlides.length}</span>
													<button
														type="button"
														class="rounded-md p-1 hover:bg-gray-100 disabled:opacity-30 dark:hover:bg-gray-800"
														disabled={currentSlide === previewOfficeSlides.length - 1}
														aria-label={$i18n.t('Next slide')}
														on:click={() =>
															(currentSlide = Math.min(
																previewOfficeSlides.length - 1,
																currentSlide + 1
															))}
													>
														<svg
															xmlns="http://www.w3.org/2000/svg"
															viewBox="0 0 20 20"
															fill="currentColor"
															class="size-4"
														>
															<path
																fill-rule="evenodd"
																d="M8.22 5.22a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 0 1-1.06-1.06L11.94 10 8.22 6.28a.75.75 0 0 1 0-1.06Z"
																clip-rule="evenodd"
															/>
														</svg>
													</button>
												</div>
											{/if}
										</div>
									{:else}
										<div
											class="h-full flex items-center justify-center text-xs text-gray-500 dark:text-gray-400"
										>
											{$i18n.t('Preview is not available for this file type.')}
										</div>
									{/if}
								{:else}
									<div
										class="h-full flex items-center justify-center text-xs text-gray-500 dark:text-gray-400"
									>
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
