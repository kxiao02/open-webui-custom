<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { unescapeHtml } from '$lib/utils';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import Wrench from '$lib/components/icons/Wrench.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import Document from '$lib/components/icons/Document.svelte';
	import CommandLine from '$lib/components/icons/CommandLine.svelte';
	import Download from '$lib/components/icons/Download.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	export let token: any;

	type ToolFileItem = {
		id: string;
		name: string;
		url: string;
		isImage: boolean;
	};

	const parseAttr = (value: unknown): unknown => {
		if (typeof value !== 'string') return value;
		const decoded = unescapeHtml(value);
		if (!decoded) return '';

		try {
			let parsed = JSON.parse(decoded);
			if (typeof parsed === 'string') {
				const trimmed = parsed.trim();
				if (
					(trimmed.startsWith('{') && trimmed.endsWith('}')) ||
					(trimmed.startsWith('[') && trimmed.endsWith(']'))
				) {
					try {
						parsed = JSON.parse(trimmed);
					} catch {
						return parsed;
					}
				}
			}
			return parsed;
		} catch {
			return decoded;
		}
	};

	const prettyValue = (value: unknown): string => {
		if (value === null || value === undefined) return '';
		if (typeof value === 'string') return value;
		try {
			return JSON.stringify(value, null, 2);
		} catch {
			return String(value);
		}
	};

	const toBool = (value: unknown): boolean => {
		if (typeof value === 'boolean') return value;
		if (typeof value === 'string') return value.toLowerCase() === 'true';
		return !!value;
	};

	const normalizeFileUrl = (value: string): string => {
		if (!value) return value;
		if (value.startsWith('http') || value.startsWith('/') || value.startsWith('data:')) return value;
		return `/api/v1/files/${value}/content`;
	};

	const inferName = (value: string, fallback = 'generated-file') => {
		const sanitized = (value || '').split('?')[0];
		const pathPart = sanitized.split('/').pop() || sanitized;
		const windowsPathPart = pathPart.split('\\').pop() || pathPart;
		return windowsPathPart.trim() || fallback;
	};

	const isImageName = (name: string, type?: string, contentType?: string) => {
		if ((contentType || '').startsWith('image/')) return true;
		if ((type || '').toLowerCase() === 'image') return true;
		const ext = (name.split('.').pop() || '').toLowerCase();
		return ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext);
	};

	const normalizeFiles = (rawFiles: unknown): ToolFileItem[] => {
		if (!Array.isArray(rawFiles)) return [];
		const items: ToolFileItem[] = [];

		for (const raw of rawFiles) {
			if (typeof raw === 'string') {
				const id = raw.trim();
				if (!id) continue;
				const name = inferName(id);
				items.push({
					id: `file:${id}`,
					name,
					url: normalizeFileUrl(id),
					isImage: isImageName(name)
				});
				continue;
			}

			if (!raw || typeof raw !== 'object') continue;
			const file = raw as Record<string, unknown>;
			const fileRef =
				(typeof file.url === 'string' && file.url) ||
				(typeof file.download_url === 'string' && file.download_url) ||
				(typeof file.downloadUrl === 'string' && file.downloadUrl) ||
				(typeof file.id === 'string' && file.id) ||
				(typeof file.file_id === 'string' && file.file_id) ||
				(typeof file.fileId === 'string' && file.fileId) ||
				'';
			if (!fileRef) continue;

			const name =
				(typeof file.name === 'string' && file.name.trim()) ||
				(typeof file.filename === 'string' && file.filename.trim()) ||
				(typeof file.fileName === 'string' && file.fileName.trim()) ||
				inferName(fileRef);

			items.push({
				id: `file:${fileRef}:${name}`,
				name,
				url: normalizeFileUrl(fileRef),
				isImage: isImageName(
					name,
					typeof file.type === 'string' ? file.type : undefined,
					typeof file.content_type === 'string' ? file.content_type : undefined
				)
			});
		}

		const dedup = new Map<string, ToolFileItem>();
		for (const item of items) {
			dedup.set(`${item.url}|${item.name}`, item);
		}
		return Array.from(dedup.values());
	};

	const getToolMeta = (name: string) => {
		const normalized = (name || '').toLowerCase();
		if (normalized.includes('search') || normalized.includes('web')) {
			return {
				icon: Search,
				label: 'Web Search',
				iconClass: 'text-blue-600 dark:text-blue-400',
				bgClass: 'bg-blue-100 dark:bg-blue-950/60'
			};
		}
		if (normalized.includes('file') || normalized.includes('write') || normalized.includes('read')) {
			return {
				icon: Document,
				label: 'File Tool',
				iconClass: 'text-emerald-600 dark:text-emerald-400',
				bgClass: 'bg-emerald-100 dark:bg-emerald-950/60'
			};
		}
		if (normalized.includes('code') || normalized.includes('python') || normalized.includes('execute')) {
			return {
				icon: CommandLine,
				label: 'Code Tool',
				iconClass: 'text-purple-600 dark:text-purple-400',
				bgClass: 'bg-purple-100 dark:bg-purple-950/60'
			};
		}
		return {
			icon: Wrench,
			label: name || 'Tool Call',
			iconClass: 'text-gray-600 dark:text-gray-300',
			bgClass: 'bg-gray-100 dark:bg-gray-800'
		};
	};

	let open = false;
	$: done = toBool(token?.attributes?.done);
	$: toolName = token?.attributes?.name || '';
	$: summary = token?.summary || (done ? 'Tool Executed' : 'Executing...');

	$: argumentsParsed = parseAttr(token?.attributes?.arguments);
	$: resultParsed = parseAttr(token?.attributes?.result);
	$: filesParsed = normalizeFiles(parseAttr(token?.attributes?.files));
	$: embedsParsed = parseAttr(token?.attributes?.embeds);

	$: argumentsText = prettyValue(argumentsParsed);
	$: resultText = prettyValue(resultParsed);
	$: embedsText = prettyValue(embedsParsed);

	$: hasBody = !!argumentsText || !!resultText || filesParsed.length > 0 || !!embedsText;
	$: if (!hasBody) {
		open = false;
	}

	$: meta = getToolMeta(toolName);
</script>

<div
	data-tool-call-container="true"
	class="my-2 overflow-hidden rounded-xl border border-gray-200/90 bg-gray-50/80 dark:border-gray-800 dark:bg-gray-900/70"
>
	<button
		type="button"
		class="flex w-full items-start gap-3 px-3 py-2 text-left"
		on:click={() => {
			if (hasBody) open = !open;
		}}
	>
		<div class="relative mt-0.5 shrink-0">
			<div class={`flex size-7 items-center justify-center rounded-full ${meta.bgClass}`}>
				{#if !done}
					<Spinner className="size-3.5 text-blue-600 dark:text-blue-400" />
				{:else}
					<svelte:component this={meta.icon} className={`size-3.5 ${meta.iconClass}`} />
				{/if}
			</div>
			<span
				class={`absolute -bottom-0.5 -right-0.5 size-2 rounded-full border border-white dark:border-gray-900 ${done
					? 'bg-emerald-500'
					: 'bg-blue-500'}`}
			></span>
		</div>

		<div class="min-w-0 flex-1">
			<div class="line-clamp-1 text-[13px] font-medium text-gray-900 dark:text-gray-100">
				{toolName || meta.label}
			</div>
			<div class="line-clamp-1 text-xs text-gray-500 dark:text-gray-400">{summary}</div>
		</div>

		<div class="flex items-center gap-2">
			<span
				class={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${done
					? 'border-emerald-200 bg-emerald-100 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300'
					: 'border-blue-200 bg-blue-100 text-blue-700 dark:border-blue-900 dark:bg-blue-950/60 dark:text-blue-300'}`}
			>
				{done ? $i18n.t('Completed') : $i18n.t('Running')}
			</span>

			{#if hasBody}
				<ChevronDown className={`size-3.5 text-gray-500 transition-transform ${open ? 'rotate-180' : ''}`} />
			{/if}
		</div>
	</button>

	{#if open && hasBody}
		<div
			data-tool-call-content="true"
			class="space-y-2 border-t border-gray-200/80 px-3 pb-3 pt-2 dark:border-gray-800"
		>
			{#if argumentsText}
				<div class="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-950/70">
					<div class="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
						Input
					</div>
					<pre class="max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs text-gray-800 dark:text-gray-200">{argumentsText}</pre>
				</div>
			{/if}

			{#if resultText}
				<div class="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-950/70">
					<div class="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
						Result
					</div>
					<pre class="max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs text-gray-800 dark:text-gray-200">{resultText}</pre>
				</div>
			{/if}

			{#if filesParsed.length > 0}
				<div class="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-950/70">
					<div class="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
						Files
					</div>
					<div class="space-y-1.5">
						{#each filesParsed as file}
							<div class="flex items-center justify-between gap-2 rounded-md border border-gray-200 bg-gray-50 px-2 py-1.5 dark:border-gray-700 dark:bg-gray-900/70">
								<div class="min-w-0 flex items-center gap-2">
									{#if file.isImage}
										<img
											src={file.url}
											alt={file.name}
											class="size-7 rounded-md border border-gray-200 object-cover dark:border-gray-700"
										/>
									{/if}
									<a
										href={file.url}
										target="_blank"
										rel="noreferrer"
										class="line-clamp-1 text-xs font-medium text-gray-800 hover:text-blue-600 dark:text-gray-100 dark:hover:text-blue-400"
									>
										{file.name}
									</a>
								</div>
								<a
									href={file.url}
									target="_blank"
									rel="noreferrer"
									class="inline-flex size-7 shrink-0 items-center justify-center rounded-md border border-gray-200 bg-white text-gray-600 hover:bg-gray-100 hover:text-blue-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800 dark:hover:text-blue-400"
									title={$i18n.t('Download')}
								>
									<Download className="size-3.5" />
								</a>
							</div>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	{/if}
</div>
