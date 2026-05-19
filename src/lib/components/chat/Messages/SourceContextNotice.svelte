<script lang="ts">
	type SourceScopeItem = {
		id?: string;
		name?: string;
		title?: string;
		type?: string;
		url?: string;
	};

	type ActiveSourceScope = {
		status?: string;
		source_set_mode?: string;
		sources?: SourceScopeItem[];
		source_ids?: string[];
	};

	type RetrievalDiagnostic = {
		reason?: string;
		classification?: string;
		url?: string;
		source?: { url?: string; name?: string };
		source_class?: string;
	};

	type RetrievalMetadata = {
		active_source_scope?: ActiveSourceScope;
		retrieval_diagnostics?: RetrievalDiagnostic[];
		canonical_references?: unknown[];
	};

	export let metadata: RetrievalMetadata | null | undefined = null;
	export let hasRenderableSources = false;

	const MAX_SOURCE_NAMES = 2;
	const MAX_DIAGNOSTICS = 3;

	const getSourceName = (source: SourceScopeItem | null | undefined): string => {
		const value = source?.name ?? source?.title ?? source?.url ?? source?.id ?? '';
		return typeof value === 'string' ? value.trim() : '';
	};

	const getSourceNames = (scope: ActiveSourceScope | null | undefined): string[] => {
		const names = Array.isArray(scope?.sources)
			? scope.sources.map(getSourceName).filter(Boolean)
			: [];
		if (names.length > 0) return names;

		return Array.isArray(scope?.source_ids)
			? scope.source_ids.map((id) => String(id).trim()).filter(Boolean)
			: [];
	};

	const getSourceState = (
		scope: ActiveSourceScope | null | undefined,
		diagnostics: RetrievalDiagnostic[]
	): 'none' | 'single' | 'multi' | 'ambiguous' => {
		const status = (scope?.status ?? '').toLowerCase();
		const mode = (scope?.source_set_mode ?? '').toLowerCase();
		const sourceCount = getSourceNames(scope).length;

		if (status === 'ambiguous' || mode === 'ambiguous') return 'ambiguous';
		if (mode === 'none') return sourceCount > 0 ? 'ambiguous' : 'none';
		if (mode === 'multi' || sourceCount > 1) return 'multi';
		if (mode === 'single' || sourceCount === 1) return 'single';
		if (
			diagnostics.length > 0 ||
			(Array.isArray(metadata?.canonical_references) && metadata.canonical_references.length > 0)
		) {
			return 'none';
		}
		return 'none';
	};

	const getScopeLabel = (state: 'none' | 'single' | 'multi' | 'ambiguous', names: string[]) => {
		if (state === 'ambiguous') return '来源不明确';
		if (state === 'single') return names[0] ? `当前来源：${names[0]}` : '当前来源：1 个来源';
		if (state === 'multi') return `当前来源：${names.length || '多个'} 个来源`;
		return '未限定来源';
	};

	const getScopeHint = (state: 'none' | 'single' | 'multi' | 'ambiguous', names: string[]) => {
		if (state === 'ambiguous') return '需要明确要使用的文件或来源';
		if (state === 'multi') return names.slice(0, MAX_SOURCE_NAMES).join('、');
		if (state === 'single') return names[0] ?? '';
		return hasRenderableSources ? '有可展示的引用来源' : '没有可展示的引用来源';
	};

	const getScopeClass = (state: 'none' | 'single' | 'multi' | 'ambiguous') => {
		if (state === 'ambiguous') {
			return 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/70 dark:bg-amber-950/30 dark:text-amber-300';
		}
		if (state === 'none') {
			return 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-800 dark:bg-gray-900/60 dark:text-gray-300';
		}
		return 'border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900/70 dark:bg-blue-950/30 dark:text-blue-300';
	};

	const getDiagnosticDomain = (diagnostic: RetrievalDiagnostic) => {
		const value = diagnostic?.url ?? diagnostic?.source?.url ?? diagnostic?.source?.name ?? '';
		if (!value) return '';
		try {
			return new URL(value).hostname.replace(/^www\./, '');
		} catch {
			return value.replace(/^https?:\/\//i, '').split(/[/?#]/)[0];
		}
	};

	const describeDiagnosticReason = (reason: string | undefined): string => {
		switch ((reason ?? '').trim()) {
			case 'ambiguous_retrieval_scope':
				return '多个可能来源匹配，请明确要使用哪个文件或来源。';
			case 'missing_document_body':
				return '来源内容为空，无法提取可核查证据。';
			case 'no_injectable_evidence':
				return '未找到可用于回答的证据片段。';
			case 'provider_blocked_url':
			case 'search_result_blocked':
			case 'webpage_result_blocked':
				return '来源被阻止或无法访问。';
			case 'weak_evidence':
				return '检索结果相关性较弱。';
			case 'empty_result':
				return '检索未返回结果。';
			default:
				return '检索存在限制，回答可能缺少可核查证据。';
		}
	};

	const shouldShowDiagnostic = (diagnostic: RetrievalDiagnostic | null | undefined): boolean => {
		if (!diagnostic || typeof diagnostic !== 'object') return false;
		const classification = (diagnostic.classification ?? '').toLowerCase();
		return (
			classification === 'no_evidence' ||
			classification === 'diagnostics' ||
			Boolean(diagnostic.reason)
		);
	};

	const getDiagnosticKey = (diagnostic: RetrievalDiagnostic): string =>
		`${diagnostic.reason ?? ''}:${diagnostic.classification ?? ''}:${getDiagnosticDomain(diagnostic)}`;

	$: diagnostics = Array.isArray(metadata?.retrieval_diagnostics)
		? metadata.retrieval_diagnostics.filter(shouldShowDiagnostic)
		: [];
	$: uniqueDiagnostics = diagnostics.filter(
		(diagnostic, index, items) =>
			items.findIndex((item) => getDiagnosticKey(item) === getDiagnosticKey(diagnostic)) === index
	);
	$: sourceNames = getSourceNames(metadata?.active_source_scope);
	$: sourceState = getSourceState(metadata?.active_source_scope, uniqueDiagnostics);
	$: scopeLabel = getScopeLabel(sourceState, sourceNames);
	$: scopeHint = getScopeHint(sourceState, sourceNames);
	$: scopeClass = getScopeClass(sourceState);
	$: hasCanonicalReferences =
		Array.isArray(metadata?.canonical_references) && metadata.canonical_references.length > 0;
	$: showScope =
		Boolean(metadata?.active_source_scope) ||
		uniqueDiagnostics.length > 0 ||
		hasCanonicalReferences;
	$: visibleDiagnostics = uniqueDiagnostics.slice(0, MAX_DIAGNOSTICS);
</script>

{#if showScope}
	<div class="mb-2 flex w-full flex-col gap-1.5 text-xs">
		<div class="flex flex-wrap items-center gap-1.5">
			<div
				class={`inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 ${scopeClass}`}
				title={sourceNames.join('、')}
			>
				<span class="size-1.5 shrink-0 rounded-full bg-current opacity-70"></span>
				<span class="truncate font-medium">{scopeLabel}</span>
			</div>
			{#if scopeHint}
				<div class="min-w-0 flex-1 truncate text-gray-500 dark:text-gray-400">{scopeHint}</div>
			{/if}
		</div>

		{#if visibleDiagnostics.length > 0}
			<div
				class="rounded-xl border border-amber-200/80 bg-amber-50/70 px-3 py-2 text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/25 dark:text-amber-200"
			>
				<div class="mb-1 font-medium">检索限制</div>
				<div class="space-y-1">
					{#each visibleDiagnostics as diagnostic}
						{@const domain = getDiagnosticDomain(diagnostic)}
						<div class="flex gap-2">
							<span class="mt-1 size-1 shrink-0 rounded-full bg-current opacity-60"></span>
							<span>
								{describeDiagnosticReason(diagnostic.reason)}
								{#if domain}
									<span class="text-amber-700/80 dark:text-amber-200/80">（{domain}）</span>
								{/if}
							</span>
						</div>
					{/each}
				</div>
			</div>
		{/if}
	</div>
{/if}
