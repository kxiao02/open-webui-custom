<script lang="ts">
	import { decode } from 'html-entities';
	import { v4 as uuidv4 } from 'uuid';

	import { getContext } from 'svelte';
	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	import dayjs from '$lib/dayjs';
	import duration from 'dayjs/plugin/duration';
	import relativeTime from 'dayjs/plugin/relativeTime';

	dayjs.extend(duration);
	dayjs.extend(relativeTime);

	async function loadLocale(locales) {
		if (!locales || !Array.isArray(locales)) {
			return;
		}
		for (const locale of locales) {
			try {
				dayjs.locale(locale);
				break; // Stop after successfully loading the first available locale
			} catch (error) {
				console.error(`Could not load locale '${locale}':`, error);
			}
		}
	}

	// Assuming $i18n.languages is an array of language codes
	$: loadLocale($i18n.languages);

	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';

	import ChevronUp from '../icons/ChevronUp.svelte';
	import ChevronDown from '../icons/ChevronDown.svelte';
	import Spinner from './Spinner.svelte';
	import Image from './Image.svelte';
	import FullHeightIframe from './FullHeightIframe.svelte';
	import {
		settings,
		showControls,
		showOverview,
		showArtifacts,
		showEmbeds,
		showCallOverlay,
		showFilePreview
	} from '$lib/stores';
	import {
		isFileGeneratingToolId,
		normalizeOpenWebUiFileUrl as normalizeGeneratedFileUrl,
		parseToolCallPayload,
		resolveToolCallStatus
	} from '$lib/utils/generated-files';
	import { resolveToolDisplay } from '$lib/utils/tool-display';

	export let open = false;

	export let className = '';
	export let buttonClassName =
		'w-fit text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition';

	export let id = '';
	export let title = null;
	export let attributes = null;

	export let chevron = false;
	export let grow = false;
	export let centerTitle = false;

	export let disabled = false;
	export let hide = false;

	export let onChange: Function = () => {};

	$: onChange(open);

	const collapsibleId = uuidv4();

	function parseJSONString(str) {
		try {
			return parseJSONString(JSON.parse(str));
		} catch (e) {
			return str;
		}
	}

	function formatJSONString(str) {
		try {
			const parsed = parseJSONString(str);
			// If parsed is an object/array, then it's valid JSON
			if (typeof parsed === 'object') {
				return JSON.stringify(parsed, null, 2);
			} else {
				// It's a primitive value like a number, boolean, etc.
				return `${JSON.stringify(String(parsed))}`;
			}
		} catch (e) {
			// Not valid JSON, return as-is
			return str;
		}
	}

	type ToolFileItem = {
		id: string;
		name: string;
		url: string;
		type?: string;
		contentType?: string;
	};

	function normalizeFileRef(value: unknown): string {
		if (typeof value !== 'string') return '';
		const normalized = value.trim();
		if (!normalized) return '';
		const lowered = normalized.toLowerCase();
		if (lowered === 'null' || lowered === 'undefined') return '';
		return normalized;
	}

	function inferFileName(value: string, fallback = 'generated-file') {
		const sanitized = (value || '').split('?')[0];
		const pathPart = sanitized.split('/').pop() || sanitized;
		const windowsPathPart = pathPart.split('\\').pop() || pathPart;
		return windowsPathPart.trim() || fallback;
	}

	function toToolFileItem(value: any, source: string): ToolFileItem | null {
		if (typeof value === 'string') {
			const ref = normalizeFileRef(value);
			if (!ref) return null;
			const url = normalizeGeneratedFileUrl(ref);
			return {
				id: `${source}:${url}`,
				name: inferFileName(ref),
				url
			};
		}

		if (!value || typeof value !== 'object') {
			return null;
		}

		const ref =
			normalizeFileRef(value.bridge_url) ||
			normalizeFileRef(value.generated_file_url) ||
			normalizeFileRef(value.download_url) ||
			normalizeFileRef(value.downloadUrl) ||
			normalizeFileRef(value.bridge_file_id) ||
			normalizeFileRef(value.file_id) ||
			normalizeFileRef(value.fileId) ||
			(typeof value.url === 'string' &&
			value.url.trim() &&
			/^(?:https?:\/\/[^/]+)?\/?(?:api\/v1|openai\/v1|v1)\/(?:files|generated-files)\//i.test(
				value.url.trim()
			)
				? value.url.trim()
				: '') ||
			'';

		if (!ref) {
			return null;
		}

		const url = normalizeGeneratedFileUrl(ref);
		const name =
			(typeof value.name === 'string' && value.name.trim()) ||
			(typeof value.filename === 'string' && value.filename.trim()) ||
			(typeof value.fileName === 'string' && value.fileName.trim()) ||
			inferFileName(ref);

		return {
			id: `${source}:${url}:${name}`,
			name,
			url,
			type: typeof value.type === 'string' ? value.type : undefined,
			contentType: typeof value.content_type === 'string' ? value.content_type : undefined
		};
	}

	function dedupeToolFiles(items: ToolFileItem[]): ToolFileItem[] {
		const deduped = new Map<string, ToolFileItem>();
		for (const item of items) {
			if (!item) continue;
			deduped.set(`${item.url}|${item.name}`, item);
		}
		return Array.from(deduped.values());
	}

	function normalizeToolFiles(value: any): ToolFileItem[] {
		const parsed = parseJSONString(value);
		const list = Array.isArray(parsed) ? parsed : parsed ? [parsed] : [];
		return dedupeToolFiles(
			list
				.map((item) => toToolFileItem(item, 'tool_files'))
				.filter((item): item is ToolFileItem => item !== null)
		);
	}

	function collectFilesFromToolResult(value: any): ToolFileItem[] {
		const parsed = parseJSONString(value);
		if (!parsed || typeof parsed !== 'object') return [];

		const record = parsed as Record<string, any>;
		const listCandidates = [
			record.generated_files,
			record.generatedFiles,
			record.fileInfo,
			record.fileList,
			record.files
		];

		const files: ToolFileItem[] = [];
		for (const list of listCandidates) {
			if (!Array.isArray(list)) continue;
			for (const item of list) {
				const normalized = toToolFileItem(item, 'tool_result');
				if (normalized) {
					files.push(normalized);
				}
			}
		}

		return dedupeToolFiles(files);
	}

	function hasResultFileArtifacts(parsedResult: any): boolean {
		const visit = (value: any): boolean => {
			if (!value) return false;
			if (Array.isArray(value)) {
				return value.some((entry) => visit(entry));
			}
			if (typeof value !== 'object') return false;

			const record = value as Record<string, any>;
			if (record.success === false) return false;

			const listKeys = [
				'files',
				'generated_files',
				'generatedFiles',
				'fileList',
				'fileInfo',
				'outputs',
				'artifacts',
				'attachments'
			];
			for (const key of listKeys) {
				const list = record[key];
				if (Array.isArray(list) && list.length > 0) return true;
			}

			const stringKeys = [
				'path',
				'output_path',
				'target_path',
				'file_path',
				'bridge_url',
				'generated_file_url',
				'download_url',
				'downloadUrl',
				'bridge_file_id',
				'file_id',
				'fileId',
			];
			for (const key of stringKeys) {
				const value = record[key];
				if (typeof value === 'string' && value.trim()) return true;
			}

			return false;
		};

		return visit(parsedResult);
	}

	const openFilePreviewPane = () => {
		showOverview.set(false);
		showArtifacts.set(false);
		showEmbeds.set(false);
		showCallOverlay.set(false);
		showFilePreview.set(true);
		showControls.set(true);
	};

	type SearchResultItem = {
		title: string;
		url?: string;
		snippet?: string;
	};

	const TOOL_ICONS: Record<string, string> = {
		internet_search: '🔎',
		visit_webpage: '🌐',
		current_server_time: '🕒',
		math_calculator: '🧮',
		read_file: '📄',
		display_file: '📄',
		write_file: '✍️',
		replace_file_content: '✍️',
		edit_file: '✍️',
		ls: '📁',
		glob: '📁',
		grep: '🔍',
		execute: '⌨️',
		read_structured_file: '📄',
		write_structured_file: '✍️',
		gotenberg_convert: '📘',
		pdf_create_document: '📘',
		pdf_inspect_form: '📘',
		pdf_fill_form_tool: '📘',
		pdf_reformat_document: '📘',
		pdf_remove_pages: '📘',
		pdf_add_pages: '📘',
		pdf_locate_pages: '📘',
		xlsx_read_workbook: '📊',
		xlsx_validate_workbook: '📊',
		xlsx_create_workbook: '📊',
		xlsx_add_column_tool: '📊',
		xlsx_insert_row_tool: '📊',
		docx_export_document: '📄',
		pptx_export_presentation: '🖼️',
		write_todos: '🗂️'
	};

	function asRecord(value: unknown): Record<string, unknown> {
		if (value && typeof value === 'object' && !Array.isArray(value)) {
			return value as Record<string, unknown>;
		}
		return {};
	}

	function getStringArg(record: Record<string, unknown>, keys: string[]) {
		for (const key of keys) {
			const value = record?.[key];
			if (typeof value === 'string' && value.trim()) {
				return value.trim();
			}
		}
		return '';
	}

	function getToolCardMeta(
		rawToolId: string | undefined,
		rawToolName: string | undefined,
		legacyName: string | undefined,
		args: Record<string, unknown>,
		result: unknown = null
	) {
		const resolved = resolveToolDisplay({
			toolId: rawToolId,
			toolName: rawToolName,
			legacyName,
			parsedArgs: args,
			parsedResult: result
		});
		const name = resolved.toolName;
		const icon = TOOL_ICONS[resolved.toolId] || '🧩';

		if (resolved.toolId === 'internet_search') {
			const query = getStringArg(args, ['query', 'q']);
			return {
				label: name,
				icon,
				action: query ? `正在搜索：${query}` : '正在搜索网络信息',
				reason: '需要获取外部来源作为回答依据'
			};
		}

		if (resolved.toolId === 'visit_webpage') {
			const target =
				getStringArg(args, ['title', 'name', 'siteName', 'site_name']) ||
				getStringArg(args, ['url', 'href', 'link']);
			return {
				label: name,
				icon,
				action: target ? `正在打开：${target}` : '正在打开网页',
				reason: '需要读取原始页面以补充细节'
			};
		}

		if (resolved.toolId === 'read_structured_file') {
			const path = getStringArg(args, ['path', 'file_path', 'file']);
			return {
				label: name,
				icon,
				action: path ? `正在读取：${path}` : '正在读取结构化文件',
				reason: '需要先提取文件内容再进行总结或分析'
			};
		}

		if (resolved.toolId === 'write_structured_file') {
			const path = getStringArg(args, ['path', 'file_path', 'file']);
			return {
				label: name,
				icon,
				action: path ? `正在写入：${path}` : '正在写入结构化文件',
				reason: '需要把处理结果保存到目标文件'
			};
		}

		if (resolved.toolId === 'gotenberg_convert') {
			return {
				label: name,
				icon,
				action: '正在执行文档转换',
				reason: '需要将网页或文档转换为 PDF'
			};
		}

		return {
			label: name,
			icon,
			action: `正在执行：${name}`,
			reason: '正在执行一个工具步骤'
		};
	}

	function getToolStatusLabel(status: string) {
		if (status === 'success') return '已完成';
		if (status === 'timeout') return '已超时';
		if (status === 'error') return '执行失败';
		return '进行中';
	}

	function getToolStatusBadgeClass(status: string) {
		if (status === 'success') {
			return 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/70 dark:bg-emerald-900/30 dark:text-emerald-300';
		}
		if (status === 'timeout') {
			return 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/70 dark:bg-amber-900/30 dark:text-amber-300';
		}
		if (status === 'error') {
			return 'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900/70 dark:bg-rose-900/30 dark:text-rose-300';
		}
		return 'border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900/70 dark:bg-blue-900/30 dark:text-blue-300';
	}

	function getToolResultHeading(status: string) {
		return status === 'success' ? '工具结果' : '错误信息';
	}

	function pickFirstString(record: Record<string, any>, keys: string[]) {
		for (const key of keys) {
			const value = record?.[key];
			if (typeof value === 'string' && value.trim()) {
				return value.trim();
			}
		}
		return '';
	}

	function normalizeSearchResultItems(value: any): SearchResultItem[] {
		const parsed = parseJSONString(value);
		let candidates: any[] = [];

		if (Array.isArray(parsed)) {
			candidates = parsed;
		} else if (parsed && typeof parsed === 'object') {
			if (Array.isArray((parsed as any).results)) {
				candidates = (parsed as any).results;
			} else if (Array.isArray((parsed as any).data)) {
				candidates = (parsed as any).data;
			}
		}

		return candidates
			.filter((item) => item && typeof item === 'object')
			.map((item) => ({
				title: pickFirstString(item, ['title', 'name', 'pageName']),
				url: pickFirstString(item, ['url', 'link', 'sourceUrl', 'source_url']) || undefined,
				snippet:
					pickFirstString(item, [
						'content',
						'snippet',
						'summary',
						'pageContent',
						'page_content',
						'description'
					]) || undefined
			}))
			.filter((item) => item.title || item.url || item.snippet)
			.slice(0, 8);
	}
</script>

<div
	{id}
	class={className}
	data-tool-call-container={attributes?.type === 'tool_calls' ? 'true' : undefined}
>
	{#if attributes?.type === 'tool_calls'}
		{@const args = decode(attributes?.arguments)}
		{@const result = decode(attributes?.result ?? '')}
		{@const files = parseJSONString(decode(attributes?.files ?? ''))}
		{@const embeds = parseJSONString(decode(attributes?.embeds ?? ''))}
		{@const parsedArgs = parseJSONString(args)}
		{@const parsedResult = parseToolCallPayload(result)}

		{#if embeds && Array.isArray(embeds) && embeds.length > 0}
			<div class="py-1 w-full cursor-pointer">
				<div class=" w-full text-xs text-gray-500">
					<div class="">
						{resolveToolDisplay({
							toolId: attributes?.tool_id,
							toolName: attributes?.tool_name,
							legacyName: attributes?.name,
							parsedArgs: asRecord(parsedArgs),
							parsedResult
						}).toolName}
					</div>

					{#each embeds as embed, idx}
						<div class="my-2" id={`${collapsibleId}-tool-calls-${attributes?.id}-embed-${idx}`}>
							<FullHeightIframe
								src={embed}
								{args}
								allowScripts={true}
								allowForms={true}
								allowSameOrigin={true}
								allowPopups={true}
							/>
						</div>
					{/each}
				</div>
			</div>
		{:else}
			{@const argsRecord = asRecord(parsedArgs)}
			{@const toolFiles = dedupeToolFiles([
				...normalizeToolFiles(files),
				...collectFilesFromToolResult(parsedResult)
			])}
			{@const supportsArtifactInference = isFileGeneratingToolId(
				resolveToolDisplay({
					toolId: attributes?.tool_id,
					toolName: attributes?.tool_name,
					legacyName: attributes?.name
				}).toolId
			)}
			{@const hasArtifacts =
				supportsArtifactInference &&
				(toolFiles.length > 0 || hasResultFileArtifacts(parsedResult))}
			{@const toolStatus = resolveToolCallStatus(attributes ?? {}, {
				promoteArtifactRunning: hasArtifacts
			})}
			{@const statusDone = toolStatus === 'success'}
			{@const statusTerminal = toolStatus !== 'running'}
			{@const meta = getToolCardMeta(
				attributes?.tool_id,
				attributes?.tool_name,
				attributes?.name,
				argsRecord,
				parsedResult
			)}
			{@const searchItems = normalizeSearchResultItems(parsedResult)}
			{@const mergedCount = Number(attributes?.merged_count || 1)}
			{@const mergedResultsRaw = parseJSONString(decode(attributes?.merged_results ?? '[]'))}
			{@const mergedResults = Array.isArray(mergedResultsRaw) ? mergedResultsRaw : []}
			{@const mergedSearchItems = mergedResults.flatMap((entry) =>
				normalizeSearchResultItems(entry)
			)}
			{@const visibleSearchItems = mergedSearchItems.length > 0 ? mergedSearchItems : searchItems}
			<div
				class="my-1 w-full rounded-lg border border-gray-200/80 bg-white/90 dark:border-gray-800 dark:bg-gray-900/80"
				data-tool-call-content="true"
			>
				<button
					class="w-full cursor-pointer px-2.5 py-2 text-left"
					on:pointerup={() => {
						if (!disabled) {
							open = !open;
						}
					}}
				>
					<div class="flex items-start justify-between gap-2">
						<div class="min-w-0 flex items-start gap-2.5">
							<div
								class="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full bg-gray-100 text-xs dark:bg-gray-800"
							>
								{meta.icon}
							</div>
							<div class="min-w-0">
								<div class="text-[12px] font-semibold text-gray-800 dark:text-gray-100">
									{meta.label}
								</div>
								<div class="mt-0.5 line-clamp-1 text-[12px] text-gray-600 dark:text-gray-300">
									{meta.action}
								</div>
							</div>
						</div>

						<div class="flex shrink-0 items-center gap-1.5">
							{#if mergedCount > 1}
								<div
									class="rounded-full border border-violet-200 bg-violet-50 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 dark:border-violet-900/70 dark:bg-violet-900/30 dark:text-violet-300"
								>
									x{mergedCount}
								</div>
							{/if}
							<div
								class="rounded-full border px-1.5 py-0.5 text-[10px] font-medium {getToolStatusBadgeClass(
									toolStatus
								)}"
							>
								{getToolStatusLabel(toolStatus)}
							</div>
							{#if !statusTerminal}
								<Spinner className="size-4" />
							{/if}
							<div class="flex self-center translate-y-[1px] text-gray-500">
								{#if open}
									<ChevronUp strokeWidth="3.5" className="size-3.5" />
								{:else}
									<ChevronDown strokeWidth="3.5" className="size-3.5" />
								{/if}
							</div>
						</div>
					</div>
				</button>

				{#if !grow}
					{#if open && !hide}
						<div
							class="border-t border-gray-200/80 px-3 pb-3 pt-2 dark:border-gray-800"
							transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}
						>
							{#if Object.keys(argsRecord).length > 0}
								<div class="mb-2">
									<div
										class="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
									>
										输入参数
									</div>
									<pre
										class="m-0 overflow-x-auto whitespace-pre-wrap break-all rounded-md border border-gray-200 bg-gray-50 p-2 text-xs text-gray-700 dark:border-gray-700 dark:bg-gray-850 dark:text-gray-300">{formatJSONString(
											args
										)}</pre>
								</div>
							{/if}

							{#if statusTerminal}
								<div>
									<div
										class="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
									>
										{getToolResultHeading(toolStatus)}
									</div>
									{#if visibleSearchItems.length > 0}
										<div class="space-y-2">
											{#each visibleSearchItems as item, idx}
												<a
													href={item.url || '#'}
													target="_blank"
													rel="noreferrer"
													class="block rounded-md border border-gray-200 bg-gray-50 px-2.5 py-2 text-xs text-gray-700 hover:bg-gray-100 dark:border-gray-700 dark:bg-gray-850 dark:text-gray-200 dark:hover:bg-gray-800/80"
												>
													<div class="line-clamp-1 font-medium">
														{item.title || item.url || `结果 ${idx + 1}`}
													</div>
													{#if item.url}
														<div
															class="mt-0.5 line-clamp-1 text-[11px] text-blue-600 dark:text-blue-400"
														>
															{item.url}
														</div>
													{/if}
													{#if item.snippet}
														<div
															class="mt-1 line-clamp-2 text-[11px] text-gray-600 dark:text-gray-300"
														>
															{item.snippet}
														</div>
													{/if}
												</a>
											{/each}
										</div>
									{:else if mergedResults.length > 1}
										<pre
											class="m-0 overflow-x-auto whitespace-pre-wrap break-all rounded-md border border-gray-200 bg-gray-50 p-2 text-xs text-gray-700 dark:border-gray-700 dark:bg-gray-850 dark:text-gray-300">{formatJSONString(
												mergedResults
											)}</pre>
									{:else}
										<pre
											class="m-0 overflow-x-auto whitespace-pre-wrap break-all rounded-md border border-gray-200 bg-gray-50 p-2 text-xs text-gray-700 dark:border-gray-700 dark:bg-gray-850 dark:text-gray-300">{formatJSONString(
												result
											)}</pre>
									{/if}
								</div>
							{/if}

							{#if toolFiles.length > 0}
								<div class="mt-2">
									<div
										class="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
									>
										生成文件
									</div>
									<div class="space-y-1.5">
										{#each toolFiles as toolFile}
											<div
												class="flex items-center justify-between gap-2 rounded-md border border-gray-200 bg-gray-50 px-2 py-1.5 text-xs dark:border-gray-700 dark:bg-gray-850"
											>
												<a
													href={toolFile.url}
													target="_blank"
													rel="noreferrer"
													class="line-clamp-1 text-blue-600 hover:underline dark:text-blue-400"
												>
													{toolFile.name}
												</a>
												<div class="flex items-center gap-1.5">
													<button
														class="rounded border border-gray-200 px-1.5 py-0.5 text-[11px] text-gray-700 hover:bg-white dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-800"
														on:click|stopPropagation={openFilePreviewPane}
													>
														预览
													</button>
													<a
														href={toolFile.url}
														target="_blank"
														rel="noreferrer"
														class="rounded border border-gray-200 px-1.5 py-0.5 text-[11px] text-gray-700 hover:bg-white dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-800"
													>
														下载
													</a>
												</div>
											</div>
										{/each}
									</div>
								</div>
							{/if}
						</div>
					{/if}
				{/if}
			</div>
		{/if}

		{#if toolStatus !== 'running'}
			{#if typeof files === 'object'}
				{#each files ?? [] as file, idx}
					{#if typeof file === 'string'}
						{#if file.startsWith('data:image/')}
							<Image
								id={`${collapsibleId}-tool-calls-${attributes?.id}-result-${idx}`}
								src={file}
								alt="Image"
							/>
						{/if}
					{:else if typeof file === 'object'}
						{#if (file.type === 'image' || (file?.content_type ?? '').startsWith('image/')) && file.url}
							<Image
								id={`${collapsibleId}-tool-calls-${attributes?.id}-result-${idx}`}
								src={file.url}
								alt="Image"
							/>
						{/if}
					{/if}
				{/each}
			{/if}
		{/if}
	{:else if title !== null}
		<!-- svelte-ignore a11y-no-static-element-interactions -->
		<!-- svelte-ignore a11y-click-events-have-key-events -->
		<div
			class="{buttonClassName} {disabled ? '' : 'cursor-pointer'}"
			on:pointerup={() => {
				if (!disabled) {
					open = !open;
				}
			}}
		>
			<div
				class="w-full flex items-center gap-2 {centerTitle
					? 'relative justify-center'
					: 'justify-between'} {attributes?.done && attributes?.done !== 'true' ? 'shimmer' : ''}
			"
			>
				{#if attributes?.done && attributes?.done !== 'true'}
					<div>
						<Spinner className="size-4" />
					</div>
				{/if}

				<div class={centerTitle ? 'flex-1 text-center' : ''}>
					{#if attributes?.type === 'reasoning'}
						{#if attributes?.done === 'true' && attributes?.duration}
							{#if attributes.duration < 1}
								{$i18n.t('Thought for less than a second')}
							{:else if attributes.duration < 60}
								{$i18n.t('Thought for {{DURATION}} seconds', {
									DURATION: attributes.duration
								})}
							{:else}
								{$i18n.t('Thought for {{DURATION}}', {
									DURATION: dayjs.duration(attributes.duration, 'seconds').humanize()
								})}
							{/if}
						{:else}
							{$i18n.t('Thinking...')}
						{/if}
					{:else if attributes?.type === 'code_interpreter'}
						{#if attributes?.done === 'true'}
							{$i18n.t('Analyzed')}
						{:else}
							{$i18n.t('Analyzing...')}
						{/if}
					{:else}
						{title}
					{/if}
				</div>

				{#if !disabled}
					<div
						class="flex self-center translate-y-[1px] {centerTitle
							? 'absolute right-0 top-1/2 -translate-y-1/2'
							: ''}"
					>
						{#if open}
							<ChevronUp strokeWidth="3.5" className="size-3.5" />
						{:else}
							<ChevronDown strokeWidth="3.5" className="size-3.5" />
						{/if}
					</div>
				{/if}
			</div>
		</div>
	{:else}
		<!-- svelte-ignore a11y-no-static-element-interactions -->
		<!-- svelte-ignore a11y-click-events-have-key-events -->
		<div
			class="{buttonClassName} cursor-pointer"
			on:click={(e) => {
				e.stopPropagation();
			}}
			on:pointerup={(e) => {
				if (!disabled) {
					open = !open;
				}
			}}
		>
			<div>
				<div class="flex items-start justify-between">
					<slot />

					{#if chevron}
						<div class="flex self-start translate-y-1">
							{#if open}
								<ChevronUp strokeWidth="3.5" className="size-3.5" />
							{:else}
								<ChevronDown strokeWidth="3.5" className="size-3.5" />
							{/if}
						</div>
					{/if}
				</div>

				{#if grow}
					{#if open && !hide}
						<div
							transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}
							on:pointerup={(e) => {
								e.stopPropagation();
							}}
						>
							<slot name="content" />
						</div>
					{/if}
				{/if}
			</div>
		</div>
	{/if}

	{#if !grow}
		{#if open && !hide}
			<div transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}>
				<slot name="content" />
			</div>
		{/if}
	{/if}
</div>
