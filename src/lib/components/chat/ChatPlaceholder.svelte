<script lang="ts">
	import { WEBUI_API_BASE_URL, WEBUI_BASE_URL } from '$lib/constants';
	import { marked } from 'marked';
	import { generateFollowUps } from '$lib/apis';
	import { getChatList } from '$lib/apis/chats';

	import { config, user, models as _models, temporaryChatEnabled } from '$lib/stores';
	import { onMount, getContext } from 'svelte';

	import { blur, fade } from 'svelte/transition';

	import Suggestions from './Suggestions.svelte';
	import { sanitizeResponseContent } from '$lib/utils';
	import {
		buildHistoryTitleSuggestions,
		getHardcodedSuggestionPrompts
	} from '$lib/utils/historySuggestions';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import EyeSlash from '$lib/components/icons/EyeSlash.svelte';

	const i18n = getContext('i18n');

	export let modelIds = [];
	export let models = [];
	export let atSelectedModel;
	export let thinkingModelId: string | null = null;

	export let onSelect = (e) => {};

	let mounted = false;
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
				modelIds[selectedModelIdx] ??
				modelIds[0] ??
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

	$: if (modelIds.length > 0) {
		selectedModelIdx = models.length - 1;
	}

	$: models = modelIds.map((id) => $_models.find((m) => m.id === id));
	$: suggestionPrompts =
		historySuggestionPrompts.length > 0
			? historySuggestionPrompts
			: (atSelectedModel?.info?.meta?.suggestion_prompts ??
				models[selectedModelIdx]?.info?.meta?.suggestion_prompts ??
				$config?.default_prompt_suggestions ??
				[]);

	onMount(() => {
		mounted = true;
		loadHistorySuggestions();
	});
</script>

{#key mounted}
	<div class="m-auto w-full max-w-6xl px-8 lg:px-20">
		<div class="flex justify-start">
			<div class="flex -space-x-4 mb-0.5" in:fade={{ duration: 200 }}>
				{#each models as model, modelIdx}
					<button
						on:click={() => {
							selectedModelIdx = modelIdx;
						}}
					>
						<Tooltip
							content={marked.parse(
								sanitizeResponseContent(
									models[selectedModelIdx]?.info?.meta?.description ?? ''
								).replaceAll('\n', '<br>')
							)}
							placement="right"
						>
							<img
								src={`${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model?.id}&lang=${$i18n.language}`}
								class="h-[2.7rem] w-[3.5rem] rounded-md object-contain"
								alt="logo"
								draggable="false"
							/>
						</Tooltip>
					</button>
				{/each}
			</div>
		</div>

		{#if $temporaryChatEnabled}
			<Tooltip
				content={$i18n.t("This chat won't appear in history and your messages will not be saved.")}
				className="w-full flex justify-start mb-0.5"
				placement="top"
			>
				<div class="flex items-center gap-2 text-gray-500 text-lg mt-2 w-fit">
					<EyeSlash strokeWidth="2.5" className="size-5" />{$i18n.t('Temporary Chat')}
				</div>
			</Tooltip>
		{/if}

		<div
			class=" mt-2 mb-4 text-3xl text-gray-800 dark:text-gray-100 text-left flex items-center gap-4 font-primary"
		>
			<div>
				<div class=" capitalize line-clamp-1" in:fade={{ duration: 200 }}>
					{#if models[selectedModelIdx]?.name}
						{models[selectedModelIdx]?.name}
					{:else}
						{$i18n.t('Hello, {{name}}', { name: $user?.name })}
					{/if}
				</div>

				<div in:fade={{ duration: 200, delay: 200 }}>
					{#if models[selectedModelIdx]?.info?.meta?.description ?? null}
						<div
							class="mt-0.5 text-base font-normal text-gray-500 dark:text-gray-400 line-clamp-3 markdown"
						>
							{@html marked.parse(
								sanitizeResponseContent(
									models[selectedModelIdx]?.info?.meta?.description
								).replaceAll('\n', '<br>')
							)}
						</div>
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
					{:else}
						<div class=" text-gray-400 dark:text-gray-500 line-clamp-1 font-p">
							{$i18n.t('How can I help you today?')}
						</div>
					{/if}
				</div>
			</div>
		</div>

		<div class=" w-full font-primary" in:fade={{ duration: 200, delay: 300 }}>
			<Suggestions
				className="grid grid-cols-2"
				{suggestionPrompts}
				{onSelect}
			/>
		</div>
	</div>
{/key}
