<script lang="ts">
	import { decode } from 'html-entities';
	import { v4 as uuidv4 } from 'uuid';

	import { getContext } from 'svelte';
	const i18n = getContext('i18n');

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
	import { WEBUI_API_BASE_URL } from '$lib/constants';

	export let open = false;

	export let className = '';
	export let buttonClassName =
		'w-fit text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition';

	export let id = '';
	export let title = null;
	export let attributes = null;

	export let chevron = false;
	export let grow = false;

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

	function normalizeOpenWebUiFileUrl(value: string): string {
		if (!value) return value;

		let output = value;
		if (output.includes('/v1/files/') && !output.includes('/openai/v1/files/')) {
			output = output.replace(/(^|[^/])\/v1\/files\//g, '$1/openai/v1/files/');
		}

		if (output.startsWith('http') || output.startsWith('data:') || output.startsWith('/')) {
			return output;
		}

		return `${WEBUI_API_BASE_URL}/files/${output}/content`;
	}

	function toToolFileItem(value: any, source: string): ToolFileItem | null {
		if (typeof value === 'string') {
			const ref = normalizeFileRef(value);
			if (!ref) return null;
			const url = normalizeOpenWebUiFileUrl(ref);
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
			normalizeFileRef(value.url) ||
			normalizeFileRef(value.download_url) ||
			normalizeFileRef(value.downloadUrl) ||
			normalizeFileRef(value.domainUrl) ||
			normalizeFileRef(value.ossUrl) ||
			normalizeFileRef(value.id) ||
			normalizeFileRef(value.file_id) ||
			normalizeFileRef(value.fileId);

		if (!ref) {
			return null;
		}

		const url = normalizeOpenWebUiFileUrl(ref);
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

	const TOOL_LABELS: Record<string, string> = {
		internet_search: '网络搜索',
		联网搜索: '网络搜索',
		visit_webpage: '网页读取',
		网页读取: '网页读取',
		current_server_time: '服务器时间',
		服务器时间: '服务器时间',
		math_calculator: '数学计算',
		数学计算: '数学计算',
		read_structured_file: '读取结构化文件',
		读取结构化文件: '读取结构化文件',
		write_structured_file: '写入结构化文件',
		写入结构化文件: '写入结构化文件',
		gotenberg_convert: 'PDF 转换',
		'PDF转换': 'PDF 转换'
	};

	const TOOL_ICONS: Record<string, string> = {
		网络搜索: '🔎',
		网页读取: '🌐',
		服务器时间: '🕒',
		数学计算: '🧮',
		读取结构化文件: '📄',
		写入结构化文件: '✍️',
		'PDF 转换': '📘'
	};

	function asRecord(value: any): Record<string, any> {
		if (value && typeof value === 'object' && !Array.isArray(value)) {
			return value;
		}
		return {};
	}

	function getStringArg(record: Record<string, any>, keys: string[]) {
		for (const key of keys) {
			const value = record?.[key];
			if (typeof value === 'string' && value.trim()) {
				return value.trim();
			}
		}
		return '';
	}

	function getToolCardMeta(rawName: string, args: Record<string, any>) {
		const name = TOOL_LABELS[rawName] || rawName || '工具调用';
		const icon = TOOL_ICONS[name] || '🧩';

		if (name === '网络搜索') {
			const query = getStringArg(args, ['query', 'q']);
			return {
				label: name,
				icon,
				action: query ? `正在检索：${query}` : '正在检索网络信息',
				reason: '需要获取外部来源作为回答依据'
			};
		}

		if (name === '网页读取') {
			const url = getStringArg(args, ['url', 'href', 'link']);
			return {
				label: name,
				icon,
				action: url ? `正在读取：${url}` : '正在读取网页内容',
				reason: '需要读取原始页面以补充细节'
			};
		}

		if (name === '读取结构化文件') {
			const path = getStringArg(args, ['path', 'file_path', 'file']);
			return {
				label: name,
				icon,
				action: path ? `正在读取：${path}` : '正在读取结构化文件',
				reason: '需要先提取文件内容再进行总结或分析'
			};
		}

		if (name === '写入结构化文件') {
			const path = getStringArg(args, ['path', 'file_path', 'file']);
			return {
				label: name,
				icon,
				action: path ? `正在写入：${path}` : '正在写入结构化文件',
				reason: '需要把处理结果保存到目标文件'
			};
		}

		if (name === 'PDF 转换') {
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

		{#if embeds && Array.isArray(embeds) && embeds.length > 0}
			<div class="py-1 w-full cursor-pointer">
				<div class=" w-full text-xs text-gray-500">
					<div class="">
						{attributes.name}
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
		{:else}
			{@const parsedArgs = parseJSONString(args)}
			{@const parsedResult = parseJSONString(result)}
			{@const argsRecord = asRecord(parsedArgs)}
			{@const statusDone = attributes?.done === 'true'}
			{@const meta = getToolCardMeta(attributes?.name, argsRecord)}
			{@const searchItems = normalizeSearchResultItems(parsedResult)}
			{@const mergedCount = Number(attributes?.merged_count || 1)}
			{@const mergedResultsRaw = parseJSONString(decode(attributes?.merged_results ?? '[]'))}
			{@const mergedResults = Array.isArray(mergedResultsRaw) ? mergedResultsRaw : []}
			{@const mergedSearchItems = mergedResults.flatMap((entry) => normalizeSearchResultItems(entry))}
			{@const visibleSearchItems = mergedSearchItems.length > 0 ? mergedSearchItems : searchItems}
			{@const toolFiles = dedupeToolFiles([
				...normalizeToolFiles(files),
				...collectFilesFromToolResult(parsedResult)
			])}
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
							<div class="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full bg-gray-100 text-xs dark:bg-gray-800">
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
								<div class="rounded-full border border-violet-200 bg-violet-50 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 dark:border-violet-900/70 dark:bg-violet-900/30 dark:text-violet-300">
									x{mergedCount}
								</div>
							{/if}
							<div
								class="rounded-full border px-1.5 py-0.5 text-[10px] font-medium {statusDone
									? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/70 dark:bg-emerald-900/30 dark:text-emerald-300'
									: 'border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900/70 dark:bg-blue-900/30 dark:text-blue-300'}"
							>
								{statusDone ? '已完成' : '进行中'}
							</div>
							{#if !statusDone}
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
									<div class="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
										输入参数
									</div>
									<pre class="m-0 overflow-x-auto whitespace-pre-wrap break-all rounded-md border border-gray-200 bg-gray-50 p-2 text-xs text-gray-700 dark:border-gray-700 dark:bg-gray-850 dark:text-gray-300">{formatJSONString(args)}</pre>
								</div>
							{/if}

							{#if statusDone}
								<div>
									<div class="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
										工具结果
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
														<div class="mt-0.5 line-clamp-1 text-[11px] text-blue-600 dark:text-blue-400">
															{item.url}
														</div>
													{/if}
													{#if item.snippet}
														<div class="mt-1 line-clamp-2 text-[11px] text-gray-600 dark:text-gray-300">
															{item.snippet}
														</div>
													{/if}
												</a>
											{/each}
										</div>
									{:else if mergedResults.length > 1}
										<pre class="m-0 overflow-x-auto whitespace-pre-wrap break-all rounded-md border border-gray-200 bg-gray-50 p-2 text-xs text-gray-700 dark:border-gray-700 dark:bg-gray-850 dark:text-gray-300">{formatJSONString(mergedResults)}</pre>
									{:else}
										<pre class="m-0 overflow-x-auto whitespace-pre-wrap break-all rounded-md border border-gray-200 bg-gray-50 p-2 text-xs text-gray-700 dark:border-gray-700 dark:bg-gray-850 dark:text-gray-300">{formatJSONString(result)}</pre>
									{/if}
								</div>
							{/if}

							{#if toolFiles.length > 0}
								<div class="mt-2">
									<div class="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
										生成文件
									</div>
									<div class="space-y-1.5">
										{#each toolFiles as toolFile}
											<div class="flex items-center justify-between gap-2 rounded-md border border-gray-200 bg-gray-50 px-2 py-1.5 text-xs dark:border-gray-700 dark:bg-gray-850">
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

		{#if attributes?.done === 'true'}
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
				class=" w-full flex items-center justify-between gap-2 {attributes?.done &&
				attributes?.done !== 'true'
					? 'shimmer'
					: ''}
			"
			>
				{#if attributes?.done && attributes?.done !== 'true'}
					<div>
						<Spinner className="size-4" />
					</div>
				{/if}

				<div class="">
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
					<div class="flex self-center translate-y-[1px]">
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
