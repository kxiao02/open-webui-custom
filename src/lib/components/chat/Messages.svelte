<script lang="ts">
	import { v4 as uuidv4 } from 'uuid';
	import {
		chats,
		config,
		settings,
		user as _user,
		mobile,
		currentChatPage,
		temporaryChatEnabled
	} from '$lib/stores';
	import { tick, getContext, onMount, onDestroy, createEventDispatcher } from 'svelte';
	const dispatch = createEventDispatcher();

	import { toast } from 'svelte-sonner';
	import { getChatList, updateChatById } from '$lib/apis/chats';
	import { copyToClipboard, extractCurlyBraceWords } from '$lib/utils';

	import Message from './Messages/Message.svelte';
	import Loader from '../common/Loader.svelte';
	import Spinner from '../common/Spinner.svelte';

	import ChatPlaceholder from './ChatPlaceholder.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	export let className = 'h-full flex pt-8';

	export let chatId = '';
	export let user: any = $_user;

	export let prompt: any = null;
	export let history: any = {};
	export let historyMeta: any = null;
	export let selectedModels: any[] = [];
	export let atSelectedModel: any = undefined;
	export let processing = '';

	export let messages: any[] = [];

	export let setInputText: Function = () => {};

	export let sendMessage: Function = () => {};
	export let continueResponse: Function = () => {};
	export let regenerateResponse: Function = () => {};
	export let mergeResponses: Function = () => {};

	export let chatActionHandler: Function = () => {};
	export let showMessage: Function = () => {};
	export let submitMessage: Function = () => {};
	export let addMessages: Function = () => {};

	export let readOnly = false;
	export let editCodeBlock = true;

	export let topPadding = false;
	export let bottomPadding = false;
	export let autoScroll: boolean = true;

	export let onSelect = (e: any) => {};

	export let messagesCount: number | null = 20;
	export let loadMoreHistory: Function | null = null;
	export let ensureHistoryLoaded: Function = async () => true;
	let messagesLoading = false;
	const INITIAL_VISIBLE_MESSAGES = 4;
	const VISIBLE_MESSAGE_STEP = 4;
	let visibleMessageCount = 0;
	let renderedMessages: any[] = [];
	let pendingVisibleReveal: number | null = null;
	let revealRunId = 0;

	const usesExternalHistoryPagination = () => typeof loadMoreHistory === 'function';
	const usesLocalWindowing = () => messagesCount !== null && !usesExternalHistoryPagination();

	const canLoadMoreHistory = () =>
		Boolean(
			historyMeta?.canLoadMore ??
				historyMeta?.can_load_more ??
				historyMeta?.truncated ??
				historyMeta?.is_truncated ??
				false
		);

	const loadMoreMessages = async () => {
		const element = document.getElementById('messages-container');
		if (!element) return;
		const previousMessageCount = messages.length;
		const previousVisibleMessageCount = visibleMessageCount;
		const previousScrollTop = element.scrollTop;
		const previousScrollHeight = element.scrollHeight;

		messagesLoading = true;
		if (canLoadMoreHistory() && typeof loadMoreHistory === 'function') {
			await loadMoreHistory();
		}
		if (usesLocalWindowing() && messagesCount !== null) {
			messagesCount += 20;
		}
		buildMessages();

		if (messages.length > previousMessageCount) {
			cancelVisibleReveal();
			revealRunId += 1;
			if (usesExternalHistoryPagination()) {
				visibleMessageCount = messages.length;
			} else {
				const addedMessages = messages.length - previousMessageCount;
				visibleMessageCount = Math.min(
					messages.length,
					Math.max(previousVisibleMessageCount + addedMessages, INITIAL_VISIBLE_MESSAGES)
				);
			}
		}

		await tick();
		if (messages.length > previousMessageCount) {
			const heightDelta = element.scrollHeight - previousScrollHeight;
			element.scrollTop = Math.max(0, previousScrollTop + heightDelta);
		}

		messagesLoading = false;
	};

	let pendingRebuild: number | null = null;
	let lastCurrentId: any = null;

	const cancelVisibleReveal = () => {
		if (pendingVisibleReveal) {
			cancelAnimationFrame(pendingVisibleReveal);
			pendingVisibleReveal = null;
		}
	};

	const scheduleVisibleReveal = () => {
		cancelVisibleReveal();

		const runId = ++revealRunId;
		const revealNextBatch = async () => {
			if (runId !== revealRunId || visibleMessageCount >= messages.length) {
				pendingVisibleReveal = null;
				return;
			}

			const element = document.getElementById('messages-container');
			const previousBottomOffset = element ? element.scrollHeight - element.scrollTop : 0;

			visibleMessageCount = Math.min(messages.length, visibleMessageCount + VISIBLE_MESSAGE_STEP);
			await tick();

			if (runId !== revealRunId) {
				pendingVisibleReveal = null;
				return;
			}

			if (element) {
				element.scrollTop = Math.max(0, element.scrollHeight - previousBottomOffset);
			}

			if (visibleMessageCount < messages.length) {
				pendingVisibleReveal = requestAnimationFrame(() => {
					void revealNextBatch();
				});
			} else {
				pendingVisibleReveal = null;
			}
		};

		if (visibleMessageCount < messages.length) {
			pendingVisibleReveal = requestAnimationFrame(() => {
				void revealNextBatch();
			});
		}
	};

	const resetVisibleMessages = () => {
		cancelVisibleReveal();
		revealRunId += 1;

		if (usesExternalHistoryPagination()) {
			visibleMessageCount = messages.length;
			return;
		}

		if (messages.length <= INITIAL_VISIBLE_MESSAGES) {
			visibleMessageCount = messages.length;
			return;
		}

		visibleMessageCount = INITIAL_VISIBLE_MESSAGES;
		scheduleVisibleReveal();
	};

	const buildMessages = () => {
		let _messages: any[] = [];
		const windowSize = usesLocalWindowing() ? messagesCount : null;

		let message = history.messages[history.currentId];
		const visitedMessageIds = new Set();

		while (message && (windowSize !== null ? _messages.length <= windowSize : true)) {
			if (visitedMessageIds.has(message.id)) {
				console.warn('Circular dependency detected in message history', message.id);
				break;
			}
			visitedMessageIds.add(message.id);

			_messages.push(message);
			message = message.parentId !== null ? history.messages[message.parentId] : null;
		}

		messages = _messages.reverse();
	};

	// Throttle message list rebuilds to once per animation frame during streaming.
	// Structural changes (currentId change) always rebuild immediately.
	const handleHistoryChange = (currentId: any, _messages: any) => {
		if (!currentId) {
			messages = [];
			visibleMessageCount = 0;
			renderedMessages = [];
			cancelVisibleReveal();
			return;
		}

		const currentIdChanged = currentId !== lastCurrentId;
		lastCurrentId = currentId;

		if (currentIdChanged) {
			// Structural change: new chat, navigation, new message — rebuild immediately
			if (pendingRebuild !== null) {
				cancelAnimationFrame(pendingRebuild);
			}
			pendingRebuild = null;
			buildMessages();
			resetVisibleMessages();
		} else if (_messages) {
			// Content update (streaming) — throttle to once per frame
			if (!pendingRebuild) {
				pendingRebuild = requestAnimationFrame(() => {
					pendingRebuild = null;
					buildMessages();
				});
			}
		}
	};

	$: handleHistoryChange(history.currentId, history.messages);
	$: renderedMessages =
		visibleMessageCount >= messages.length
			? messages
			: messages.slice(Math.max(messages.length - visibleMessageCount, 0));

	$: if (autoScroll && bottomPadding) {
		(async () => {
			await tick();
			scrollToBottom();
		})();
	}

	const scrollToBottom = () => {
		const element = document.getElementById('messages-container');
		if (!element) return;
		element.scrollTop = element.scrollHeight;
	};

	const hasMoreLocalHistory = () => {
		const first = messages.at(0);
		if (!first) return false;
		const parentId = first.parentId;
		if (parentId === null || parentId === undefined) return false;
		return Boolean(history?.messages?.[parentId]);
	};

	const shouldShowHistoryLoader = () => hasMoreLocalHistory() || canLoadMoreHistory();

	const updateChat = async () => {
		if (!$temporaryChatEnabled) {
			await ensureHistoryLoaded();
			history = history;
			await tick();
			await updateChatById(localStorage.token, chatId, {
				history: history,
				messages: messages
			});

			currentChatPage.set(1);
			await chats.set(await getChatList(localStorage.token, $currentChatPage));
		}
	};

	const gotoMessage = async (message: any, idx: number) => {
		// Determine the correct sibling list (either parent's children or root messages)
		let siblings;
		if (message.parentId !== null) {
			siblings = history.messages[message.parentId].childrenIds;
		} else {
			siblings = Object.values(history.messages)
				.filter((msg) => msg.parentId === null)
				.map((msg) => msg.id);
		}

		// Clamp index to a valid range
		idx = Math.max(0, Math.min(idx, siblings.length - 1));

		let messageId = siblings[idx];

		// If we're navigating to a different message
		if (message.id !== messageId) {
			// Drill down to the deepest child of that branch
			let messageChildrenIds = history.messages[messageId].childrenIds;
			while (messageChildrenIds.length !== 0) {
				messageId = messageChildrenIds.at(-1);
				messageChildrenIds = history.messages[messageId].childrenIds;
			}

			history.currentId = messageId;
		}

		await tick();

		// Optional auto-scroll
		if ($settings?.scrollOnBranchChange ?? true) {
			const element = document.getElementById('messages-container');
			autoScroll = element.scrollHeight - element.scrollTop <= element.clientHeight + 50;

			setTimeout(() => {
				scrollToBottom();
			}, 100);
		}
	};

	const showPreviousMessage = async (message: any) => {
		if (message.parentId !== null) {
			let messageId =
				history.messages[message.parentId].childrenIds[
					Math.max(history.messages[message.parentId].childrenIds.indexOf(message.id) - 1, 0)
				];

			if (message.id !== messageId) {
				let messageChildrenIds = history.messages[messageId].childrenIds;

				while (messageChildrenIds.length !== 0) {
					messageId = messageChildrenIds.at(-1);
					messageChildrenIds = history.messages[messageId].childrenIds;
				}

				history.currentId = messageId;
			}
		} else {
			let childrenIds = Object.values(history.messages)
				.filter((message) => message.parentId === null)
				.map((message) => message.id);
			let messageId = childrenIds[Math.max(childrenIds.indexOf(message.id) - 1, 0)];

			if (message.id !== messageId) {
				let messageChildrenIds = history.messages[messageId].childrenIds;

				while (messageChildrenIds.length !== 0) {
					messageId = messageChildrenIds.at(-1);
					messageChildrenIds = history.messages[messageId].childrenIds;
				}

				history.currentId = messageId;
			}
		}

		await tick();

		if ($settings?.scrollOnBranchChange ?? true) {
			const element = document.getElementById('messages-container');
			autoScroll = element.scrollHeight - element.scrollTop <= element.clientHeight + 50;

			setTimeout(() => {
				scrollToBottom();
			}, 100);
		}
	};

	const showNextMessage = async (message) => {
		if (message.parentId !== null) {
			let messageId =
				history.messages[message.parentId].childrenIds[
					Math.min(
						history.messages[message.parentId].childrenIds.indexOf(message.id) + 1,
						history.messages[message.parentId].childrenIds.length - 1
					)
				];

			if (message.id !== messageId) {
				let messageChildrenIds = history.messages[messageId].childrenIds;

				while (messageChildrenIds.length !== 0) {
					messageId = messageChildrenIds.at(-1);
					messageChildrenIds = history.messages[messageId].childrenIds;
				}

				history.currentId = messageId;
			}
		} else {
			let childrenIds = Object.values(history.messages)
				.filter((message) => message.parentId === null)
				.map((message) => message.id);
			let messageId =
				childrenIds[Math.min(childrenIds.indexOf(message.id) + 1, childrenIds.length - 1)];

			if (message.id !== messageId) {
				let messageChildrenIds = history.messages[messageId].childrenIds;

				while (messageChildrenIds.length !== 0) {
					messageId = messageChildrenIds.at(-1);
					messageChildrenIds = history.messages[messageId].childrenIds;
				}

				history.currentId = messageId;
			}
		}

		await tick();

		if ($settings?.scrollOnBranchChange ?? true) {
			const element = document.getElementById('messages-container');
			autoScroll = element.scrollHeight - element.scrollTop <= element.clientHeight + 50;

			setTimeout(() => {
				scrollToBottom();
			}, 100);
		}
	};

	const rateMessage = async (messageId, rating) => {
		history.messages[messageId].annotation = {
			...history.messages[messageId].annotation,
			rating: rating
		};

		await updateChat();
	};

	const editMessage = async (messageId, { content, files }, submit = true) => {
		if ((selectedModels ?? []).filter((id) => id).length === 0) {
			toast.error($i18n.t('Model not selected'));
			return;
		}
		if (history.messages[messageId].role === 'user') {
			if (submit) {
				// New user message
				let userPrompt = content;
				let userMessageId = uuidv4();

				let userMessage = {
					id: userMessageId,
					parentId: history.messages[messageId].parentId,
					childrenIds: [],
					role: 'user',
					content: userPrompt,
					...(files && { files: files }),
					models: selectedModels,
					timestamp: Math.floor(Date.now() / 1000) // Unix epoch
				};

				let messageParentId = history.messages[messageId].parentId;

				if (messageParentId !== null) {
					history.messages[messageParentId].childrenIds = [
						...history.messages[messageParentId].childrenIds,
						userMessageId
					];
				}

				history.messages[userMessageId] = userMessage;
				history.currentId = userMessageId;

				await tick();
				await sendMessage(history, userMessageId);
			} else {
				// Edit user message
				history.messages[messageId].content = content;
				history.messages[messageId].files = files;
				await updateChat();
			}
		} else {
			if (submit) {
				// New response message
				const responseMessageId = uuidv4();
				const message = history.messages[messageId];
				const parentId = message.parentId;

				const responseMessage = {
					...message,
					id: responseMessageId,
					parentId: parentId,
					childrenIds: [],
					files: undefined,
					content: content,
					timestamp: Math.floor(Date.now() / 1000) // Unix epoch
				};

				history.messages[responseMessageId] = responseMessage;
				history.currentId = responseMessageId;

				// Append messageId to childrenIds of parent message
				if (parentId !== null) {
					history.messages[parentId].childrenIds = [
						...history.messages[parentId].childrenIds,
						responseMessageId
					];
				}

				await updateChat();
			} else {
				// Edit response message
				history.messages[messageId].originalContent = history.messages[messageId].content;
				history.messages[messageId].content = content;
				await updateChat();
			}
		}
	};

	const actionMessage = async (actionId, message, event = null) => {
		await chatActionHandler(chatId, actionId, message.model, message.id, event);
	};

	const saveMessage = async (messageId, message) => {
		if (!history.messages?.[messageId]) {
			return;
		}

		history.messages[messageId] = message;
		await updateChat();
	};

	const deleteMessage = async (messageId) => {
		const messageToDelete = history.messages[messageId];
		const parentMessageId = messageToDelete.parentId;
		const childMessageIds = messageToDelete.childrenIds ?? [];

		// Collect all grandchildren
		const grandchildrenIds = childMessageIds.flatMap(
			(childId) => history.messages[childId]?.childrenIds ?? []
		);

		// Update parent's children
		if (parentMessageId && history.messages[parentMessageId]) {
			history.messages[parentMessageId].childrenIds = [
				...history.messages[parentMessageId].childrenIds.filter((id) => id !== messageId),
				...grandchildrenIds
			];
		}

		// Update grandchildren's parent
		grandchildrenIds.forEach((grandchildId) => {
			if (history.messages[grandchildId]) {
				history.messages[grandchildId].parentId = parentMessageId;
			}
		});

		// Delete the message and its children
		[messageId, ...childMessageIds].forEach((id) => {
			delete history.messages[id];
		});

		showMessage({ id: parentMessageId }, false);
	};

	onDestroy(() => {
		if (pendingRebuild !== null) {
			cancelAnimationFrame(pendingRebuild);
		}
		cancelVisibleReveal();
	});

	const triggerScroll = () => {
		if (autoScroll) {
			const element = document.getElementById('messages-container');
			autoScroll = element.scrollHeight - element.scrollTop <= element.clientHeight + 50;
			setTimeout(() => {
				scrollToBottom();
			}, 100);
		}
	};
</script>

<div class={className}>
	{#if Object.keys(history?.messages ?? {}).length == 0}
		<ChatPlaceholder modelIds={selectedModels} {atSelectedModel} {onSelect} />
	{:else}
		<div class="w-full pt-2">
			{#key chatId}
				<section class="w-full" aria-labelledby="chat-conversation">
					<h2 class="sr-only" id="chat-conversation">{$i18n.t('Chat Conversation')}</h2>
					{#if shouldShowHistoryLoader()}
						<Loader
							on:visible={(e) => {
								if (!messagesLoading) {
									loadMoreMessages();
								}
							}}
						>
							<div class="w-full flex justify-center py-1 text-xs animate-pulse items-center gap-2">
								<Spinner className=" size-4" />
								<div class=" ">{$i18n.t('Loading...')}</div>
							</div>
						</Loader>
					{/if}
					<ul role="log" aria-live="polite" aria-relevant="additions" aria-atomic="false">
						{#each renderedMessages as message, messageIdx (message.id)}
							<Message
								{chatId}
								bind:history
								{selectedModels}
								messageId={message.id}
								idx={messageIdx}
								{user}
								{setInputText}
								{gotoMessage}
								{showPreviousMessage}
								{showNextMessage}
								{updateChat}
								{editMessage}
								{deleteMessage}
								{rateMessage}
								{actionMessage}
								{saveMessage}
								{submitMessage}
								{regenerateResponse}
								{continueResponse}
								{mergeResponses}
								{addMessages}
								{triggerScroll}
								{readOnly}
								{editCodeBlock}
								{topPadding}
							/>
						{/each}
					</ul>
				</section>
				<div class="pb-18" />
				{#if bottomPadding}
					<div class="  pb-6" />
				{/if}
			{/key}
		</div>
	{/if}
</div>
