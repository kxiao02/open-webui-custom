<script lang="ts">
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { config, user } from '$lib/stores';
	import {
		bindKnowflowApiKey,
		clearKnowflowBinding,
		getKnowflowBindingStatus,
		getKnowflowFrontendConfig
	} from '$lib/apis/knowflow';
	import { getEmbeddingConfig, updateEmbeddingConfig } from '$lib/apis/retrieval';
	import { reindexKnowledgeFiles } from '$lib/apis/knowledge';

	import SensitiveInput from '$lib/components/common/SensitiveInput.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import LinkSlash from '$lib/components/icons/LinkSlash.svelte';
	import PencilSquare from '$lib/components/icons/PencilSquare.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	type KnowflowBindingStatus = {
		connected: boolean;
		status: string;
		mode: string;
		bind_required: boolean;
		manual_binding_present?: boolean;
		public_binding_present?: boolean;
		user_id?: string;
		user_email?: string;
		user_name?: string;
	};

	let knowflowStatus: KnowflowBindingStatus | null = null;
	let knowflowStatusLoading = false;
	let knowflowStatusFetched = false;
	let knowflowBindLoading = false;
	let knowflowClearLoading = false;
	let knowflowApiKeyInput = '';
	let showKnowflowBindingEditor = false;
	let knowflowError = '';
	let knowflowInitialized = false;

	let embeddingConfigLoading = false;
	let embeddingSaveLoading = false;
	let embeddingReindexLoading = false;
	let embeddingSettingsError = '';
	let adminSettingsInitialized = false;

	let RAG_EMBEDDING_ENGINE = '';
	let RAG_EMBEDDING_MODEL = '';
	let RAG_EMBEDDING_BATCH_SIZE = 1;
	let ENABLE_ASYNC_EMBEDDING = true;
	let RAG_EMBEDDING_CONCURRENT_REQUESTS = 0;

	let OpenAIUrl = '';
	let OpenAIKey = '';
	let AzureOpenAIUrl = '';
	let AzureOpenAIKey = '';
	let AzureOpenAIVersion = '';
	let OllamaUrl = '';
	let OllamaKey = '';

	const knowflowStatusPillBaseClass =
		'inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 select-none pointer-events-none';
	const knowflowStatusPillConnectedClass =
		'border-green-200 bg-green-50 text-green-700 dark:border-green-800/70 dark:bg-green-900/15 dark:text-green-300';
	const knowflowStatusPillNeutralClass =
		'border-gray-200 bg-gray-100/70 text-gray-600 dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-300';
	const knowflowStatusPillWarningClass =
		'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800/70 dark:bg-amber-900/15 dark:text-amber-300';
	const knowflowStatusItemClass =
		'inline-flex min-w-[10.5rem] items-center justify-between gap-2 rounded-2xl border border-gray-100/60 bg-white/80 px-3 py-2 dark:border-gray-850/35 dark:bg-gray-900/65';
	const knowflowWideStatusItemClass =
		'flex items-start justify-between gap-3 rounded-2xl border border-gray-100/60 bg-white/80 px-3 py-2 dark:border-gray-850/35 dark:bg-gray-900/65';
	const knowflowSoftActionButtonClass =
		'inline-flex items-center gap-2 rounded-xl border border-gray-200/70 bg-gray-100/90 px-3 py-2 text-xs font-medium text-gray-700 shadow-none transition hover:border-gray-200 hover:bg-gray-100 dark:border-gray-800/70 dark:bg-gray-850/80 dark:text-gray-100 dark:hover:border-gray-700 dark:hover:bg-gray-800 disabled:opacity-60 disabled:cursor-not-allowed';
	const knowflowFloatSectionClass = 'w-full space-y-3 lg:ml-auto lg:w-[24rem]';
	const knowflowSubtlePanelClass =
		'rounded-2xl border border-gray-100/45 bg-gray-50/45 px-3 py-2.5 dark:border-gray-850/30 dark:bg-gray-950/15';
	const knowflowInputPanelClass =
		'rounded-2xl border border-gray-100/45 bg-white/85 px-3 py-3 dark:border-gray-850/30 dark:bg-gray-900/80';

	const getToken = () =>
		typeof localStorage === 'undefined' ? '' : localStorage.getItem('token') ?? '';

	const kbText = {
		status: '状态',
		mode: '模式',
		account: '账号',
		actions: '操作',
		updateBinding: '更新绑定',
		clearManualBinding: '清除手动绑定',
		bindApiKey: '绑定 API 密钥',
		bindingRequired: '需要绑定',
		knowflowApiKey: 'Knowflow API 密钥',
		cancel: '取消',
		pasteKnowflowApiKey: '粘贴 Knowflow API 密钥',
		loadingKnowflowStatus: '正在加载 Knowflow 状态...',
		managed: '托管',
		manual: '手动',
		unknown: '未知',
		connected: '已连接',
		notConnected: '未连接',
		manualBindingDisabledToast: '管理员已禁用手动绑定。',
		knowflowApiKeyRequired: '需要提供 Knowflow API 密钥。',
		knowflowApiKeyConnected: 'Knowflow API 密钥已连接。',
		knowflowManualBindingCleared: '已清除 Knowflow 手动绑定。',
		managedModeHint: 'Knowflow 当前以托管模式连接，无需手动绑定 API 密钥。',
		publicMode: '公共只读',
		manualBindingDisabledHint: '管理员已禁用手动 API 密钥绑定。',
		knowflowDisabled: '未启用 Knowflow 集成。',
		knowledgeBaseSettings: '知识库设置',
		loading: '加载中...',
		embeddingModelEngine: '向量模型引擎',
		defaultSentenceTransformers: '默认（SentenceTransformers）',
		apiBaseUrl: '接口地址',
		apiKey: 'API 密钥',
		version: '版本',
		embeddingModel: '向量模型',
		setEmbeddingModel: '设置向量模型',
		embeddingBatchSize: '批处理大小',
		asyncEmbeddingProcessing: '异步向量处理',
		embeddingConcurrentRequests: '并发请求数',
		embeddingConfigHint:
			'更新或切换向量模型后，需要重新索引知识库才能生效。可使用下方“重新索引”按钮执行。',
		reindexing: '重新索引中...',
		reindex: '重新索引',
		saving: '保存中...',
		save: '保存',
		saveSuccess: '保存成功',
		openAIConfigRequired: '需要填写 OpenAI 接口地址和密钥。'
	};

	$: isAdmin = $user?.role === 'admin';

	$: knowflowConfig = getKnowflowFrontendConfig($config);
	$: knowflowEnabled = Boolean(knowflowConfig?.enabled);
	$: knowflowManualBindEnabled = Boolean(knowflowConfig?.manualBindEnabled);
	$: knowflowModeLabel =
		knowflowStatus?.mode === 'managed'
			? kbText.managed
			: knowflowStatus?.mode === 'manual_bind'
				? kbText.manual
				: knowflowStatus?.mode === 'public'
					? kbText.publicMode
					: kbText.unknown;
	$: knowflowConnectedLabel = knowflowStatus?.connected ? kbText.connected : kbText.notConnected;
	$: knowflowManagedMode = knowflowStatus?.mode === 'managed' && knowflowStatus?.connected;
	$: knowflowManualBindingPresent = Boolean(knowflowStatus?.manual_binding_present);
	$: knowflowHasValidBinding = Boolean(knowflowStatus?.connected && !knowflowStatus?.bind_required);
	$: showKnowflowBindingAction =
		knowflowManualBindEnabled &&
		!knowflowManagedMode &&
		knowflowHasValidBinding &&
		!showKnowflowBindingEditor;
	$: showKnowflowClearAction = Boolean(knowflowManualBindingPresent && !knowflowManagedMode);
	$: showKnowflowApiKeyInput =
		!knowflowManagedMode &&
		(showKnowflowBindingEditor ||
			(!knowflowHasValidBinding && (knowflowStatusFetched || Boolean(knowflowStatus))));

	$: if (knowflowEnabled && !knowflowInitialized) {
		knowflowInitialized = true;
		void refreshKnowflowStatus({ silent: true });
	}

	$: if (!knowflowEnabled) {
		knowflowInitialized = false;
		knowflowStatusFetched = false;
		knowflowStatus = null;
		knowflowError = '';
	}

	$: if (!knowflowManualBindEnabled || knowflowManagedMode) {
		showKnowflowBindingEditor = false;
	}

	$: if (isAdmin && !adminSettingsInitialized) {
		adminSettingsInitialized = true;
		void loadEmbeddingConfig({ silent: true });
	}

	$: if (!isAdmin) {
		adminSettingsInitialized = false;
	}

	const refreshKnowflowStatus = async ({ silent = false }: { silent?: boolean } = {}) => {
		if (!knowflowEnabled) {
			knowflowStatus = null;
			return;
		}

		const token = getToken();
		if (!token) {
			return;
		}

		knowflowError = '';
		knowflowStatusLoading = true;

		try {
			knowflowStatus = await getKnowflowBindingStatus(token);
		} catch (error) {
			knowflowError = `${error}`;
			if (!silent) {
				toast.error(`${error}`);
			}
		} finally {
			knowflowStatusLoading = false;
			knowflowStatusFetched = true;
		}
	};

	export const refreshKnowflowStatusFromOutside = async () => {
		await refreshKnowflowStatus();
	};

	const bindKnowflowApiKeyHandler = async () => {
		if (!knowflowManualBindEnabled) {
			toast.error(kbText.manualBindingDisabledToast);
			return;
		}

		if (!knowflowApiKeyInput.trim()) {
			toast.error(kbText.knowflowApiKeyRequired);
			return;
		}

		const token = getToken();
		if (!token) {
			return;
		}

		knowflowError = '';
		knowflowBindLoading = true;

		try {
			knowflowStatus = await bindKnowflowApiKey(token, knowflowApiKeyInput.trim());
			knowflowApiKeyInput = '';
			showKnowflowBindingEditor = false;
			toast.success(kbText.knowflowApiKeyConnected);
		} catch (error) {
			knowflowError = `${error}`;
			toast.error(`${error}`);
		} finally {
			knowflowBindLoading = false;
		}
	};

	const clearKnowflowBindingHandler = async () => {
		const token = getToken();
		if (!token) {
			return;
		}

		knowflowError = '';
		knowflowClearLoading = true;

		try {
			knowflowStatus = await clearKnowflowBinding(token);
			knowflowApiKeyInput = '';
			showKnowflowBindingEditor = false;
			toast.success(kbText.knowflowManualBindingCleared);
		} catch (error) {
			knowflowError = `${error}`;
			toast.error(`${error}`);
		} finally {
			knowflowClearLoading = false;
		}
	};

	const handleEmbeddingEngineChange = (nextEngine: string) => {
		RAG_EMBEDDING_ENGINE = nextEngine;

		if (nextEngine === 'ollama') {
			RAG_EMBEDDING_MODEL = '';
		} else if (nextEngine === 'openai' || nextEngine === 'azure_openai') {
			RAG_EMBEDDING_MODEL = 'text-embedding-3-small';
		} else if (nextEngine === '') {
			RAG_EMBEDDING_MODEL = 'sentence-transformers/all-MiniLM-L6-v2';
		}
	};

	const loadEmbeddingConfig = async ({ silent = false }: { silent?: boolean } = {}) => {
		const token = getToken();
		if (!token) {
			return;
		}

		embeddingSettingsError = '';
		embeddingConfigLoading = true;

		try {
			const embeddingConfig = await getEmbeddingConfig(token);
			if (!embeddingConfig) {
				return;
			}

			RAG_EMBEDDING_ENGINE = embeddingConfig.RAG_EMBEDDING_ENGINE ?? '';
			RAG_EMBEDDING_MODEL = embeddingConfig.RAG_EMBEDDING_MODEL ?? '';
			RAG_EMBEDDING_BATCH_SIZE = embeddingConfig.RAG_EMBEDDING_BATCH_SIZE ?? 1;
			ENABLE_ASYNC_EMBEDDING = embeddingConfig.ENABLE_ASYNC_EMBEDDING ?? true;
			RAG_EMBEDDING_CONCURRENT_REQUESTS = embeddingConfig.RAG_EMBEDDING_CONCURRENT_REQUESTS ?? 0;

			OpenAIKey = embeddingConfig?.openai_config?.key ?? '';
			OpenAIUrl = embeddingConfig?.openai_config?.url ?? '';

			OllamaKey = embeddingConfig?.ollama_config?.key ?? '';
			OllamaUrl = embeddingConfig?.ollama_config?.url ?? '';

			AzureOpenAIKey = embeddingConfig?.azure_openai_config?.key ?? '';
			AzureOpenAIUrl = embeddingConfig?.azure_openai_config?.url ?? '';
			AzureOpenAIVersion = embeddingConfig?.azure_openai_config?.version ?? '';
		} catch (error) {
			embeddingSettingsError = `${error}`;
			if (!silent) {
				toast.error(`${error}`);
			}
		} finally {
			embeddingConfigLoading = false;
		}
	};

	const saveEmbeddingConfigHandler = async () => {
		if (!isAdmin) {
			return;
		}

		if (RAG_EMBEDDING_ENGINE === '' && RAG_EMBEDDING_MODEL.split('/').length - 1 > 1) {
			toast.error(
				$i18n.t(
					'Model filesystem path detected. Model shortname is required for update, cannot continue.'
				)
			);
			return;
		}

		if (RAG_EMBEDDING_ENGINE === 'ollama' && RAG_EMBEDDING_MODEL === '') {
			toast.error(
				$i18n.t(
					'Model filesystem path detected. Model shortname is required for update, cannot continue.'
				)
			);
			return;
		}

		if (RAG_EMBEDDING_ENGINE === 'openai' && RAG_EMBEDDING_MODEL === '') {
			toast.error(
				$i18n.t(
					'Model filesystem path detected. Model shortname is required for update, cannot continue.'
				)
			);
			return;
		}

		if (
			RAG_EMBEDDING_ENGINE === 'azure_openai' &&
			(AzureOpenAIKey === '' || AzureOpenAIUrl === '' || AzureOpenAIVersion === '')
		) {
			toast.error(kbText.openAIConfigRequired);
			return;
		}

		const token = getToken();
		if (!token) {
			return;
		}

		embeddingSettingsError = '';
		embeddingSaveLoading = true;

		try {
			await updateEmbeddingConfig(token, {
				RAG_EMBEDDING_ENGINE,
				RAG_EMBEDDING_MODEL,
				RAG_EMBEDDING_BATCH_SIZE,
				ENABLE_ASYNC_EMBEDDING,
				RAG_EMBEDDING_CONCURRENT_REQUESTS,
				ollama_config: {
					key: OllamaKey,
					url: OllamaUrl
				},
				openai_config: {
					key: OpenAIKey,
					url: OpenAIUrl
				},
				azure_openai_config: {
					key: AzureOpenAIKey,
					url: AzureOpenAIUrl,
					version: AzureOpenAIVersion
				}
			} as any);

			toast.success(kbText.saveSuccess);
		} catch (error) {
			embeddingSettingsError = `${error}`;
			toast.error(`${error}`);
			await loadEmbeddingConfig({ silent: true });
		} finally {
			embeddingSaveLoading = false;
		}
	};

	const reindexKnowledgeHandler = async () => {
		if (!isAdmin || embeddingReindexLoading) {
			return;
		}

		const token = getToken();
		if (!token) {
			return;
		}

		embeddingSettingsError = '';
		embeddingReindexLoading = true;

		try {
			await reindexKnowledgeFiles(token);
			toast.success(kbText.saveSuccess);
		} catch (error) {
			embeddingSettingsError = `${error}`;
			toast.error(`${error}`);
		} finally {
			embeddingReindexLoading = false;
		}
	};
</script>

<div class="space-y-3 text-sm">
	<div class="flex justify-end">
		<section
			class={`rounded-3xl border border-gray-100/25 bg-white/80 px-3.5 py-3 dark:border-gray-850/25 dark:bg-gray-900/75 ${knowflowFloatSectionClass}`}
		>
			<div class="flex items-center justify-between gap-2">
				<div class="font-medium">{$i18n.t('Knowflow')}</div>
				{#if knowflowStatusFetched && !knowflowStatusLoading}
					<div
						class={`${knowflowStatusPillBaseClass} ${
							knowflowStatus?.connected
								? knowflowStatusPillConnectedClass
								: knowflowStatusPillNeutralClass
						}`}
					>
						{knowflowConnectedLabel}
					</div>
				{/if}
			</div>

			{#if knowflowEnabled}
				{#if knowflowStatusLoading}
					<div class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
						<Spinner className="size-3.5" />
						<span>{kbText.loadingKnowflowStatus}</span>
					</div>
				{:else}
					<div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
						<div class={knowflowStatusItemClass}>
							<div class="text-[11px] text-gray-500 dark:text-gray-400">{kbText.status}</div>
							<div
								class={`${knowflowStatusPillBaseClass} ${
									knowflowStatus?.connected
										? knowflowStatusPillConnectedClass
										: knowflowStatusPillNeutralClass
								}`}
							>
								{knowflowConnectedLabel}
							</div>
						</div>

						<div class={knowflowStatusItemClass}>
							<div class="text-[11px] text-gray-500 dark:text-gray-400">{kbText.mode}</div>
							<div class={`${knowflowStatusPillBaseClass} ${knowflowStatusPillNeutralClass}`}>
								{knowflowModeLabel}
							</div>
						</div>

						{#if knowflowStatus?.bind_required}
							<div
								class="sm:col-span-2 flex items-center justify-between gap-2 rounded-2xl border border-amber-200/70 bg-amber-50/70 px-3 py-2 dark:border-amber-900/35 dark:bg-amber-950/15"
							>
								<div class="text-[11px] text-amber-700 dark:text-amber-300">
									{kbText.bindApiKey}
								</div>
								<div class={`${knowflowStatusPillBaseClass} ${knowflowStatusPillWarningClass}`}>
									{kbText.bindingRequired}
								</div>
							</div>
						{/if}

						{#if knowflowStatus?.user_email || knowflowStatus?.user_name}
							<div class={`sm:col-span-2 ${knowflowWideStatusItemClass}`}>
								<div class="pt-0.5 text-[11px] text-gray-500 dark:text-gray-400">{kbText.account}</div>
								<div class="max-w-[72%] break-all text-right text-xs font-medium text-gray-700 dark:text-gray-200">
									{knowflowStatus?.user_name || knowflowStatus?.user_email}
								</div>
							</div>
						{/if}
					</div>
				{/if}

				{#if knowflowError}
					<div
						class="rounded-xl border border-red-200 bg-red-50/80 px-3 py-2 text-xs text-red-500 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300"
					>
						{knowflowError}
					</div>
				{/if}

				{#if showKnowflowBindingAction || showKnowflowClearAction}
					<div class="space-y-2.5 border-t border-gray-200/70 pt-3 dark:border-gray-800/80">
						<div class="text-[11px] font-medium tracking-wide text-gray-500 dark:text-gray-400">
							{kbText.actions}
						</div>

						<div class="flex flex-wrap gap-2">
							{#if showKnowflowBindingAction}
								<button
									class={knowflowSoftActionButtonClass}
									type="button"
									disabled={knowflowStatusLoading || knowflowBindLoading || knowflowClearLoading}
										on:click={() => {
											knowflowApiKeyInput = '';
											showKnowflowBindingEditor = true;
										}}
									>
										<PencilSquare className="size-3.5" strokeWidth="2.25" />
										<span>{knowflowManualBindingPresent ? kbText.updateBinding : kbText.bindApiKey}</span>
									</button>
							{/if}

							{#if showKnowflowClearAction}
								<button
									class="inline-flex items-center gap-2 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs font-medium text-red-600 shadow-xs transition hover:bg-red-100 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300 dark:hover:bg-red-950/30 disabled:opacity-60 disabled:cursor-not-allowed"
									type="button"
									disabled={knowflowClearLoading || knowflowStatusLoading}
									on:click={() => {
										clearKnowflowBindingHandler();
									}}
								>
									<LinkSlash className="size-3.5" strokeWidth="2.25" />
									{knowflowClearLoading ? '清除中...' : kbText.clearManualBinding}
								</button>
							{/if}
						</div>
					</div>
				{/if}

				{#if knowflowManualBindEnabled}
					{#if knowflowManagedMode}
						<div class={`${knowflowSubtlePanelClass} text-xs text-gray-500 dark:text-gray-400`}>
							{kbText.managedModeHint}
						</div>
					{:else if showKnowflowApiKeyInput}
						<div class={knowflowInputPanelClass}>
								<div class="flex items-center justify-between gap-2">
									<div class="space-y-0.5">
										<div class="text-xs font-medium">{kbText.knowflowApiKey}</div>
										<div class="text-xs text-gray-500 dark:text-gray-400">
											{knowflowManualBindingPresent ? kbText.updateBinding : kbText.bindApiKey}
										</div>
									</div>

								{#if knowflowHasValidBinding}
									<button
										class="inline-flex items-center justify-center rounded-xl border border-gray-200 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 transition hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 dark:hover:bg-gray-900 disabled:opacity-60 disabled:cursor-not-allowed"
										type="button"
										disabled={knowflowBindLoading || knowflowStatusLoading}
										on:click={() => {
											knowflowApiKeyInput = '';
											showKnowflowBindingEditor = false;
										}}
									>
										{kbText.cancel}
									</button>
								{/if}
							</div>

							<div class="flex flex-col gap-2">
								<SensitiveInput
									id="knowflow-api-key"
									placeholder={kbText.pasteKnowflowApiKey}
									type="password"
									bind:value={knowflowApiKeyInput}
								/>

								<button
									class="inline-flex w-full items-center justify-center rounded-xl border border-gray-200/70 bg-gray-100/90 px-3 py-2 text-xs font-medium text-gray-700 shadow-none transition hover:border-gray-200 hover:bg-gray-100 dark:border-gray-800/70 dark:bg-gray-850/80 dark:text-gray-100 dark:hover:border-gray-700 dark:hover:bg-gray-800 disabled:opacity-60 disabled:cursor-not-allowed"
									type="button"
									disabled={knowflowBindLoading || knowflowStatusLoading}
										on:click={() => {
											bindKnowflowApiKeyHandler();
										}}
									>
										{knowflowBindLoading
											? '连接中...'
											: knowflowManualBindingPresent
												? kbText.updateBinding
												: kbText.bindApiKey}
									</button>
							</div>
						</div>
					{/if}
				{:else}
					<div class={`${knowflowSubtlePanelClass} text-xs text-gray-500 dark:text-gray-400`}>
						{kbText.manualBindingDisabledHint}
					</div>
				{/if}
			{:else}
				<div class={`${knowflowSubtlePanelClass} text-xs text-gray-500 dark:text-gray-400`}>
					{kbText.knowflowDisabled}
				</div>
			{/if}
		</section>
	</div>

	{#if isAdmin}
		<section
			class="rounded-3xl border border-gray-100/30 bg-white px-3.5 py-3 dark:border-gray-850/30 dark:bg-gray-900 space-y-3"
		>
			<div class="font-medium">{kbText.knowledgeBaseSettings}</div>

			{#if embeddingConfigLoading}
				<div class="flex items-center gap-2 text-xs text-gray-500">
					<Spinner className="size-3.5" />
					<span>{kbText.loading}</span>
				</div>
			{:else}
				<div class="space-y-2.5">
					<div class="flex items-center justify-between gap-2">
						<div class="text-xs font-medium">{kbText.embeddingModelEngine}</div>
						<select
							class="w-fit pr-8 rounded-lg px-2 py-1 text-xs bg-transparent outline-hidden text-right border border-gray-200 dark:border-gray-800"
							value={RAG_EMBEDDING_ENGINE}
							on:change={(event) => {
								handleEmbeddingEngineChange((event.target as HTMLSelectElement).value);
							}}
						>
							<option value="">{kbText.defaultSentenceTransformers}</option>
							<option value="ollama">{$i18n.t('Ollama')}</option>
							<option value="openai">{$i18n.t('OpenAI')}</option>
							<option value="azure_openai">{$i18n.t('Azure OpenAI')}</option>
						</select>
					</div>

					{#if RAG_EMBEDDING_ENGINE === 'openai'}
						<div class="grid grid-cols-1 md:grid-cols-2 gap-2">
							<input
								class="w-full rounded-lg py-1.5 px-3 text-xs bg-gray-50 dark:bg-gray-850 outline-hidden"
								placeholder={kbText.apiBaseUrl}
								bind:value={OpenAIUrl}
								required
							/>
							<SensitiveInput
								id="knowledge-settings-openai-key"
								placeholder={kbText.apiKey}
								bind:value={OpenAIKey}
								required={false}
							/>
						</div>
					{:else if RAG_EMBEDDING_ENGINE === 'ollama'}
						<div class="grid grid-cols-1 md:grid-cols-2 gap-2">
							<input
								class="w-full rounded-lg py-1.5 px-3 text-xs bg-gray-50 dark:bg-gray-850 outline-hidden"
								placeholder={kbText.apiBaseUrl}
								bind:value={OllamaUrl}
								required
							/>
							<SensitiveInput
								id="knowledge-settings-ollama-key"
								placeholder={kbText.apiKey}
								bind:value={OllamaKey}
								required={false}
							/>
						</div>
					{:else if RAG_EMBEDDING_ENGINE === 'azure_openai'}
						<div class="space-y-2">
							<div class="grid grid-cols-1 md:grid-cols-2 gap-2">
								<input
									class="w-full rounded-lg py-1.5 px-3 text-xs bg-gray-50 dark:bg-gray-850 outline-hidden"
									placeholder={kbText.apiBaseUrl}
									bind:value={AzureOpenAIUrl}
									required
								/>
								<SensitiveInput
									id="knowledge-settings-azure-key"
									placeholder={kbText.apiKey}
									bind:value={AzureOpenAIKey}
									required={false}
								/>
							</div>
							<input
								class="w-full rounded-lg py-1.5 px-3 text-xs bg-gray-50 dark:bg-gray-850 outline-hidden"
								placeholder={kbText.version}
								bind:value={AzureOpenAIVersion}
								required
							/>
						</div>
					{/if}

					<div class="space-y-1">
						<div class="text-xs font-medium">{kbText.embeddingModel}</div>
						<input
							class="w-full rounded-lg py-1.5 px-3 text-xs bg-gray-50 dark:bg-gray-850 outline-hidden"
							placeholder={kbText.setEmbeddingModel}
							bind:value={RAG_EMBEDDING_MODEL}
						/>
					</div>

					<div class="flex items-center justify-between gap-2">
						<div class="text-xs font-medium">{kbText.embeddingBatchSize}</div>
						<input
							bind:value={RAG_EMBEDDING_BATCH_SIZE}
							type="number"
							class="bg-transparent text-right w-20 outline-none border-b border-gray-200 dark:border-gray-800 pb-0.5"
							min="-2"
							max="16000"
							step="1"
						/>
					</div>

					{#if RAG_EMBEDDING_ENGINE === 'ollama' || RAG_EMBEDDING_ENGINE === 'openai' || RAG_EMBEDDING_ENGINE === 'azure_openai'}
						<div class="flex items-center justify-between gap-2">
							<div class="text-xs font-medium">{kbText.asyncEmbeddingProcessing}</div>
							<Switch bind:state={ENABLE_ASYNC_EMBEDDING} />
						</div>

						<div class="flex items-center justify-between gap-2">
							<div class="text-xs font-medium">{kbText.embeddingConcurrentRequests}</div>
							<input
								bind:value={RAG_EMBEDDING_CONCURRENT_REQUESTS}
								type="number"
								class="bg-transparent text-right w-20 outline-none border-b border-gray-200 dark:border-gray-800 pb-0.5"
								min="0"
								step="1"
							/>
						</div>
					{/if}

					<div class="text-[11px] text-gray-500">
						{kbText.embeddingConfigHint}
					</div>
				</div>

				{#if embeddingSettingsError}
					<div class="text-xs text-red-500">{embeddingSettingsError}</div>
				{/if}

				<div class="flex items-center justify-end gap-2 pt-1">
					<button
						type="button"
						class="px-3 py-1.5 text-xs rounded-lg bg-gray-100 hover:bg-gray-200 dark:bg-gray-850 dark:hover:bg-gray-800 transition disabled:opacity-60 disabled:cursor-not-allowed"
						disabled={embeddingSaveLoading || embeddingReindexLoading}
						on:click={() => {
							reindexKnowledgeHandler();
						}}
					>
						{embeddingReindexLoading ? kbText.reindexing : kbText.reindex}
					</button>

					<button
						type="button"
						class="px-3 py-1.5 text-xs rounded-lg border border-gray-200/70 bg-gray-100/90 text-gray-700 shadow-none transition hover:border-gray-200 hover:bg-gray-100 dark:border-gray-800/70 dark:bg-gray-850/80 dark:text-gray-100 dark:hover:border-gray-700 dark:hover:bg-gray-800 disabled:opacity-60 disabled:cursor-not-allowed"
						disabled={embeddingSaveLoading || embeddingConfigLoading || embeddingReindexLoading}
						on:click={() => {
							saveEmbeddingConfigHandler();
						}}
					>
						{embeddingSaveLoading ? kbText.saving : kbText.save}
					</button>
				</div>
			{/if}
		</section>
	{/if}
</div>
