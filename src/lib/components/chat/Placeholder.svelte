<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { marked } from 'marked';

	import { onMount, getContext, tick, createEventDispatcher } from 'svelte';
	import { blur, fade } from 'svelte/transition';

	const dispatch = createEventDispatcher();

	import { generateFollowUps } from '$lib/apis';
	import { getChatList } from '$lib/apis/chats';
	import { updateFolderById } from '$lib/apis/folders';

	import {
		config,
		user,
		models as _models,
		temporaryChatEnabled,
		selectedFolder,
		chats,
		currentChatPage
	} from '$lib/stores';
	import { sanitizeResponseContent, extractCurlyBraceWords } from '$lib/utils';
	import {
		buildHistoryTitleSuggestions,
		getHardcodedSuggestionPrompts
	} from '$lib/utils/historySuggestions';
	import { WEBUI_API_BASE_URL, WEBUI_BASE_URL } from '$lib/constants';

	import Suggestions from './Suggestions.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import EyeSlash from '$lib/components/icons/EyeSlash.svelte';
	import MessageInput from './MessageInput.svelte';
	import FolderPlaceholder from './Placeholder/FolderPlaceholder.svelte';
	import FolderTitle from './Placeholder/FolderTitle.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	export let createMessagePair: Function;
	export let stopResponse: Function;

	export let autoScroll = false;

	export let atSelectedModel: Model | undefined;
	export let selectedModels: [''];

	export let history;

	export let prompt = '';
	export let files = [];
	export let messageInput = null;

	export let selectedToolIds = [];
	export let lockedToolIds = [];
	export let selectedFilterIds = [];

	export let showCommands = false;

	export let imageGenerationEnabled = false;
	export let codeInterpreterEnabled = false;
	export let webSearchEnabled = false;
	export let thinkingModeEnabled = false;
	export let thinkingModelId: string | null = null;

	export let onUpload: Function = (e) => {};
	export let onSelect = (e) => {};
	export let onChange = (e) => {};

	export let toolServers = [];

	export let dragged = false;

	let models = [];
	let selectedModelIdx = 0;
	let historySuggestionPrompts = [];
	let suggestionPrompts = [];

	const buildHistorySummaryPrompt = (titles: string[]): string => {
		const items = titles
			.slice(0, 50)
			.map((title, index) => `${index + 1}. ${title}`)
			.join('\n');

		return `以下是我全部历史会话标题。请基于这些标题，生成5条新的、可直接发送给助手的问题建议，优先覆盖近期主题。只输出问题本身。\n\n历史标题：\n${items}`;
	};

	const toSuggestionPrompts = (
		items: string[],
		sourceLabel: string
	): Array<{ id: string; content: string; title: [string, string] }> => {
		const seen = new Set<string>();
		const prompts: Array<{ id: string; content: string; title: [string, string] }> = [];

		for (const item of items) {
			const text = (item ?? '').trim();
			const key = text.toLowerCase();
			if (!text || seen.has(key)) continue;
			seen.add(key);
			prompts.push({
				id: `${sourceLabel}-${prompts.length + 1}-${key.slice(0, 24)}`,
				content: text,
				title: [text, sourceLabel]
			});
			if (prompts.length >= 8) break;
		}

		return prompts;
	};

	const resolveTaskModelIds = (): { thinking: string; fallback: string } => {
		const availableIds = new Set(($_models ?? []).map((m) => m.id));
		const preferredThinkingId = (thinkingModelId ?? '').trim();

		if (preferredThinkingId && availableIds.has(preferredThinkingId)) {
			const fallbackId = preferredThinkingId.endsWith('-thinking')
				? preferredThinkingId.slice(0, -'-thinking'.length)
				: preferredThinkingId;
			return { thinking: preferredThinkingId, fallback: fallbackId || preferredThinkingId };
		}

		const currentModelId =
			(atSelectedModel?.id ??
				models[selectedModelIdx]?.id ??
				models[0]?.id ??
				selectedModels[selectedModelIdx] ??
				selectedModels[0] ??
				'') || '';

		const normalizedModelId = currentModelId.trim();
		if (!normalizedModelId) return { thinking: '', fallback: '' };

		if (normalizedModelId.endsWith('-thinking')) {
			const fallbackId = normalizedModelId.slice(0, -'-thinking'.length);
			return { thinking: normalizedModelId, fallback: fallbackId || normalizedModelId };
		}

		const thinkingCandidate = `${normalizedModelId}-thinking`;
		if (availableIds.has(thinkingCandidate) || !availableIds.size) {
			return { thinking: thinkingCandidate, fallback: normalizedModelId };
		}

		return { thinking: thinkingCandidate, fallback: normalizedModelId };
	};

	const loadHistorySuggestions = async () => {
		if (!localStorage.token) {
			historySuggestionPrompts = [];
			return;
		}

		try {
			const allChatTitles = await getChatList(localStorage.token);
			const baseTitlePrompts = buildHistoryTitleSuggestions(allChatTitles ?? [], 8);
			historySuggestionPrompts =
				baseTitlePrompts.length > 0 ? baseTitlePrompts : getHardcodedSuggestionPrompts();

			const historyTitleTexts = buildHistoryTitleSuggestions(allChatTitles ?? [], 50).map(
				(prompt) => prompt.content
			);
			if (historyTitleTexts.length === 0) return;

			const { thinking: thinkingTaskModelId, fallback: fallbackTaskModelId } = resolveTaskModelIds();
			if (!thinkingTaskModelId) return;

			let generated = await generateFollowUps(localStorage.token, thinkingTaskModelId, [
				{ role: 'user', content: buildHistorySummaryPrompt(historyTitleTexts) }
			]).catch(() => []);
			if (
				(!Array.isArray(generated) || generated.length === 0) &&
				fallbackTaskModelId &&
				fallbackTaskModelId !== thinkingTaskModelId
			) {
				generated = await generateFollowUps(localStorage.token, fallbackTaskModelId, [
					{ role: 'user', content: buildHistorySummaryPrompt(historyTitleTexts) }
				]).catch(() => []);
			}

			const generatedPrompts = toSuggestionPrompts(generated ?? [], '历史推荐');
			if (generatedPrompts.length > 0) {
				historySuggestionPrompts = generatedPrompts;
			}
		} catch (error) {
			console.error('Failed to load history title suggestions', error);
			historySuggestionPrompts = getHardcodedSuggestionPrompts();
		}
	};

	$: if (selectedModels.length > 0) {
		selectedModelIdx = models.length - 1;
	}

	$: models = selectedModels.map((id) => $_models.find((m) => m.id === id));

	onMount(async () => {
		await loadHistorySuggestions();
	});

	$: suggestionPrompts =
		historySuggestionPrompts.length > 0
			? historySuggestionPrompts
			: (atSelectedModel?.info?.meta?.suggestion_prompts ??
				models[selectedModelIdx]?.info?.meta?.suggestion_prompts ??
				$config?.default_prompt_suggestions ??
				[]);
</script>

<div class="m-auto w-full max-w-6xl px-2 @2xl:px-20 translate-y-6 py-24 text-center">
	{#if $temporaryChatEnabled}
		<Tooltip
			content={$i18n.t("This chat won't appear in history and your messages will not be saved.")}
			className="w-full flex justify-center mb-0.5"
			placement="top"
		>
			<div class="flex items-center gap-2 text-gray-500 text-base my-2 w-fit">
				<EyeSlash strokeWidth="2.5" className="size-4" />{$i18n.t('Temporary Chat')}
			</div>
		</Tooltip>
	{/if}

	<div
		class="w-full text-3xl text-gray-800 dark:text-gray-100 text-center flex items-center gap-4 font-primary"
	>
		<div class="w-full flex flex-col justify-center items-center">
			{#if $selectedFolder}
				<FolderTitle
					folder={$selectedFolder}
					onUpdate={async (folder) => {
						await chats.set(await getChatList(localStorage.token, $currentChatPage));
						currentChatPage.set(1);
					}}
					onDelete={async () => {
						await chats.set(await getChatList(localStorage.token, $currentChatPage));
						currentChatPage.set(1);

						selectedFolder.set(null);
					}}
				/>
			{:else}
				<div class="flex flex-row justify-center gap-3 @sm:gap-3.5 w-fit px-5 max-w-xl">
					<div class="flex shrink-0 justify-center">
						<div class="flex -space-x-4 mb-0.5" in:fade={{ duration: 100 }}>
							{#each models as model, modelIdx}
								<Tooltip
									content={(models[modelIdx]?.info?.meta?.tags ?? [])
										.map((tag) => tag.name.toUpperCase())
										.join(', ')}
									placement="top"
								>
									<button
										aria-hidden={models.length <= 1}
										aria-label={$i18n.t('Get information on {{name}} in the UI', {
											name: models[modelIdx]?.name
										})}
										on:click={() => {
											selectedModelIdx = modelIdx;
										}}
									>
										<img
											src={`${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model?.id}&lang=${$i18n.language}&theme=light`}
											class="h-[55px] w-[71px] @sm:h-[59px] @sm:w-[79px] rounded-md object-contain dark:hidden"
											aria-hidden="true"
											draggable="false"
										/>
										<img
											src={`${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model?.id}&lang=${$i18n.language}&theme=dark`}
											class="h-[55px] w-[71px] @sm:h-[59px] @sm:w-[79px] rounded-md object-contain hidden dark:block"
											aria-hidden="true"
											draggable="false"
										/>
									</button>
								</Tooltip>
							{/each}
						</div>
					</div>

					<div
						class=" text-3xl @sm:text-3xl line-clamp-1 flex items-center"
						in:fade={{ duration: 100 }}
					>
						{#if models[selectedModelIdx]?.name}
							<Tooltip
								content={models[selectedModelIdx]?.name}
								placement="top"
								className=" flex items-center "
							>
								<span class="line-clamp-1">
									{models[selectedModelIdx]?.name}
								</span>
							</Tooltip>
						{:else}
							{$i18n.t('Hello, {{name}}', { name: $user?.name })}
						{/if}
					</div>
				</div>

				<div class="flex mt-1 mb-2">
					<div in:fade={{ duration: 100, delay: 50 }}>
						{#if models[selectedModelIdx]?.info?.meta?.description ?? null}
							<Tooltip
								className=" w-fit"
								content={marked.parse(
									sanitizeResponseContent(
										models[selectedModelIdx]?.info?.meta?.description ?? ''
									).replaceAll('\n', '<br>')
								)}
								placement="top"
							>
								<div
									class="mt-0.5 px-2 text-sm font-normal text-gray-500 dark:text-gray-400 line-clamp-2 max-w-xl markdown"
								>
									{@html marked.parse(
										sanitizeResponseContent(
											models[selectedModelIdx]?.info?.meta?.description ?? ''
										).replaceAll('\n', '<br>')
									)}
								</div>
							</Tooltip>

							{#if models[selectedModelIdx]?.info?.meta?.user}
								<div class="mt-0.5 text-sm font-normal text-gray-400 dark:text-gray-500">
									By
									{#if models[selectedModelIdx]?.info?.meta?.user.community}
										<a
											href="https://openwebui.com/m/{models[selectedModelIdx]?.info?.meta?.user
												.username}"
											>{models[selectedModelIdx]?.info?.meta?.user.name
												? models[selectedModelIdx]?.info?.meta?.user.name
												: `@${models[selectedModelIdx]?.info?.meta?.user.username}`}</a
										>
									{:else}
										{models[selectedModelIdx]?.info?.meta?.user.name}
									{/if}
								</div>
							{/if}
						{/if}
					</div>
				</div>
			{/if}

			<div class="text-base font-normal @md:max-w-3xl w-full py-3 {atSelectedModel ? 'mt-2' : ''}">
				<MessageInput
					bind:this={messageInput}
					{history}
					{selectedModels}
					{thinkingModelId}
					bind:files
					bind:prompt
					bind:autoScroll
					bind:selectedToolIds
					{lockedToolIds}
					bind:selectedFilterIds
					bind:imageGenerationEnabled
					bind:codeInterpreterEnabled
					bind:webSearchEnabled
					bind:thinkingModeEnabled
					bind:atSelectedModel
					bind:showCommands
					bind:dragged
					{toolServers}
					{stopResponse}
					{createMessagePair}
					placeholder={$i18n.t('How can I help you today?')}
					{onChange}
					{onUpload}
					on:submit={(e) => {
						dispatch('submit', e.detail);
					}}
				/>
			</div>
		</div>
	</div>

	{#if $selectedFolder}
		<div
			class="mx-auto px-4 md:max-w-3xl md:px-6 font-primary min-h-62"
			in:fade={{ duration: 200, delay: 200 }}
		>
			<FolderPlaceholder folder={$selectedFolder} />
		</div>
	{:else}
		<div class="mx-auto max-w-2xl font-primary mt-2" in:fade={{ duration: 200, delay: 200 }}>
			<div class="mx-5">
				<Suggestions
					{suggestionPrompts}
					inputValue={prompt}
					{onSelect}
				/>
			</div>
		</div>
	{/if}
</div>
