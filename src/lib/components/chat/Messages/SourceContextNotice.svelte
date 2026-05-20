<script lang="ts">
	type RetrievalDiagnostic = {
		reason?: string;
		classification?: string;
		url?: string;
		source?: { url?: string; name?: string };
		source_class?: string;
	};

	type RetrievalMetadata = {
		retrieval_diagnostics?: RetrievalDiagnostic[];
		canonical_references?: unknown[];
	};

	export let metadata: RetrievalMetadata | null | undefined = null;

	const MAX_DIAGNOSTICS = 3;

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
	$: visibleDiagnostics = uniqueDiagnostics.slice(0, MAX_DIAGNOSTICS);
	$: showScope = visibleDiagnostics.length > 0;
</script>

{#if showScope}
	<div class="mb-2 flex w-full flex-col gap-1.5 text-xs">
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
