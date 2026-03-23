<script lang="ts">
	import { decode } from 'html-entities';
	import { v4 as uuidv4 } from 'uuid';

	import { getContext } from 'svelte';
	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';

	import ChevronDown from '../icons/ChevronDown.svelte';
	import Markdown from '../chat/Messages/Markdown.svelte';
	import Image from './Image.svelte';
	import FullHeightIframe from './FullHeightIframe.svelte';
	import { settings } from '$lib/stores';

	export let id: string = '';
	export let attributes: {
		type?: string;
		id?: string;
		name?: string;
		arguments?: string;
		result?: string;
		files?: string;
		embeds?: string;
		done?: string;
		status?: string;
	} = {};

	export let open = false;
	export let className = '';

	const RESULT_PREVIEW_LIMIT = 10000;
	let expandedResult = false;

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
		'PDF转换': 'PDF 转换',
		'PDF 转换': 'PDF 转换'
	};

	$: if (!open) expandedResult = false;
	export let buttonClassName = '';

	const componentId = id || uuidv4();

	function parseJSONString(str: string) {
		try {
			return parseJSONString(JSON.parse(str));
		} catch (e) {
			return str;
		}
	}

	function formatJSONString(str: string) {
		try {
			const parsed = parseJSONString(str);
			if (typeof parsed === 'object') {
				return JSON.stringify(parsed, null, 2);
			} else {
				return String(parsed);
			}
		} catch (e) {
			return str;
		}
	}

	function parseArguments(str: string): Record<string, unknown> | null {
		try {
			const parsed = parseJSONString(str);
			if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
				return parsed as Record<string, unknown>;
			}
			return null;
		} catch {
			return null;
		}
	}

	function normalizeStatus(status: string | undefined, done: string | undefined): string {
		const normalized = (status || '').trim().toLowerCase();
		if (['running', 'success', 'error', 'timeout'].includes(normalized)) {
			return normalized;
		}
		return done === 'true' ? 'success' : 'running';
	}

	function getStatusMessage(status: string, name: string): string {
		if (status === 'success') return `查看 ${name} 的结果`;
		if (status === 'timeout') return `${name} 已超时`;
		if (status === 'error') return `${name} 运行失败`;
		return `正在执行 ${name}...`;
	}

	$: args = decode(attributes?.arguments ?? '');
	$: result = decode(attributes?.result ?? '');
	$: files = parseJSONString(decode(attributes?.files ?? ''));
	$: embeds = parseJSONString(decode(attributes?.embeds ?? ''));
	$: parsedArgs = parseArguments(args);
	$: parsedResult = parseJSONString(result);
	$: displayName = TOOL_LABELS[attributes?.name ?? ''] ?? attributes?.name ?? '';
	$: status = normalizeStatus(attributes?.status, attributes?.done);
	$: isTerminal = status !== 'running';
	$: isExecuting = status === 'running';
	$: statusMessage = getStatusMessage(status, displayName);
	$: hasEmbeds = embeds && Array.isArray(embeds) && embeds.length > 0;
	$: hasFiles = Array.isArray(files) && files.length > 0;
	$: canExpand = !hasEmbeds && (Boolean(args) || Boolean(result) || hasFiles);
	$: if (!canExpand) open = false;
</script>

<div {id} class={className}>
	<div class="mb-2 w-full overflow-hidden rounded-xl border border-gray-200/90 bg-gray-50/80 dark:border-gray-800 dark:bg-gray-900/70">
		<button
			type="button"
			class="flex w-full items-center justify-between gap-2 border-b border-gray-200/80 px-3 py-2 text-left dark:border-gray-800 {buttonClassName} {canExpand ? '' : 'cursor-default'}"
			on:click={() => {
				if (canExpand) {
					open = !open;
				}
			}}
		>
			<div class="min-w-0">
				<div class="text-[11px] font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-300">
					{$i18n.t('Process')}
				</div>
				<div
					class="mt-0.5 line-clamp-1 text-xs text-gray-500 dark:text-gray-400 {isExecuting
						? 'shimmer'
						: ''}"
				>
					{statusMessage}
				</div>
			</div>

			{#if canExpand}
				<ChevronDown
					className={`size-3.5 shrink-0 text-gray-500 transition-transform ${open
						? 'rotate-180'
						: ''}`}
				/>
			{/if}
		</button>

		{#if (open && canExpand) || hasEmbeds}
			<div transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}>
				<div class="px-2 py-2 space-y-3">
					{#if hasEmbeds}
						{#each embeds as embed, idx}
							<div class="my-2" id={`${componentId}-tool-call-embed-${idx}`}>
								<FullHeightIframe
									src={embed}
									{args}
									allowScripts={true}
									allowForms={$settings?.iframeSandboxAllowForms ?? false}
									allowSameOrigin={$settings?.iframeSandboxAllowSameOrigin ?? false}
									allowPopups={true}
								/>
							</div>
						{/each}
					{:else}
						<div class="flex items-stretch gap-2">
							<div>
								<div class="mb-1.5 px-1 pt-3">
									<span class="relative flex size-1.5 items-center justify-center rounded-full">
										<span class="relative inline-flex size-1.5 rounded-full bg-gray-500 dark:bg-gray-400"></span>
									</span>
								</div>
							</div>

							<div class="flex-1 space-y-3">
								<div
									class="{isExecuting
										? 'shimmer'
										: ''} text-gray-500 dark:text-gray-500 text-base line-clamp-1 text-wrap"
								>
									{displayName}
								</div>

								<!-- Input -->
								{#if args}
									<div>
										<div
											class="text-[10px] uppercase tracking-wider font-medium text-gray-400 dark:text-gray-500 mb-1.5 px-1"
										>
											{$i18n.t('Input')}
										</div>

										{#if parsedArgs}
											<div class="px-1 space-y-0.5">
												{#each Object.entries(parsedArgs) as [key, value]}
													<div class="flex gap-2 text-xs py-0.5">
														<span class="font-medium text-gray-600 dark:text-gray-400 shrink-0"
															>{key}</span
														>
														<span class="text-gray-800 dark:text-gray-200 break-all"
															>{typeof value === 'object'
																? JSON.stringify(value)
																: value}</span
														>
													</div>
												{/each}
											</div>
										{:else}
											<div class="tool-call-body w-full max-w-none!">
												<Markdown
													id={`${componentId}-tool-call-args`}
													content={`\`\`\`json\n${formatJSONString(args)}\n\`\`\``}
												/>
											</div>
										{/if}
									</div>
								{/if}

								<!-- Output -->
								{#if isTerminal && result}
									<div>
										<div
											class="text-[10px] uppercase tracking-wider font-medium text-gray-400 dark:text-gray-500 mb-1.5 px-1"
										>
											{status === 'success' ? $i18n.t('Output') : '错误信息'}
										</div>
										<div class="w-full max-w-none!">
											{#if typeof parsedResult === 'object' && parsedResult !== null}
												<Markdown
													id={`${componentId}-tool-call-result`}
													content={`\`\`\`json\n${JSON.stringify(parsedResult, null, 2)}\n\`\`\``}
												/>
											{:else}
												{@const resultStr = String(parsedResult)}
												{@const isTruncated =
													resultStr.length > RESULT_PREVIEW_LIMIT && !expandedResult}
												<pre
													class="text-xs text-gray-600 dark:text-gray-300 whitespace-pre-wrap break-words font-mono">{isTruncated
														? resultStr.slice(0, RESULT_PREVIEW_LIMIT)
														: resultStr}</pre>
												{#if isTruncated}
													<button
														class="mt-1 text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition"
														on:click|stopPropagation={() => {
															expandedResult = true;
														}}
													>
														{$i18n.t('Show all ({{COUNT}} characters)', {
															COUNT: resultStr.length.toLocaleString()
														})}
													</button>
												{/if}
											{/if}
										</div>
									</div>
								{/if}
							</div>
						</div>
					{/if}
				</div>
			</div>
		{/if}
	</div>

	<!-- Files display (images etc.) when done -->
	{#if isTerminal}
		{#if typeof files === 'object'}
			{#each files ?? [] as file, idx}
				{#if typeof file === 'string'}
					{#if file.startsWith('data:image/')}
						<Image id={`${componentId}-tool-call-result-${idx}`} src={file} alt="Image" />
					{/if}
				{:else if typeof file === 'object'}
					{#if (file.type === 'image' || (file?.content_type ?? '').startsWith('image/')) && file.url}
						<Image id={`${componentId}-tool-call-result-${idx}`} src={file.url} alt="Image" />
					{/if}
				{/if}
			{/each}
		{/if}
	{/if}
</div>
