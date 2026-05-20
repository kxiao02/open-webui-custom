<script lang="ts">
	import { v4 as uuidv4 } from 'uuid';
	import { toast } from 'svelte-sonner';
	import { PaneGroup, Pane, PaneResizer } from 'paneforge';

	import { getContext, onDestroy, onMount, tick } from 'svelte';
	import { fade } from 'svelte/transition';
	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	import { goto } from '$app/navigation';
	import { page } from '$app/stores';

	import { type Unsubscriber, type Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { WEBUI_BASE_URL } from '$lib/constants';

	import {
		chatId,
		chats,
		config,
		type Model,
		models,
		tags as allTags,
		settings,
		showSidebar,
		WEBUI_NAME,
		banners,
		user,
		socket,
		audioQueue,
		showControls,
		showCallOverlay,
		currentChatPage,
		temporaryChatEnabled,
		mobile,
		chatTitle,
		showArtifacts,
		artifactContents,
		tools,
		skills,
		toolServers,
		terminalServers,
		functions,
		selectedFolder,
		pinnedChats,
		showEmbeds,
		showFilePreview,
		showOverview,
		selectedGeneratedFilePreviewId,
		selectedTerminalId,
		showFileNavPath,
		showFileNavDir,
		activeChatIds
	} from '$lib/stores';

	import { WEBUI_API_BASE_URL } from '$lib/constants';

	import {
		convertMessagesToHistory,
		copyToClipboard,
		getMessageContentParts,
		createMessagesList,
		getPromptVariables,
		processDetails,
		removeDetails,
		removeAllDetails,
		getCodeBlockContents,
		isYoutubeUrl,
		displayFileHandler
	} from '$lib/utils';
	import { getClientCapabilities } from '$lib/utils/client-capabilities';
	import { AudioQueue } from '$lib/utils/audio';
	import {
		collectGeneratedFilesFromResponseOutput,
		normalizeKnowflowAssetUrl,
		normalizeVisualUrlForMatching,
		normalizeToolResponseOutput,
		normalizeToolCallContent
	} from '$lib/utils/generated-files';

	import {
		archiveChatById,
		createNewChat,
		getAllTags,
		getChatById,
		getChatList,
		getPinnedChatList,
		getTagsById,
		updateChatSessionCapabilities,
		updateChatById,
		updateChatFolderIdById
	} from '$lib/apis/chats';
	import { generateOpenAIChatCompletion } from '$lib/apis/openai';
	import { processWeb, processWebSearch, processYoutubeVideo } from '$lib/apis/retrieval';
	import { getAndUpdateUserLocation, getUserSettings } from '$lib/apis/users';
	import {
		chatCompleted,
		generateQueries,
		chatAction,
		generateMoACompletion,
		stopTask,
		getTaskIdsByChatId
	} from '$lib/apis';
	import { getSkills, installSkillById } from '$lib/apis/skills';
	import { getTools, installToolById } from '$lib/apis/tools';
	import { uploadFile } from '$lib/apis/files';
	import { createOpenAITextStream } from '$lib/apis/streaming';
	import { getFunctions } from '$lib/apis/functions';
	import { updateFolderById } from '$lib/apis/folders';

	import Banner from '../common/Banner.svelte';
	import MessageInput from '$lib/components/chat/MessageInput.svelte';
	import Messages from '$lib/components/chat/Messages.svelte';
	import Navbar from '$lib/components/chat/Navbar.svelte';
	import ChatControls from './ChatControls.svelte';
	import CapabilityInstallDialog from './CapabilityInstallDialog.svelte';
	import EventConfirmDialog from '../common/ConfirmDialog.svelte';
	import Placeholder from './Placeholder.svelte';
	import LandingAmbientBackground from './LandingAmbientBackground.svelte';
	import FilesOverlay from './MessageInput/FilesOverlay.svelte';
	import NotificationToast from '../NotificationToast.svelte';
	import Spinner from '../common/Spinner.svelte';
	import Tooltip from '../common/Tooltip.svelte';
	import Sidebar from '../icons/Sidebar.svelte';
	import Image from '../common/Image.svelte';
	import { getBanners } from '$lib/apis/configs';

	export let chatIdProp = '';

	let loading = true;
	let initNewChatRunId = 0;

	type HistoryMeta = {
		truncated: boolean;
		totalMessages: number | null;
		canLoadMore: boolean;
		windowSize: number | null;
	};

	const HISTORY_TAIL_DEFAULT = 120;
	const HISTORY_TAIL_STEP = 120;

	const eventTarget = new EventTarget();
	let controlPane: Pane | undefined;
	let controlPaneComponent: ChatControls | undefined;

	let messageInput: MessageInput | undefined;

	let autoScroll = true;
	let processing = '';
	let messagesContainerElement: HTMLDivElement;

	let navbarElement;

	let showEventConfirmation = false;
	let eventConfirmationTitle = '';
	let eventConfirmationMessage = '';
	let eventConfirmationInput = false;
	let eventConfirmationInputPlaceholder = '';
	let eventConfirmationInputValue = '';
	let eventConfirmationInputType = '';
	let eventCallback: ((value?: string) => unknown) | null = null;

	type CapabilityInstallRequest = {
		resource_type: 'tool' | 'skill';
		resource_id: string;
		name?: string;
		title?: string;
		message?: string;
		install_label?: string;
		session_label?: string;
		cancel_label?: string;
	};

	let showCapabilityInstallDialog = false;
	let capabilityInstallRequest: CapabilityInstallRequest | null = null;

	let selectedModels: string[] = [''];
	let atSelectedModel: Model | undefined;
	let selectedModelIds: string[] = [];
	$: if (atSelectedModel !== undefined) {
		selectedModelIds = [atSelectedModel.id];
	} else {
		selectedModelIds = selectedModels;
	}

	const DEFAULT_THINKING_MODE_ENABLED = true;
	const normalizeThinkingModeEnabled = (value: unknown): boolean => {
		if (typeof value === 'boolean') {
			return value;
		}

		if (typeof value === 'string') {
			const normalizedValue = value.trim().toLowerCase();
			if (normalizedValue === 'true') {
				return true;
			}
			if (normalizedValue === 'false') {
				return false;
			}
		}

		return DEFAULT_THINKING_MODE_ENABLED;
	};

	let thinkingModeEnabled = DEFAULT_THINKING_MODE_ENABLED;

	let thinkingModelId: string | null = null;
	$: thinkingModelId = selectedModelIds[0] ?? null;

	let selectedToolIds: string[] = [];
	let lockedToolIds: string[] = [];
	let selectedFilterIds: string[] = [];
	let sessionToolIds: string[] = [];
	let sessionSkillIds: string[] = [];

	let imageGenerationEnabled = false;
	let webSearchEnabled = false;
	let codeInterpreterEnabled = false;

	let showCommands = false;

	let generating = false;
	let dragged = false;
	let generationController: AbortController | null = null;

	let chat: any = null;
	let tags: any[] = [];

	let history: any = {
		messages: {},
		currentId: null
	};
	let historyMeta: HistoryMeta = {
		truncated: false,
		totalMessages: null,
		canLoadMore: false,
		windowSize: null
	};
	let historyFullLoadPromise: Promise<boolean> | null = null;
	let loadingHistoryMore = false;
	let pendingArtifactRefresh = false;
	let pendingTaskIdsLoad: Promise<string[]> | null = null;
	let lastTaskRefreshAt = 0;

	let taskIds: any[] | null = null;
	type PendingChatCompletion = {
		chatId: string;
		messageId: string;
	};
	let pendingChatCompletion: PendingChatCompletion | null = null;

	// Chat Input
	let prompt = '';
	let chatFiles: any[] = [];
	let files: any[] = [];
	let params: Record<string, any> = {};
	let messageCount = 0;
	let lastMessageCountId: string | null = null;
	let hasCustomBackground = false;
	let showLandingAmbientBackground = false;

	type ChatRequestContext = {
		thinkingModeEnabled: boolean;
		selectedToolIds: string[];
		selectedFilterIds: string[];
		sessionSkillIds: string[];
		selectedTerminalId: string | number | null;
		finalAnswerOnly: boolean;
		featureToggles: {
			imageGenerationEnabled: boolean;
			webSearchEnabled: boolean;
			codeInterpreterEnabled: boolean;
		};
	};

	type QueuedMessage = {
		id: string;
		prompt: string;
		files: any[];
		requestContext: ChatRequestContext;
	};

	const responseRequestContexts = new Map<string, ChatRequestContext>();

	const updateMessageCount = (currentId: string | null) => {
		if (!currentId) {
			lastMessageCountId = null;
			const messages = history?.messages;
			messageCount = messages && typeof messages === 'object' ? Object.keys(messages).length : 0;
			return;
		}

		if (currentId === lastMessageCountId) {
			return;
		}

		lastMessageCountId = currentId;
		messageCount = createMessagesList(history, currentId).length;
	};

	$: updateMessageCount(history?.currentId ?? null);
	$: hasCustomBackground = Boolean(
		$selectedFolder?.meta?.background_image_url ||
			($settings?.backgroundImageUrl ?? $config?.license_metadata?.background_image_url ?? null)
	);
	$: showLandingAmbientBackground =
		$page.url.pathname === '/' && !chatIdProp && messageCount === 0 && !hasCustomBackground;

	const normalizeFileRef = (value: unknown): string | null => {
		if (typeof value !== 'string') {
			return null;
		}
		const normalized = value.trim();
		if (normalized === '') {
			return null;
		}
		const lowered = normalized.toLowerCase();
		if (lowered === 'null' || lowered === 'undefined') {
			return null;
		}
		return normalized;
	};

	const normalizeConversationId = (value: unknown): string | null => {
		if (typeof value !== 'string') {
			return null;
		}
		const normalized = value.trim();
		if (normalized === '' || normalized === 'null' || normalized === 'undefined') {
			return null;
		}
		return normalized;
	};

	const getMessageConversationId = (message: any): string | null =>
		normalizeConversationId(message?.conversationId ?? message?.conversation_id);

	const resolveResponseConversationId = ({
		historyData,
		chatId,
		userMessageId,
		responseMessageId,
		forkBranch
	}: {
		historyData: any;
		chatId: string | null | undefined;
		userMessageId: string | null | undefined;
		responseMessageId: string;
		forkBranch: boolean;
	}): string | null => {
		const normalizedUserMessageId = normalizeConversationId(userMessageId);
		const userMessage = normalizedUserMessageId ? historyData?.messages?.[normalizedUserMessageId] : null;
		const parentAssistantId = normalizeConversationId(userMessage?.parentId);
		const parentAssistant = parentAssistantId ? historyData?.messages?.[parentAssistantId] : null;
		const baseConversationId =
			getMessageConversationId(parentAssistant) ?? normalizeConversationId(chatId);

		if (!baseConversationId) {
			return null;
		}

		if (!forkBranch) {
			return baseConversationId;
		}

		// Multi-response fan-out must fork the backend thread identity so
		// concurrent assistant branches do not collide on the same bridge run lock.
		return `${baseConversationId}::branch::${responseMessageId}`;
	};

	const extractKnowflowAssetRef = (value: unknown): string | null => {
		if (typeof value !== 'string') {
			return null;
		}

		const normalized = value.trim();
		if (!normalized || normalized.startsWith('data:') || normalized.startsWith('blob:')) {
			return null;
		}

		let path = normalized;
		try {
			const parsed = new URL(
				normalized,
				typeof window !== 'undefined' ? window.location.origin : WEBUI_BASE_URL
			);
			path = parsed.pathname || normalized;
		} catch {
			path = normalized;
		}

		if (!path.startsWith('/')) {
			path = `/${path.replace(/^\/+/, '')}`;
		}

		const match = path.match(/\/(?:openai\/)?minio\/[^?#]+/i);
		if (!match) {
			return null;
		}

		return match[0].replace(/^\/openai(?=\/minio\/)/i, '');
	};

	const sanitizeFilesList = (fileList: any[] = []) => {
		if (!Array.isArray(fileList)) {
			return [];
		}

		const extractFileDownloadRef = (file: any): string | null =>
			normalizeKnowflowAssetUrl(file?.bridge_url) ??
			normalizeKnowflowAssetUrl(file?.generated_file_url) ??
			normalizeKnowflowAssetUrl(file?.download_url) ??
			normalizeKnowflowAssetUrl(file?.downloadUrl) ??
			normalizeKnowflowAssetUrl(file?.url);

		const extractFileIdRef = (file: any): string | null =>
			normalizeKnowflowAssetUrl(file?.id) ??
			normalizeKnowflowAssetUrl(file?.bridge_file_id) ??
			normalizeKnowflowAssetUrl(file?.file_id) ??
			normalizeKnowflowAssetUrl(file?.fileId);

		const extractFilePathRef = (file: any): string | null =>
			normalizeFileRef(file?.path) ??
			normalizeFileRef(file?.output_path) ??
			normalizeFileRef(file?.target_path) ??
			normalizeFileRef(file?.file_path);

		const scoreFileLabel = (value: unknown): number => {
			if (typeof value !== 'string') return 0;
			const normalized = value.trim();
			if (!normalized) return 0;
			if (['generated-file', 'knowledge visual', 'knowflow', 'image', 'file'].includes(normalized.toLowerCase())) {
				return 1;
			}
			return normalized.length;
		};

		const scoreFileRef = (value: unknown): number => {
			const normalized = normalizeKnowflowAssetUrl(value)?.toLowerCase() ?? '';
			if (!normalized) return 0;
			if (normalized.includes('/api/v1/files/') && normalized.includes('/content')) return 5;
			if (normalized.includes('/api/v1/files/')) return 4;
			if (extractKnowflowAssetRef(normalized)) return 3;
			if (normalized.startsWith('https://') || normalized.startsWith('http://')) return 2;
			return 0;
		};

		const buildFileDedupeKey = (file: any): string => {
			const pathRef = extractFilePathRef(file);
			if (pathRef) {
				return `path::${pathRef}`;
			}

			const assetRef = extractKnowflowAssetRef(
				extractFileDownloadRef(file) ?? extractFileIdRef(file)
			);
			if (assetRef) {
				return `asset::${assetRef}`;
			}

			return [
				extractFileDownloadRef(file) ?? '',
				extractFileIdRef(file) ?? '',
				typeof file?.name === 'string' ? file.name.trim() : '',
				typeof file?.filename === 'string' ? file.filename.trim() : '',
				typeof file?.fileName === 'string' ? file.fileName.trim() : '',
				typeof file?.content_type === 'string' ? file.content_type.trim() : '',
				file?.size ?? file?.size_bytes ?? ''
			].join('::');
		};

		const mergeDuplicateFiles = (existing: any, incoming: any) => {
			const existingNameScore = Math.max(
				scoreFileLabel(existing?.name),
				scoreFileLabel(existing?.filename),
				scoreFileLabel(existing?.fileName)
			);
			const incomingNameScore = Math.max(
				scoreFileLabel(incoming?.name),
				scoreFileLabel(incoming?.filename),
				scoreFileLabel(incoming?.fileName)
			);
			const existingDownloadRef = extractFileDownloadRef(existing);
			const incomingDownloadRef = extractFileDownloadRef(incoming);
			const preferredDownloadRef =
				scoreFileRef(existingDownloadRef) >= scoreFileRef(incomingDownloadRef)
					? (existingDownloadRef ?? incomingDownloadRef)
					: (incomingDownloadRef ?? existingDownloadRef);
			const preferredId = extractFileIdRef(incoming) ?? extractFileIdRef(existing);
			const preferredPath = extractFilePathRef(incoming) ?? extractFilePathRef(existing);

			return {
				...existing,
				...incoming,
				url: preferredDownloadRef ?? incoming?.url ?? existing?.url,
				id: preferredId ?? incoming?.id ?? existing?.id,
				path: preferredPath ?? incoming?.path ?? existing?.path,
				output_path: incoming?.output_path ?? existing?.output_path ?? preferredPath,
				name:
					incomingNameScore >= existingNameScore
						? (incoming?.name ?? incoming?.filename ?? incoming?.fileName ?? existing?.name)
						: (existing?.name ?? existing?.filename ?? existing?.fileName),
				filename:
					incomingNameScore >= existingNameScore
						? (incoming?.filename ?? incoming?.name ?? existing?.filename)
						: (existing?.filename ?? existing?.name),
				fileName:
					incomingNameScore >= existingNameScore
						? (incoming?.fileName ?? incoming?.filename ?? incoming?.name ?? existing?.fileName)
						: (existing?.fileName ?? existing?.filename ?? existing?.name)
			};
		};

		const sanitizedFiles = fileList
			.map((file) => {
				if (!file || typeof file !== 'object') {
					return null;
				}

				const downloadRef = extractFileDownloadRef(file);
				const normalizedId = extractFileIdRef(file);
				const pathRef = extractFilePathRef(file);
				const fileRef = downloadRef ?? normalizedId ?? pathRef;
				const hasInlineContent = typeof file?.content === 'string' && file.content.trim() !== '';

				if (!fileRef && !hasInlineContent) {
					return null;
				}

				const sanitized = { ...file };

				if (downloadRef) {
					sanitized.url = downloadRef;
					if (pathRef) {
						sanitized.path = pathRef;
						sanitized.output_path = sanitized.output_path ?? pathRef;
					}
					if (normalizedId) {
						sanitized.id = normalizedId;
					} else if (!downloadRef.startsWith('http') && !downloadRef.startsWith('data:')) {
						sanitized.id = downloadRef;
					}
				} else if (normalizedId) {
					sanitized.url = normalizedId;
					sanitized.id = normalizedId;
					if (pathRef) {
						sanitized.path = pathRef;
						sanitized.output_path = sanitized.output_path ?? pathRef;
					}
				} else if (pathRef) {
					sanitized.path = pathRef;
					sanitized.id = pathRef;
					delete sanitized.url;
				} else {
					delete sanitized.url;
					delete sanitized.id;
				}

				return sanitized;
			})
			.filter(Boolean);

		const dedupedFiles: any[] = [];
		const dedupedIndexByKey = new Map<string, number>();

		for (const file of sanitizedFiles) {
			const dedupeKey = buildFileDedupeKey(file);
			const existingIndex = dedupedIndexByKey.get(dedupeKey);
			if (existingIndex === undefined) {
				dedupedIndexByKey.set(dedupeKey, dedupedFiles.length);
				dedupedFiles.push(file);
				continue;
			}
			dedupedFiles[existingIndex] = mergeDuplicateFiles(dedupedFiles[existingIndex], file);
		}

		return dedupedFiles;
	};

	const extractFileIdFromUrl = (value: string | null): string | null => {
		if (!value) {
			return null;
		}
		const match = value.match(/\/api\/v1\/files\/([^/?#]+)/);
		return match?.[1] ?? null;
	};

	const resolveImageFileUrl = (file: any): string | null => {
		const candidates = [
			normalizeKnowflowAssetUrl(file?.bridge_url),
			normalizeKnowflowAssetUrl(file?.generated_file_url),
			normalizeKnowflowAssetUrl(file?.download_url),
			normalizeKnowflowAssetUrl(file?.downloadUrl),
			normalizeKnowflowAssetUrl(file?.url)
		].filter((value): value is string => Boolean(value));

		const fallbackId =
			normalizeKnowflowAssetUrl(file?.id) ??
			normalizeKnowflowAssetUrl(file?.file_id) ??
			normalizeKnowflowAssetUrl(file?.fileId) ??
			normalizeKnowflowAssetUrl(file?.bridge_file_id);

		if (candidates.length === 0) {
			return fallbackId;
		}

		const apiCandidate =
			candidates.find((value) => value.includes('/api/v1/files/') && value.includes('/content')) ??
			candidates.find((value) => value.includes('/api/v1/files/'));

		return apiCandidate ?? candidates[0] ?? fallbackId;
	};

	const resolveImageFileId = (file: any, url: string | null): string | null =>
		normalizeKnowflowAssetUrl(file?.id) ??
		normalizeKnowflowAssetUrl(file?.file_id) ??
		normalizeKnowflowAssetUrl(file?.fileId) ??
		normalizeKnowflowAssetUrl(file?.bridge_file_id) ??
		extractKnowflowAssetRef(url) ??
		extractFileIdFromUrl(url);

	const scoreImageUrl = (value: string | null): number => {
		if (!value) {
			return 0;
		}
		if (value.includes('/api/v1/files/') && value.includes('/content')) {
			return 4;
		}
		if (value.includes('/api/v1/files/')) {
			return 3;
		}
		if (value.startsWith('http')) {
			return 2;
		}
		return 1;
	};

	const dedupeImageFiles = (files: any[] = []): any[] => {
		if (!Array.isArray(files)) {
			return [];
		}

		const deduped: any[] = [];
		const indexByKey = new Map<string, number>();

		for (const file of files) {
			const resolvedUrl = resolveImageFileUrl(file);
			if (!resolvedUrl) {
				continue;
			}
			const dedupeKey = resolveImageFileId(file, resolvedUrl) ?? resolvedUrl;
			const existingIndex = indexByKey.get(dedupeKey);
			if (existingIndex === undefined) {
				indexByKey.set(dedupeKey, deduped.length);
				deduped.push({ ...file, url: resolvedUrl });
				continue;
			}
			const existing = deduped[existingIndex];
			if (scoreImageUrl(normalizeFileRef(existing?.url)) >= scoreImageUrl(resolvedUrl)) {
				continue;
			}
			deduped[existingIndex] = { ...existing, ...file, url: resolvedUrl };
		}

		return deduped;
	};

	const mergeFilesLists = (...fileGroups: any[]) =>
		sanitizeFilesList(
			fileGroups.flatMap((group) =>
				Array.isArray(group) ? group : group && typeof group === 'object' ? [group] : []
			)
		);

	const EMBED_IMAGE_TAG_REGEX = /<img\b[^>]*\bsrc=["']([^"'<>]+)["'][^>]*>/gi;
	const EMBED_TABLE_ROW_REGEX = /<tr\b[^>]*>([\s\S]*?)<\/tr>/gi;
	const EMBED_TABLE_CELL_REGEX = /<t[dh]\b[^>]*>([\s\S]*?)<\/t[dh]>/gi;
	const EMBED_BREAK_TAG_REGEX = /<br\s*\/?>/gi;
	const EMBED_HTML_TAG_REGEX = /<[^>]+>/g;
	const EMBED_HTML_ENTITY_REGEX = /&nbsp;/gi;

	const normalizeEmbedText = (value: string): string =>
		value
			.replace(EMBED_BREAK_TAG_REGEX, ' ')
			.replace(EMBED_HTML_ENTITY_REGEX, ' ')
			.replace(EMBED_HTML_TAG_REGEX, ' ')
			.replace(/\s+/g, ' ')
			.trim();

	const extractEmbedImageUrls = (html: string): string[] =>
		Array.from(html.matchAll(EMBED_IMAGE_TAG_REGEX), (match) =>
			normalizeVisualUrlForMatching(match[1] ?? '')
		).filter(Boolean);

	const EMBED_IFRAME_TAG_REGEX = /<iframe\b[^>]*\bsrc=(['"])(.*?)\1/gi;
	const STANDALONE_EMBED_URL_REGEX =
		/^(?:https?:\/\/|\/|data:|blob:|(?:api|openai)\/v1\/|v1\/)/i;

	const extractEmbedIframeUrls = (html: string): string[] =>
		Array.from(html.matchAll(EMBED_IFRAME_TAG_REGEX), (match) =>
			normalizeVisualUrlForMatching(match[2] ?? '')
		).filter(Boolean);

	const buildNormalizedEmbedUrlSetKey = (urls: string[]): string =>
		Array.from(new Set(urls.filter(Boolean))).sort().join('|');

	const extractStandaloneEmbedUrl = (value: string): string => {
		const normalized = value.trim();
		if (!normalized || normalized.includes('<') || !STANDALONE_EMBED_URL_REGEX.test(normalized)) {
			return '';
		}

		return normalizeVisualUrlForMatching(normalized);
	};

	const extractEmbedTableSignature = (html: string): string => {
		const rows = Array.from(html.matchAll(EMBED_TABLE_ROW_REGEX), (rowMatch) => {
			const cells = Array.from(rowMatch[1].matchAll(EMBED_TABLE_CELL_REGEX), (cellMatch) =>
				normalizeEmbedText(cellMatch[1] ?? '')
			).filter((cell) => cell.length > 0);
			return cells.join('|');
		}).filter((row) => row.length > 0);

		return rows.join('||');
	};

	const buildEmbedMergeKey = (value: unknown): string => {
		if (typeof value !== 'string') {
			return '';
		}

		const normalized = value.trim();
		if (!normalized) {
			return '';
		}

		const tableSignature = extractEmbedTableSignature(normalized);
		if (tableSignature) {
			return `table:${tableSignature}`;
		}

		const imageUrls = extractEmbedImageUrls(normalized);
		if (imageUrls.length > 0) {
			return `image:${buildNormalizedEmbedUrlSetKey(imageUrls)}`;
		}

		const iframeUrls = extractEmbedIframeUrls(normalized);
		if (iframeUrls.length > 0) {
			return `iframe:${buildNormalizedEmbedUrlSetKey(iframeUrls)}`;
		}

		const standaloneUrl = extractStandaloneEmbedUrl(normalized);
		if (standaloneUrl) {
			return `url:${standaloneUrl}`;
		}

		return `html:${normalized}`;
	};

	const scoreEmbedContent = (value: string): number => {
		const normalized = value.trim();
		if (!normalized) {
			return 0;
		}

		let score = normalized.length;
		if (/<table\b/i.test(normalized)) {
			score += 1000;
		}
		if (/<img\b/i.test(normalized)) {
			score += 500;
		}
		if (/<iframe\b/i.test(normalized)) {
			score += 250;
		}

		return score;
	};

	const mergeEmbedsLists = (...embedGroups: any[]) => {
		const merged: string[] = [];
		const indexByKey = new Map<string, number>();

		for (const group of embedGroups) {
			const items = Array.isArray(group)
				? group
				: typeof group === 'string'
					? [group]
					: [];
			for (const item of items) {
				if (typeof item !== 'string') {
					continue;
				}

				const normalized = item.trim();
				if (!normalized) {
					continue;
				}

				const mergeKey = buildEmbedMergeKey(normalized);
				const existingIndex = indexByKey.get(mergeKey);
				if (existingIndex === undefined) {
					indexByKey.set(mergeKey, merged.length);
					merged.push(normalized);
					continue;
				}

				if (scoreEmbedContent(normalized) >= scoreEmbedContent(merged[existingIndex] ?? '')) {
					merged[existingIndex] = normalized;
				}
			}
		}

		return merged;
	};

	const hasSameEmbedList = (existing: unknown, next: string[]): boolean => {
		if (!Array.isArray(existing) || existing.length !== next.length) {
			return false;
		}

		return existing.every((item, index) => typeof item === 'string' && item.trim() === next[index]);
	};

	const mergeEmbedsIntoMessage = (message: Record<string, unknown>, ...embedGroups: any[]): boolean => {
		const nextEmbeds = mergeEmbedsLists(message?.embeds ?? [], ...embedGroups);
		if (nextEmbeds.length === 0) {
			return false;
		}

		if (hasSameEmbedList(message?.embeds, nextEmbeds)) {
			return false;
		}

		message.embeds = nextEmbeds;
		return true;
	};

	const collectGeneratedFilesFromCompletionData = (payload: any) =>
		mergeFilesLists(
			payload?.files,
			payload?.generated_files,
			payload?.generatedFiles,
			payload?.metadata?.generated_files,
			payload?.metadata?.generatedFiles,
			collectGeneratedFilesFromResponseOutput(payload?.output),
			payload?.choices?.[0]?.delta?.files,
			payload?.choices?.[0]?.delta?.metadata?.generated_files,
			payload?.choices?.[0]?.delta?.metadata?.generatedFiles,
			collectGeneratedFilesFromResponseOutput(payload?.choices?.[0]?.delta?.output),
			payload?.choices?.[0]?.message?.files,
			payload?.choices?.[0]?.message?.metadata?.generated_files,
			payload?.choices?.[0]?.message?.metadata?.generatedFiles,
			collectGeneratedFilesFromResponseOutput(payload?.choices?.[0]?.message?.output)
		);

	const dedupeIds = (ids: string[] = []) => [
		...new Set(
			(ids ?? [])
				.filter((id): id is string => typeof id === 'string')
				.map((id) => id.trim())
				.filter((id) => id !== '')
		)
	];

	const sameIds = (left: string[] = [], right: string[] = []) =>
		left.length === right.length && left.every((id, index) => id === right[index]);

	const isHiddenChatTool = (toolId: string) => {
		if (toolId.startsWith('server:') || toolId.startsWith('direct_server:')) {
			return false;
		}

		const tool = $tools?.find((entry) => entry?.id === toolId);
		return tool?.meta?.visibility === 'hidden';
	};

	const isHiddenChatSkill = (skillId: string) => {
		const skill = $skills?.find((entry) => entry?.id === skillId);
		return skill?.meta?.visibility === 'hidden';
	};

	const sanitizeChatToolIds = (ids: string[] = []) =>
		dedupeIds(ids).filter((toolId) => !isHiddenChatTool(toolId));

	const sanitizeChatSkillIds = (ids: string[] = []) =>
		dedupeIds(ids).filter((skillId) => !isHiddenChatSkill(skillId));

	const mergeToolIds = (...groups: string[][]) => sanitizeChatToolIds(groups.flat());

	const applySelectedToolIds = (toolIds: string[] = []) => {
		selectedToolIds = mergeToolIds(sessionToolIds, toolIds);
	};

	$: {
		const nextSessionToolIds = sanitizeChatToolIds(sessionToolIds);
		if (!sameIds(nextSessionToolIds, sessionToolIds)) {
			sessionToolIds = nextSessionToolIds;
		}
	}

	$: {
		const nextSessionSkillIds = sanitizeChatSkillIds(sessionSkillIds);
		if (!sameIds(nextSessionSkillIds, sessionSkillIds)) {
			sessionSkillIds = nextSessionSkillIds;
		}
	}

	$: {
		const nextSelectedToolIds = sanitizeChatToolIds(selectedToolIds);
		if (!sameIds(nextSelectedToolIds, selectedToolIds)) {
			selectedToolIds = nextSelectedToolIds;
		}
	}

	const normalizeRequestContext = (
		requestContext: Partial<ChatRequestContext> | null | undefined,
		fallbackRequestContext: ChatRequestContext | null = null
	): ChatRequestContext => ({
		thinkingModeEnabled: normalizeThinkingModeEnabled(
			requestContext?.thinkingModeEnabled ?? fallbackRequestContext?.thinkingModeEnabled
		),
		selectedToolIds: sanitizeChatToolIds(
			Array.isArray(requestContext?.selectedToolIds)
				? requestContext.selectedToolIds
				: (fallbackRequestContext?.selectedToolIds ?? [])
		),
		selectedFilterIds: dedupeIds(
			Array.isArray(requestContext?.selectedFilterIds)
				? requestContext.selectedFilterIds
				: (fallbackRequestContext?.selectedFilterIds ?? [])
		),
		sessionSkillIds: sanitizeChatSkillIds(
			Array.isArray(requestContext?.sessionSkillIds)
				? requestContext.sessionSkillIds
				: (fallbackRequestContext?.sessionSkillIds ?? [])
		),
		selectedTerminalId:
			typeof requestContext?.selectedTerminalId === 'number' ||
			(typeof requestContext?.selectedTerminalId === 'string' &&
				requestContext.selectedTerminalId.trim() !== '')
				? requestContext.selectedTerminalId
				: (fallbackRequestContext?.selectedTerminalId ?? null),
		finalAnswerOnly: Boolean(
			requestContext?.finalAnswerOnly ?? fallbackRequestContext?.finalAnswerOnly
		),
		featureToggles: {
			imageGenerationEnabled: Boolean(
				requestContext?.featureToggles?.imageGenerationEnabled ??
					fallbackRequestContext?.featureToggles?.imageGenerationEnabled
			),
			webSearchEnabled: Boolean(
				requestContext?.featureToggles?.webSearchEnabled ??
					fallbackRequestContext?.featureToggles?.webSearchEnabled
			),
			codeInterpreterEnabled: Boolean(
				requestContext?.featureToggles?.codeInterpreterEnabled ??
					fallbackRequestContext?.featureToggles?.codeInterpreterEnabled
			)
		}
	});

	const normalizeQueuedMessage = (
		queuedMessage: any,
		fallbackRequestContext: ChatRequestContext | null = null
	): QueuedMessage | null => {
		if (!queuedMessage || typeof queuedMessage !== 'object') {
			return null;
		}

		const prompt =
			typeof queuedMessage.prompt === 'string' ? queuedMessage.prompt : String(queuedMessage.prompt ?? '');
		const queuedFiles = sanitizeFilesList(
			Array.isArray(queuedMessage.files) ? queuedMessage.files : []
		);
		if (prompt.trim() === '' && queuedFiles.length === 0) {
			return null;
		}

		return {
			id:
				typeof queuedMessage.id === 'string' && queuedMessage.id.trim() !== ''
					? queuedMessage.id
					: uuidv4(),
			prompt,
			files: queuedFiles,
			requestContext: normalizeRequestContext(
				queuedMessage.requestContext,
				fallbackRequestContext
			)
		};
	};

	const normalizeMessageQueue = (
		queueData: any,
		fallbackRequestContext: ChatRequestContext | null = null
	): QueuedMessage[] => {
		if (!Array.isArray(queueData)) {
			return [];
		}

		return queueData
			.map((queuedMessage) => normalizeQueuedMessage(queuedMessage, fallbackRequestContext))
			.filter((queuedMessage): queuedMessage is QueuedMessage => queuedMessage !== null);
	};

	const serializeRequestContext = (requestContext: ChatRequestContext): string =>
		JSON.stringify(normalizeRequestContext(requestContext));

	const dequeueMessageBatch = (
		queueData: QueuedMessage[]
	): { batch: QueuedMessage[]; remaining: QueuedMessage[] } => {
		if (queueData.length === 0) {
			return { batch: [], remaining: [] };
		}

		const firstRequestContextKey = serializeRequestContext(queueData[0].requestContext);
		let batchLength = 1;

		while (
			batchLength < queueData.length &&
			serializeRequestContext(queueData[batchLength].requestContext) === firstRequestContextKey
		) {
			batchLength += 1;
		}

		return {
			batch: queueData.slice(0, batchLength),
			remaining: queueData.slice(batchLength)
		};
	};

	const restoreQueuedComposerState = (requestContext: ChatRequestContext | null | undefined) => {
		if (!requestContext) {
			return;
		}

		applySelectedToolIds(requestContext.selectedToolIds);
		selectedFilterIds = [...requestContext.selectedFilterIds];
		sessionSkillIds = sanitizeChatSkillIds(requestContext.sessionSkillIds);
		imageGenerationEnabled = requestContext.featureToggles.imageGenerationEnabled;
		webSearchEnabled = requestContext.featureToggles.webSearchEnabled;
		codeInterpreterEnabled = requestContext.featureToggles.codeInterpreterEnabled;
		thinkingModeEnabled = requestContext.thinkingModeEnabled;
		selectedTerminalId.set(
			requestContext.selectedTerminalId == null ? null : `${requestContext.selectedTerminalId}`
		);
	};

	const submitQueuedMessages = async (queueData: QueuedMessage[]) => {
		const normalizedQueue = normalizeMessageQueue(queueData);
		if (normalizedQueue.length === 0) {
			return;
		}

		const { batch, remaining } = dequeueMessageBatch(normalizedQueue);
		messageQueue = remaining;

		files = sanitizeFilesList(batch.flatMap((item) => item.files));
		await tick();
		await submitPrompt(batch.map((item) => item.prompt).join('\n\n'), {
			requestContext: batch[0].requestContext
		});
	};

	$: lockedToolIds = mergeToolIds(sessionToolIds);

	const getChatSessionMeta = () => ({
		session_tool_ids: sanitizeChatToolIds(sessionToolIds),
		session_skill_ids: sanitizeChatSkillIds(sessionSkillIds)
	});

	const persistSessionCapabilities = async (
		toolIds: string[] = sessionToolIds,
		skillIds: string[] = sessionSkillIds
	) => {
		const nextToolIds = sanitizeChatToolIds(toolIds);
		const nextSkillIds = sanitizeChatSkillIds(skillIds);

		sessionToolIds = nextToolIds;
		sessionSkillIds = nextSkillIds;
		applySelectedToolIds(selectedToolIds);

		if (!$chatId || $chatId.startsWith('local:')) {
			return null;
		}

		const updatedChat = await updateChatSessionCapabilities(localStorage.token, $chatId, {
			tool_ids: nextToolIds,
			skill_ids: nextSkillIds
		});

		if (updatedChat) {
			chat = updatedChat;
			sessionToolIds = sanitizeChatToolIds(updatedChat?.meta?.session_tool_ids ?? nextToolIds);
			sessionSkillIds = sanitizeChatSkillIds(updatedChat?.meta?.session_skill_ids ?? nextSkillIds);
			applySelectedToolIds(selectedToolIds);
		}

		return updatedChat;
	};

	const closeCapabilityInstallDialog = () => {
		showCapabilityInstallDialog = false;
		capabilityInstallRequest = null;
	};

	const handleCapabilityInstallDecision = async (decision: 'install' | 'session' | 'cancel') => {
		const request = capabilityInstallRequest;
		const callback = eventCallback;

		closeCapabilityInstallDialog();
		eventCallback = null;

		if (!callback || !request) {
			return;
		}

		if (decision === 'cancel') {
			callback({
				decision,
				resource_type: request.resource_type,
				resource_id: request.resource_id
			});
			return;
		}

		try {
			if (request.resource_type === 'tool') {
				if (decision === 'install') {
					await installToolById(localStorage.token, request.resource_id);
					tools.set(await getTools(localStorage.token));
					applySelectedToolIds([...selectedToolIds, request.resource_id]);
				} else {
					await persistSessionCapabilities(
						[...sessionToolIds, request.resource_id],
						sessionSkillIds
					);
					applySelectedToolIds([...selectedToolIds, request.resource_id]);
				}
			} else if (decision === 'install') {
				await installSkillById(localStorage.token, request.resource_id);
				skills.set(await getSkills(localStorage.token));
			} else {
				await persistSessionCapabilities(sessionToolIds, [...sessionSkillIds, request.resource_id]);
			}

			callback({
				decision,
				resource_type: request.resource_type,
				resource_id: request.resource_id
			});
		} catch (error) {
			const errorMessage =
				typeof error === 'string'
					? error
					: (error?.message ?? $i18n.t('Failed to update capability'));

			toast.error(errorMessage);
			callback({
				decision: 'error',
				resource_type: request.resource_type,
				resource_id: request.resource_id,
				error: errorMessage
			});
		}
	};

	const LEGACY_MODEL_ALIASES: Record<string, string> = {
		deepagent: '中电慧语'
	};

	const normalizeModelId = (value: unknown): string => {
		if (typeof value !== 'string') {
			return '';
		}

		const normalized = value.trim();
		if (!normalized) {
			return '';
		}

		return LEGACY_MODEL_ALIASES[normalized] ?? normalized;
	};

	const normalizeModelSelection = (value: unknown): string[] => {
		const rawValues = Array.isArray(value) ? value : value != null ? [value] : [];
		const normalizedValues = rawValues
			.map((modelId) => normalizeModelId(modelId))
			.filter((modelId) => modelId !== '');

		return Array.from(new Set(normalizedValues));
	};

	const sanitizeHistoryFileRefs = (historyData: any) => {
		if (!historyData?.messages || typeof historyData.messages !== 'object') {
			return;
		}

		for (const message of Object.values(historyData.messages)) {
			if (message && typeof message === 'object' && Array.isArray((message as any).files)) {
				(message as any).files = sanitizeFilesList((message as any).files);
			}
		}
	};

	const sanitizeHistoryModelRefs = (historyData: any) => {
		if (!historyData?.messages || typeof historyData.messages !== 'object') {
			return;
		}

		for (const message of Object.values(historyData.messages)) {
			if (!message || typeof message !== 'object') {
				continue;
			}

			if (typeof (message as any).model === 'string') {
				(message as any).model = normalizeModelId((message as any).model);
			}

			if (typeof (message as any).selectedModelId === 'string') {
				(message as any).selectedModelId = normalizeModelId((message as any).selectedModelId);
			}

			if (Array.isArray((message as any).models)) {
				(message as any).models = normalizeModelSelection((message as any).models);
			}
		}
	};

	const sourceSignature = (source: any): string => {
		if (!source || typeof source !== 'object') {
			return '';
		}

		if (source?.data && typeof source.data === 'object') {
			source = source.data;
		}

		const sourceMeta = source?.source ?? {};
		const metadata = Array.isArray(source?.metadata)
			? source.metadata
			: Array.isArray(source?.metadatas)
				? source.metadatas
				: [];
		const document = Array.isArray(source?.document)
			? source.document
			: Array.isArray(source?.documents)
				? source.documents
				: [];
		const distances = Array.isArray(source?.distances)
			? source.distances
			: Array.isArray(source?.distance)
				? source.distance
				: [];

		return JSON.stringify({
			id: source?.id ?? null,
			source_id: sourceMeta?.id ?? null,
			source_name: sourceMeta?.name ?? null,
			source_url: sourceMeta?.url ?? null,
			metadata,
			document,
			distances
		});
	};

	const normalizeSourceList = (value: any): any[] => {
		if (Array.isArray(value)) return value;
		if (value && typeof value === 'object') return [value];
		return [];
	};

	const mergeMessageSources = (existingSources: any = [], incomingSources: any = []) => {
		const merged: any[] = [];
		const seen = new Set<string>();

		for (const source of [
			...normalizeSourceList(existingSources),
			...normalizeSourceList(incomingSources)
		]) {
			if (!source || typeof source !== 'object') {
				continue;
			}
			if (source?.type === 'code_execution') {
				continue;
			}

			const signature = sourceSignature(source);
			if (!signature || seen.has(signature)) {
				continue;
			}

			seen.add(signature);
			merged.push(source);
		}

		return merged;
	};

	const isSourceLikeItem = (value: any) =>
		value &&
		typeof value === 'object' &&
		('source' in value || 'document' in value || 'documents' in value || 'metadata' in value);

	const getSourceLikeItems = (payload: any): any[] => {
		if (Array.isArray(payload)) {
			return payload.flatMap(getSourceLikeItems);
		}

		if (isSourceLikeItem(payload)) {
			return [payload];
		}

		const items: any[] = [];
		for (const key of ['sources', 'citations', 'references']) {
			const value = payload?.[key];
			if (Array.isArray(value)) {
				const nestedItems = value.flatMap(getSourceLikeItems);
				items.push(
					...(nestedItems.length > 0
						? nestedItems
						: value.filter((item) => item && typeof item === 'object'))
				);
			} else if (value && typeof value === 'object') {
				const nestedItems = getSourceLikeItems(value);
				items.push(...(nestedItems.length > 0 ? nestedItems : [value]));
			}
		}
		if (items.length > 0) {
			return items;
		}

		if (payload?.data && typeof payload.data === 'object') {
			return getSourceLikeItems(payload.data);
		}

		return items;
	};

	const normalizeTaskIds = (ids: unknown): string[] =>
		(Array.isArray(ids) ? ids : [])
			.map((id) => (typeof id === 'string' ? id.trim() : ''))
			.filter((id) => id !== '' && id !== 'null' && id !== 'undefined');

	const setActiveChatIndicator = (
		targetChatId: string | null | undefined,
		isActive: boolean
	) => {
		const normalizedChatId = typeof targetChatId === 'string' ? targetChatId.trim() : '';
		if (!normalizedChatId || normalizedChatId.startsWith('local:')) return;

		activeChatIds.update((ids) => {
			const nextIds = new Set(ids);
			if (isActive) {
				nextIds.add(normalizedChatId);
			} else {
				nextIds.delete(normalizedChatId);
			}
			return nextIds;
		});
	};

	const resolveActiveTaskIds = async (requestedChatId: string | null | undefined) => {
		let activeTaskIds = normalizeTaskIds(taskIds);
		if (activeTaskIds.length > 0) {
			return activeTaskIds;
		}

		if (!requestedChatId || requestedChatId.startsWith('local:')) {
			return [];
		}

		const taskRes = await getTaskIdsByChatId(localStorage.token, requestedChatId).catch(
			(error) => {
				console.error(error);
				return null;
			}
		);
		activeTaskIds = normalizeTaskIds(taskRes?.task_ids);
		return activeTaskIds;
	};

	const finalizeAssistantMessage = async (chatId: string, message: any) => {
		message = { ...(message ?? {}), done: true };

		if ($settings.responseAutoCopy) {
			copyToClipboard(
				removeAllDetails(removeDetails(message.content, ['tool_calls'])).replace(/\n{3,}/g, '\n\n')
			);
		}

		if ($settings.responseAutoPlayback && !$showCallOverlay) {
			await tick();
			document.getElementById(`speak-button-${message.id}`)?.click();
		}

		// Emit chat event for TTS (only when call overlay is active)
		if ($showCallOverlay) {
			const lastMessageContentPart =
				getMessageContentParts(
					removeAllDetails(message.content),
					$config?.audio?.tts?.split_on ?? 'punctuation'
				)?.at(-1) ?? '';
			if (lastMessageContentPart) {
				eventTarget.dispatchEvent(
					new CustomEvent('chat', {
						detail: { id: message.id, content: lastMessageContentPart }
					})
				);
			}
		}

		eventTarget.dispatchEvent(
			new CustomEvent('chat:finish', {
				detail: {
					id: message.id,
					content: message.content
				}
			})
		);

		replaceHistoryMessage(message);

		await tick();
		if (autoScroll) {
			scrollToBottom();
		}

		await chatCompletedHandler(
			chatId,
			message.model,
			message.id,
			createMessagesList(history, message.id)
		);
	};

	const flushPendingChatCompletion = async (
		requestedChatId: string,
		activeTaskIds: string[]
	): Promise<boolean> => {
		if (!pendingChatCompletion) {
			return false;
		}
		if (pendingChatCompletion.chatId !== requestedChatId) {
			return false;
		}
		if (activeTaskIds.length > 0) {
			return false;
		}

		const pendingMessage = history.messages[pendingChatCompletion.messageId];
		pendingChatCompletion = null;
		if (!pendingMessage || pendingMessage.role !== 'assistant') {
			return false;
		}

		await finalizeAssistantMessage(requestedChatId, pendingMessage);
		return true;
	};

	const extractOutputTextParts = (value: unknown): string => {
		if (typeof value === 'string') return value;
		if (!Array.isArray(value)) return '';

		return value
			.map((part) => {
				if (typeof part === 'string') return part;
				if (!part || typeof part !== 'object') return '';
				const record = part as Record<string, unknown>;
				const text = record.text ?? record.output ?? record.content;
				return typeof text === 'string' ? text : '';
			})
			.filter(Boolean)
			.join('')
			.trim();
	};

	const extractAssistantMessageTextFromOutput = (value: unknown): string => {
		if (!Array.isArray(value)) return '';

		for (let index = value.length - 1; index >= 0; index -= 1) {
			const item = value[index];
			if (!item || typeof item !== 'object') continue;
			const record = item as Record<string, unknown>;
			if (record.type !== 'message' || record.role !== 'assistant') continue;

			const contentText = extractOutputTextParts(record.content);
			if (contentText) return contentText;

			const summaryText = extractOutputTextParts(record.summary);
			if (summaryText) return summaryText;
		}

		return '';
	};

	const hasRenderableAssistantPayload = (message: any): boolean => {
		if (!message || message.role !== 'assistant') {
			return false;
		}

		if (typeof message.content === 'string' && message.content.trim() !== '') {
			return true;
		}

		if (Array.isArray(message.content) && extractOutputTextParts(message.content)) {
			return true;
		}

		if (extractAssistantMessageTextFromOutput(message.output)) {
			return true;
		}

		if (Array.isArray(message.files) && message.files.length > 0) {
			return true;
		}

		if (Array.isArray(message.embeds) && message.embeds.length > 0) {
			return true;
		}

		return Boolean(message.error);
	};

	const mergeHistoryMessage = (existingMessage: any, nextMessage: any) => {
		const previousContent = existingMessage?.content;

		const merged = {
			...(existingMessage ?? {}),
			...(previousContent !== undefined && previousContent !== nextMessage.content
				? { originalContent: previousContent }
				: {}),
			...nextMessage
		};

		if (
			existingMessage?.parentId &&
			(nextMessage?.parentId === null || nextMessage?.parentId === undefined)
		) {
			merged.parentId = existingMessage.parentId;
		}

		if (
			Array.isArray(existingMessage?.childrenIds) &&
			existingMessage.childrenIds.length > 0 &&
			(!Array.isArray(nextMessage?.childrenIds) || nextMessage.childrenIds.length === 0)
		) {
			merged.childrenIds = existingMessage.childrenIds;
		}

		for (const key of ['sources', 'citations']) {
			const existingReferences = existingMessage?.[key];
			const nextReferences = nextMessage?.[key];
			if (
				(existingReferences && typeof existingReferences === 'object') ||
				(nextReferences && typeof nextReferences === 'object')
			) {
				const references = mergeMessageSources(existingReferences ?? [], nextReferences ?? []);
				if (references.length > 0) {
					merged[key] = references;
				}
			}
		}

		return merged;
	};

	const replaceHistoryMessage = (message: any) => {
		if (!message?.id) return;
		history = {
			...history,
			messages: {
				...(history?.messages ?? {}),
				[message.id]: message
			}
		};
	};

	const replaceHistoryMessages = (updates: Record<string, any>) => {
		const entries = Object.entries(updates ?? {}).filter(([id, message]) => id && message);
		if (entries.length === 0) return;
		history = {
			...history,
			messages: {
				...(history?.messages ?? {}),
				...Object.fromEntries(entries)
			}
		};
	};

	const hasBlockingAssistantResponse = (historyData: any): boolean => {
		const messages = historyData?.messages;
		if (!messages || typeof messages !== 'object') {
			return false;
		}

		const currentId =
			typeof historyData?.currentId === 'string' ? historyData.currentId.trim() : '';
		if (!currentId || !messages[currentId]) {
			return false;
		}

		const currentMessage = messages[currentId];
		const hasUnfinishedAssistant = (messageId: string): boolean => {
			const message = messages[messageId];
			return Boolean(message && message.role === 'assistant' && message.done !== true);
		};

		if (currentMessage.role === 'assistant') {
			if (currentMessage.done !== true) {
				return true;
			}

			const parentMessage = currentMessage.parentId ? messages[currentMessage.parentId] : null;
			const siblingIds: string[] = Array.isArray(parentMessage?.childrenIds)
				? parentMessage.childrenIds
				: [];
			return siblingIds.some((messageId: string) => hasUnfinishedAssistant(messageId));
		}

		if (currentMessage.role === 'user') {
			const childIds: string[] = Array.isArray(currentMessage.childrenIds)
				? currentMessage.childrenIds
				: [];
			return childIds.some((messageId: string) => hasUnfinishedAssistant(messageId));
		}

		return false;
	};

	let hasBlockingGeneration = false;
	$: hasBlockingGeneration = generating || hasBlockingAssistantResponse(history);

	const resolveHistoryCurrentId = (historyData: any): string | null => {
		const messages = historyData?.messages;
		if (!messages || typeof messages !== 'object') {
			return null;
		}

		const entries = Object.entries(messages).filter(
			([id, message]) => typeof id === 'string' && message && typeof message === 'object'
		) as Array<[string, Record<string, any>]>;

		if (entries.length === 0) {
			return null;
		}

		const getValidChildIds = (message: Record<string, any>) =>
			(Array.isArray(message.childrenIds) ? message.childrenIds : []).filter(
				(childId) => typeof childId === 'string' && messages[childId]
			);

		const getBranchMessages = (messageId: string) => {
			const branch: Array<Record<string, any>> = [];
			const visited = new Set<string>();
			let currentId: string | null | undefined = messageId;

			while (currentId !== null && currentId !== undefined) {
				if (visited.has(currentId)) {
					break;
				}
				visited.add(currentId);
				const message = messages[currentId];
				if (!message || typeof message !== 'object') {
					break;
				}
				branch.push(message);
				currentId = typeof message.parentId === 'string' ? message.parentId : null;
			}

			return branch;
		};

		const isValidCurrentLeaf = (messageId: string) => {
			const message = messages[messageId];
			if (!message || typeof message !== 'object') return false;
			if (getValidChildIds(message).length > 0) return false;

			const branch = getBranchMessages(messageId);
			if (branch.length > 1) return true;
			if (typeof message.parentId === 'string' && message.parentId.trim()) return true;

			// A single root user/system message can be a valid empty or imported branch.
			// A root assistant is only valid when it is the whole history; otherwise it is
			// usually a sparse completion update that lost its parent linkage.
			return message.role !== 'assistant' || entries.length === 1;
		};

		const existingCurrentId =
			typeof historyData?.currentId === 'string' ? historyData.currentId.trim() : '';
		if (existingCurrentId && messages[existingCurrentId] && isValidCurrentLeaf(existingCurrentId)) {
			return existingCurrentId;
		}

		const leaves = entries.filter(([, message]) => {
			return getValidChildIds(message).length === 0;
		});

		const validLeaves = leaves.filter(([id]) => isValidCurrentLeaf(id));
		const candidates = validLeaves.length > 0 ? validLeaves : leaves.length > 0 ? leaves : entries;
		candidates.sort((a, b) => {
			const tsA = typeof a[1]?.timestamp === 'number' ? a[1].timestamp : 0;
			const tsB = typeof b[1]?.timestamp === 'number' ? b[1].timestamp : 0;
			if (tsA !== tsB) {
				return tsB - tsA;
			}
			return a[0] < b[0] ? 1 : -1;
		});

		return candidates[0]?.[0] ?? null;
	};

	const getCurrentBranchLastAssistantMessage = (historyData: any) => {
		const currentMessageId = resolveHistoryCurrentId(historyData);
		if (!currentMessageId || !historyData?.messages?.[currentMessageId]) {
			return null;
		}

		historyData.currentId = currentMessageId;

		const branchMessages = createMessagesList(historyData, currentMessageId);
		return (
			[...branchMessages]
			.reverse()
			.find((message) => message?.role === 'assistant') ?? null
		);
	};

	const markHistoryForRecoveredActiveTasks = (historyData: any) => {
		const lastAssistantMessage = getCurrentBranchLastAssistantMessage(historyData);

		// Preserve explicit completion. Active backend tasks may continue for follow-ups,
		// title generation, or tags after the assistant answer has already finished.
		if (lastAssistantMessage && lastAssistantMessage.done !== true) {
			historyData.messages[lastAssistantMessage.id] = {
				...lastAssistantMessage,
				done: false
			};
		}
	};

	const normalizeHistoryMeta = (meta: any): HistoryMeta | null => {
		if (!meta || typeof meta !== 'object') return null;

		const truncated =
			meta.truncated ??
			meta.history_truncated ??
			meta.is_truncated ??
			(meta.history ? meta.history.truncated : undefined);
		const totalMessages =
			typeof meta.total_messages === 'number'
				? meta.total_messages
				: typeof meta.total === 'number'
					? meta.total
					: meta.history && typeof meta.history.total === 'number'
						? meta.history.total
					: meta.history && typeof meta.history.total_messages === 'number'
						? meta.history.total_messages
						: null;
		const canLoadMore =
			meta.can_load_more ??
			meta.has_more ??
			(meta.history ? meta.history.can_load_more ?? meta.history.has_more : undefined);
		const windowSize =
			typeof meta.window_size === 'number'
				? meta.window_size
				: typeof meta.tail === 'number'
					? meta.tail
				: meta.history && typeof meta.history.window_size === 'number'
					? meta.history.window_size
					: meta.history && typeof meta.history.tail === 'number'
						? meta.history.tail
					: null;

		return {
			truncated: Boolean(truncated),
			totalMessages,
			canLoadMore: Boolean(canLoadMore),
			windowSize
		};
	};

	const inferHistoryTruncation = (historyData: any): boolean => {
		if (!historyData?.messages || typeof historyData.messages !== 'object') return false;
		for (const message of Object.values(historyData.messages)) {
			if (!message || typeof message !== 'object') continue;
			const parentId = (message as any).parentId;
			if (parentId && !historyData.messages[parentId]) {
				return true;
			}
		}
		return false;
	};

	const resolveHistoryFromChatContent = (chatContent: any) => {
		if (!chatContent) {
			return { messages: {}, currentId: null };
		}
		const baseHistory =
			(chatContent?.history ?? undefined) !== undefined
				? chatContent.history
				: convertMessagesToHistory(chatContent.messages);
		return baseHistory ?? { messages: {}, currentId: null };
	};

	const prepareHistory = (historyData: any) => {
		return sanitizeHistoryForPersistence(historyData ?? { messages: {}, currentId: null });
	};

	const normalizeHistoryMessage = (message: any) => {
		if (!message || typeof message !== 'object') {
			return message;
		}

		if (message.role !== 'assistant') {
			return message;
		}

		const normalizedContent =
			typeof message.content === 'string'
				? normalizeAssistantResponseContent(message.content)
				: message.content;
		const normalizedOutput = normalizeToolResponseOutput(message.output);

		if (normalizedContent === message.content && normalizedOutput === message.output) {
			return message;
		}

		return {
			...message,
			content: normalizedContent,
			output: normalizedOutput
		};
	};

	const sanitizeHistoryForPersistence = (historyData: any) => {
		const normalized = historyData ?? { messages: {}, currentId: null };
		sanitizeHistoryFileRefs(normalized);
		sanitizeHistoryModelRefs(normalized);

		if (normalized.messages && typeof normalized.messages === 'object') {
			for (const [id, message] of Object.entries(normalized.messages)) {
				normalized.messages[id] = normalizeHistoryMessage(message);
			}
		}

		normalized.currentId = resolveHistoryCurrentId(normalized);
		return normalized;
	};

	const mergeHistoryData = (target: any, incoming: any): number => {
		if (!target?.messages || !incoming?.messages) return 0;
		let added = 0;
		for (const [id, rawMessage] of Object.entries(incoming.messages)) {
			const message = normalizeHistoryMessage(rawMessage);
			if (!target.messages[id]) {
				target.messages[id] = message;
				added += 1;
			}
		}
		for (const [id, msg] of Object.entries(incoming.messages)) {
			if (!target.messages[id] || !msg || typeof msg !== 'object') continue;
			const incomingChildren = Array.isArray((msg as any).childrenIds)
				? (msg as any).childrenIds
				: [];
			if (incomingChildren.length === 0) continue;
			const existingChildren = Array.isArray((target.messages[id] as any).childrenIds)
				? (target.messages[id] as any).childrenIds
				: [];
			const mergedChildren = Array.from(new Set([...existingChildren, ...incomingChildren]));
			(target.messages[id] as any).childrenIds = mergedChildren;
		}
		if (!target.currentId && incoming.currentId) {
			target.currentId = incoming.currentId;
		}
		return added;
	};

	const buildHistoryRequestParams = (tailSize: number) => ({
		recent_only: true,
		include_full_history: false,
		tail: tailSize
	});

	const ensureHistoryLoaded = async (): Promise<boolean> => {
		if (!$chatId || !(historyMeta?.canLoadMore || historyMeta?.truncated)) {
			return true;
		}

		if (historyFullLoadPromise) {
			return historyFullLoadPromise;
		}

		historyFullLoadPromise = (async () => {
			const chatRes = await getChatById(localStorage.token, $chatId).catch(() => null);

			if (!chatRes?.chat) {
				return false;
			}

			history = prepareHistory(resolveHistoryFromChatContent(chatRes.chat));
			const nextMeta = normalizeHistoryMeta(chatRes.meta);
			historyMeta = nextMeta ?? {
				truncated: false,
				totalMessages: Object.keys(history?.messages ?? {}).length,
				canLoadMore: false,
				windowSize: Object.keys(history?.messages ?? {}).length
			};
			return true;
		})().finally(() => {
			historyFullLoadPromise = null;
		});

		return historyFullLoadPromise;
	};

	const refreshActiveTasks = async () => {
		if (!$chatId || !$page.url.pathname.startsWith('/c/')) return;
		const now = Date.now();
		if (now - lastTaskRefreshAt < 1500) return;
		lastTaskRefreshAt = now;

		const taskRes = await getTaskIdsByChatId(localStorage.token, $chatId).catch(() => null);
		if (!taskRes) return;
		const nextTaskIds = normalizeTaskIds(taskRes?.task_ids);
		taskIds = nextTaskIds;
		setActiveChatIndicator($chatId, nextTaskIds.length > 0);

		if ((nextTaskIds?.length ?? 0) > 0) {
			markHistoryForRecoveredActiveTasks(history);
		} else if (history?.currentId) {
			if (await flushPendingChatCompletion($chatId, nextTaskIds)) {
				return;
			}

			const lastAssistantMessage = getCurrentBranchLastAssistantMessage(history);
			if (
				lastAssistantMessage &&
				(!hasRenderableAssistantPayload(lastAssistantMessage) ||
					lastAssistantMessage.done !== true)
			) {
				await finalizeIdleChatAfterReload($chatId);
			} else {
				const updates = Object.fromEntries(
					Object.entries(history.messages ?? {}).map(([id, message]) => [
						id,
						message && (message as any).role === 'assistant'
							? { ...(message as any), done: true }
							: message
					])
				);
				history = {
					...history,
					messages: updates
				};
			}
		}
	};

	const isCurrentChatRequest = (requestedChatId: string | null | undefined) =>
		Boolean(requestedChatId) && $chatId === requestedChatId && chatIdProp === requestedChatId;

	const isVisibleChatTarget = (targetChatId: string | null | undefined) =>
		Boolean(targetChatId) && ($chatId === targetChatId || chatIdProp === targetChatId);

	const applyTaskIdsToHistory = (nextTaskIds: string[]) => {
		taskIds = nextTaskIds;
		setActiveChatIndicator($chatId, (nextTaskIds?.length ?? 0) > 0);

		if ((nextTaskIds?.length ?? 0) > 0) {
			markHistoryForRecoveredActiveTasks(history);
			history = {
				...history,
				messages: { ...(history?.messages ?? {}) }
			};
		} else if (history?.currentId) {
			const updates = Object.fromEntries(
				Object.entries(history.messages ?? {}).map(([id, message]) => [
					id,
					message && message.role === 'assistant' ? { ...message, done: true } : message
				])
			);
			history = {
				...history,
				messages: updates
			};
			return;
		}
	};

	const finalizeIdleChatAfterReload = async (requestedChatId: string) => {
		const reloaded = await refreshHistoryFromServer(requestedChatId);
		if (!reloaded) {
			applyTaskIdsToHistory([]);
			return;
		}

		const refreshedLastAssistantMessage = getCurrentBranchLastAssistantMessage(history);
		if (
			refreshedLastAssistantMessage &&
			hasRenderableAssistantPayload(refreshedLastAssistantMessage) &&
			refreshedLastAssistantMessage.done === true
		) {
			return;
		}

		applyTaskIdsToHistory([]);
	};

	const loadMoreHistory = async () => {
		if (loadingHistoryMore || !$chatId) return;
		const canLoadMore = historyMeta?.canLoadMore ?? historyMeta?.truncated;
		if (!canLoadMore) return;

		loadingHistoryMore = true;
		try {
			const branchMessages = history?.currentId
				? createMessagesList(history, history.currentId)
				: [];
			const requestedTail = Math.max(branchMessages.length + HISTORY_TAIL_STEP, HISTORY_TAIL_STEP);
			const chatRes = await getChatById(
				localStorage.token,
				$chatId,
				buildHistoryRequestParams(requestedTail)
			).catch(() => null);
			if (!chatRes?.chat) {
				historyMeta = {
					...historyMeta,
					canLoadMore: false
				};
				return;
			}

			const incomingHistory = prepareHistory(resolveHistoryFromChatContent(chatRes.chat));
			const added = mergeHistoryData(history, incomingHistory);
			if (added > 0) {
				history = history;
			}

			const nextMeta = normalizeHistoryMeta(chatRes.meta);
			if (nextMeta) {
				historyMeta = nextMeta;
			} else if (added === 0) {
				historyMeta = {
					...historyMeta,
					canLoadMore: false
				};
			}
		} finally {
			loadingHistoryMore = false;
		}
	};

	// Message queue for storing messages while generating
	let messageQueue: QueuedMessage[] = [];
	let navigateRunId = 0;

	$: if (chatIdProp) {
		navigateHandler();
	}

	const isActiveChatNavigation = (runId: number, targetChatId: string | null | undefined) =>
		runId === navigateRunId && chatIdProp === targetChatId;

	const cancelPendingNewChatInit = () => {
		initNewChatRunId += 1;
	};

	const canReuseVisibleHistoryForNavigation = (targetChatId: string | null | undefined) => {
		if (!targetChatId || targetChatId !== $chatId) {
			return false;
		}

		if (!history?.currentId) {
			return false;
		}

		return Object.keys(history?.messages ?? {}).length > 0;
	};

	const navigateHandler = async () => {
		const runId = ++navigateRunId;
		const targetChatId = chatIdProp;
		const preserveVisibleHistory = canReuseVisibleHistoryForNavigation(targetChatId);
		loading = !preserveVisibleHistory;
		await resetAuxiliaryPanels();

		// Save current queue to sessionStorage before navigating away
		if (messageQueue.length > 0 && $chatId) {
			sessionStorage.setItem(`chat-queue-${$chatId}`, JSON.stringify(messageQueue));
		}

		prompt = '';
		messageInput?.setText('');

		files = [];
		messageQueue = [];
		tags = [];
		taskIds = null;
		pendingChatCompletion = null;
		pendingTaskIdsLoad = null;
		sessionToolIds = [];
		sessionSkillIds = [];
		historyMeta = {
			truncated: false,
			totalMessages: null,
			canLoadMore: false,
			windowSize: null
		};
		selectedToolIds = [];
		selectedFilterIds = [];
		webSearchEnabled = false;
		imageGenerationEnabled = false;
		thinkingModeEnabled = DEFAULT_THINKING_MODE_ENABLED;

		const storageChatInput = sessionStorage.getItem(
			`chat-input${targetChatId ? `-${targetChatId}` : ''}`
		);

		try {
			if (targetChatId && (await loadChat())) {
				if (!isActiveChatNavigation(runId, targetChatId)) {
					return;
				}

				loading = false;
				await tick();
				window.setTimeout(() => scrollToBottom(), 0);

				await tick();

				if (storageChatInput) {
					try {
						const input = JSON.parse(storageChatInput);

						if (!$temporaryChatEnabled) {
							messageInput?.setText(input.prompt);
							files = sanitizeFilesList(input.files ?? []);
							applySelectedToolIds(input.selectedToolIds ?? []);
							selectedFilterIds = input.selectedFilterIds;
							webSearchEnabled = input.webSearchEnabled;
							imageGenerationEnabled = input.imageGenerationEnabled;
							codeInterpreterEnabled = input.codeInterpreterEnabled;
							thinkingModeEnabled = normalizeThinkingModeEnabled(
								input.thinkingModeEnabled
							);
						}
					} catch (e) {}
				} else {
					await setDefaults();
				}

				// Restore queue from sessionStorage after input state is restored so older
				// queue entries without per-request metadata still inherit the right toggles.
				const storedQueueData = sessionStorage.getItem(`chat-queue-${targetChatId}`);
				if (storedQueueData) {
					try {
						const restoredQueue = normalizeMessageQueue(
							JSON.parse(storedQueueData),
							captureRequestContext()
						);

						if (restoredQueue.length > 0) {
							sessionStorage.removeItem(`chat-queue-${targetChatId}`);
							if (pendingTaskIdsLoad) {
								await pendingTaskIdsLoad;
								await tick();
							}
							if (!hasBlockingAssistantResponse(history)) {
								await submitQueuedMessages(restoredQueue);
							} else {
								// Has pending tasks - show as queued (chatCompletedHandler will process)
								messageQueue = restoredQueue;
							}
						}
					} catch (e) {}
				}

				const chatInput = document.getElementById('chat-input');
				chatInput?.focus();
			} else if (targetChatId && isActiveChatNavigation(runId, targetChatId)) {
				await goto('/');
			}
		} catch (error) {
			console.error('Failed to load chat view:', error);
			toast.error($i18n.t('Failed to load conversation'));
		} finally {
			if (
				targetChatId &&
				isActiveChatNavigation(runId, targetChatId) &&
				$page.url.pathname === `/c/${targetChatId}`
			) {
				loading = false;
			}
		}
	};

	const onSelect = async (e) => {
		const { type, data } = e;

		if (type === 'prompt') {
			// Handle prompt selection
			messageInput?.setText(data, async () => {
				if (!($settings?.insertSuggestionPrompt ?? false)) {
					cancelPendingNewChatInit();
					await tick();
					submitPrompt(prompt);
				}
			});
		}
	};

	$: if (selectedModels && chatIdProp !== '') {
		saveSessionSelectedModels();
	}

	const saveSessionSelectedModels = () => {
		const selectedModelsString = JSON.stringify(selectedModels);
		if (
			selectedModels.length === 0 ||
			(selectedModels.length === 1 && selectedModels[0] === '') ||
			sessionStorage.selectedModels === selectedModelsString
		) {
			return;
		}
		sessionStorage.selectedModels = selectedModelsString;
		console.log('saveSessionSelectedModels', selectedModels, sessionStorage.selectedModels);
	};

	let oldSelectedModelIds = [''];
	$: if (JSON.stringify(selectedModelIds) !== JSON.stringify(oldSelectedModelIds)) {
		onSelectedModelIdsChange();
	}

	const onSelectedModelIdsChange = () => {
		resetInput();
		oldSelectedModelIds = structuredClone(selectedModelIds);
	};

	const ensureSelectedModels = () => {
		const visibleModelIds = $models
			.filter((m) => !(m?.info?.meta?.hidden ?? false))
			.map((m) => m.id);

		const configuredDefaultModels = ($config?.default_models ?? '')
			.split(',')
			.map((id) => id.trim())
			.filter((id) => id);

		selectedModels = (selectedModels ?? []).filter((modelId) => visibleModelIds.includes(modelId));

		if (selectedModels.length === 0) {
			const fallbackModelId =
				configuredDefaultModels.find((modelId) => visibleModelIds.includes(modelId)) ??
				visibleModelIds[0] ??
				'';
			selectedModels = fallbackModelId ? [fallbackModelId] : [''];
		}
	};

	const resetInput = () => {
		selectedToolIds = [];
		selectedFilterIds = [];
		webSearchEnabled = false;
		imageGenerationEnabled = false;
		codeInterpreterEnabled = false;
		thinkingModeEnabled = DEFAULT_THINKING_MODE_ENABLED;

		if (selectedModelIds.filter((id) => id).length > 0) {
			setDefaults();
		}
	};

	const setDefaults = async () => {
		if (!$tools) {
			tools.set(await getTools(localStorage.token));
		}
		if (!$functions) {
			functions.set(await getFunctions(localStorage.token));
		}
		if (selectedModels.length !== 1 && !atSelectedModel) {
			return;
		}

		const model = atSelectedModel ?? $models.find((m) => m.id === selectedModels[0]);
		if (model) {
			// Set Default Tools
			if (model?.info?.meta?.toolIds) {
				applySelectedToolIds(
					(model?.info?.meta?.toolIds ?? []).filter((id) => $tools.find((t) => t.id === id))
				);
			} else if ($settings?.tools) {
				applySelectedToolIds($settings.tools);
			} else {
				applySelectedToolIds(selectedToolIds.filter((id) => !id.startsWith('direct_server:')));
			}

			// Set Default Filters (Toggleable only)
			if (model?.info?.meta?.defaultFilterIds) {
				selectedFilterIds = model.info.meta.defaultFilterIds.filter((id) =>
					model?.filters?.find((f) => f.id === id)
				);
			}

			// Set Default Features
			if (model?.info?.meta?.defaultFeatureIds) {
				if (
					model.info?.meta?.capabilities?.['image_generation'] &&
					$config?.features?.enable_image_generation &&
					($user?.role === 'admin' || $user?.permissions?.features?.image_generation)
				) {
					imageGenerationEnabled = model.info.meta.defaultFeatureIds.includes('image_generation');
				}

				if (
					model.info?.meta?.capabilities?.['web_search'] &&
					$config?.features?.enable_web_search &&
					($user?.role === 'admin' || $user?.permissions?.features?.web_search)
				) {
					webSearchEnabled = model.info.meta.defaultFeatureIds.includes('web_search');
				}

				if (
					model.info?.meta?.capabilities?.['code_interpreter'] &&
					$config?.features?.enable_code_interpreter &&
					($user?.role === 'admin' || $user?.permissions?.features?.code_interpreter)
				) {
					codeInterpreterEnabled = model.info.meta.defaultFeatureIds.includes('code_interpreter');
				}
			}
		}
	};

	const showMessage = async (message, scroll = true) => {
		const _chatId = JSON.parse(JSON.stringify($chatId));
		let _messageId = JSON.parse(JSON.stringify(message.id));

		let messageChildrenIds = [];
		if (_messageId === null) {
			messageChildrenIds = Object.keys(history.messages).filter(
				(id) => history.messages[id].parentId === null
			);
		} else {
			messageChildrenIds = history.messages[_messageId].childrenIds;
		}

		while (messageChildrenIds.length !== 0) {
			_messageId = messageChildrenIds.at(-1);
			messageChildrenIds = history.messages[_messageId].childrenIds;
		}

		history.currentId = _messageId;

		await tick();

		if (($settings?.scrollOnBranchChange ?? true) && scroll) {
			const messageElement = document.getElementById(`message-${message.id}`);
			if (messageElement) {
				messageElement.scrollIntoView({ behavior: 'smooth', block: 'start' });
			}
		}

		await tick();
		await tick();
		await tick();

		saveChatHandler(_chatId, history);
	};

	const terminalEventHandler = (type: string, data: any) => {
		if (type === 'terminal:display_file') {
			if (!data?.path) return;
			displayFileHandler(data.path, { showControls, showFileNavPath });
		} else if (type === 'terminal:write_file' || type === 'terminal:replace_file_content') {
			if (!data?.path) return;
			showFileNavDir.set(data.path);
		} else if (type === 'terminal:run_command') {
			showFileNavDir.set('/');
		}
	};

	const chatEventHandler = async (event, cb) => {
		if (event.chat_id === $chatId) {
			await tick();
			let message = history.messages[event.message_id]
				? { ...history.messages[event.message_id] }
				: null;

			if (message) {
				const type = event?.data?.type ?? null;
				const data = event?.data?.data ?? null;

				if (type === 'status') {
					if (message?.statusHistory) {
						message.statusHistory = [...message.statusHistory, data];
					} else {
						message.statusHistory = [data];
					}
				} else if (type === 'chat:completion') {
					await chatCompletionEventHandler(data, message, event.chat_id);
					return;
				} else if (type === 'chat:tasks:cancel') {
					taskIds = null;
					setActiveChatIndicator(event.chat_id, false);
					pendingChatCompletion = null;
					const targetMessageId =
						event?.message_id && history.messages[event.message_id]
							? event.message_id
							: history.currentId;
					const responseMessage = targetMessageId ? history.messages[targetMessageId] : null;
					// Mark the canceled response branch as done using the message that emitted the event.
					if (responseMessage?.parentId && history.messages[responseMessage.parentId]) {
						const updates: Record<string, any> = {};
						for (const messageId of history.messages[responseMessage.parentId].childrenIds) {
							updates[messageId] = {
								...(history.messages[messageId] ?? {}),
								done: true
							};
						}
						replaceHistoryMessages(updates);
					} else if (targetMessageId && history.messages[targetMessageId]) {
						replaceHistoryMessage({
							...history.messages[targetMessageId],
							done: true
						});
					}
					return;
				} else if (type === 'chat:active') {
					if (data?.active === false) {
						let activeTaskIds: string[] = [];
						if (event?.chat_id && !event.chat_id.startsWith('local:')) {
							const taskRes = await getTaskIdsByChatId(
								localStorage.token,
								event.chat_id
							).catch((error) => {
								console.error(error);
								return null;
							});
							activeTaskIds = normalizeTaskIds(taskRes?.task_ids);
						}
						if (activeTaskIds.length > 0) {
							applyTaskIdsToHistory(activeTaskIds);
							return;
						}

						taskIds = null;
						setActiveChatIndicator(event.chat_id, false);
						if (await flushPendingChatCompletion(event.chat_id, activeTaskIds)) {
							return;
						}
						const targetMessageId =
							event?.message_id && history.messages[event.message_id]
								? event.message_id
								: history.currentId;
						const responseMessage = targetMessageId
							? history.messages[targetMessageId]
							: getCurrentBranchLastAssistantMessage(history);

						if (
							responseMessage?.role === 'assistant' &&
							(!hasRenderableAssistantPayload(responseMessage) ||
								responseMessage.done !== true)
						) {
							await finalizeIdleChatAfterReload(event.chat_id);
						} else {
							applyTaskIdsToHistory([]);
						}
					}
				} else if (type === 'chat:message:delta' || type === 'message') {
					message.content += data.content;
				} else if (type === 'chat:message' || type === 'replace') {
					message.content = data.content;
				} else if (type === 'chat:message:files' || type === 'files') {
					message.files = mergeFilesLists(message?.files ?? [], data?.files ?? []);
				} else if (type === 'chat:message:embeds' || type === 'embeds') {
					const didUpdateEmbeds = mergeEmbedsIntoMessage(message, data?.embeds);

					if (didUpdateEmbeds) {
						// Auto-scroll only when the visible embed set actually changes.
						await tick();
						setTimeout(() => {
							const embedEl = document.getElementById(`${event.message_id}-embeds-container`);
							if (embedEl) {
								embedEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
							}
						}, 100);
					}
				} else if (type === 'chat:message:error') {
					message.error = data.error;
				} else if (type === 'chat:message:follow_ups') {
					message.followUps = data.follow_ups;
					if (hasRenderableAssistantPayload(message)) {
						message.done = true;
					}
					replaceHistoryMessage(message);

					if (autoScroll) {
						scrollToBottom('smooth');
					}
					return;
				} else if (type === 'chat:message:favorite') {
					// Update message favorite status
					message.favorite = data.favorite;
				} else if (type === 'chat:title') {
					chatTitle.set(data);
					currentChatPage.set(1);
					await chats.set(await getChatList(localStorage.token, $currentChatPage));
				} else if (type === 'chat:tags') {
					chat = await getChatById(localStorage.token, $chatId);
					allTags.set(await getAllTags(localStorage.token));
				} else if (type === 'source' || type === 'citation') {
					if (data?.type === 'code_execution') {
						// Code execution; update existing code execution by ID, or add new one.
						if (!message?.code_executions) {
							message.code_executions = [];
						}

						const existingCodeExecutionIndex = message.code_executions.findIndex(
							(execution) => execution.id === data.id
						);

						if (existingCodeExecutionIndex !== -1) {
							message.code_executions = message.code_executions.map((execution, index) =>
								index === existingCodeExecutionIndex ? data : execution
							);
						} else {
							message.code_executions = [...message.code_executions, data];
						}
					} else {
						// Regular source.
						const sourceLikeItems = getSourceLikeItems(data);
						const incomingSources =
							sourceLikeItems.length > 0 ? sourceLikeItems : Array.isArray(data) ? data : [data];
						message.sources = mergeMessageSources(message?.sources ?? [], incomingSources);
					}
				} else if (type === 'notification') {
					const toastType = data?.type ?? 'info';
					const toastContent = data?.content ?? '';

					if (toastType === 'success') {
						toast.success(toastContent);
					} else if (toastType === 'error') {
						toast.error(toastContent);
					} else if (toastType === 'warning') {
						toast.warning(toastContent);
					} else {
						toast.info(toastContent);
					}
				} else if (type === 'confirmation') {
					eventCallback = cb;

					eventConfirmationInput = false;
					showEventConfirmation = true;

					eventConfirmationTitle = data.title;
					eventConfirmationMessage = data.message;
				} else if (type === 'capability:install:confirm') {
					eventCallback = cb;
					capabilityInstallRequest = data;
					showCapabilityInstallDialog = true;
				} else if (type === 'execute') {
					eventCallback = cb;

					try {
						// Use Function constructor to evaluate code in a safer way
						const asyncFunction = new Function(`return (async () => { ${data.code} })()`);
						const result = await asyncFunction(); // Await the result of the async function

						if (cb) {
							cb(result);
						}
					} catch (error) {
						console.error('Error executing code:', error);
					}
				} else if (type === 'input') {
					eventCallback = cb;

					eventConfirmationInput = true;
					showEventConfirmation = true;

					eventConfirmationTitle = data.title;
					eventConfirmationMessage = data.message;
					eventConfirmationInputPlaceholder = data.placeholder;
					eventConfirmationInputValue = data?.value ?? '';
					eventConfirmationInputType = data?.type ?? '';
				} else if (type.startsWith('terminal:')) {
					terminalEventHandler(type, data);
				} else {
					console.log('Unknown message type', data);
				}

				replaceHistoryMessage(message);
			}
		}
	};

	const onMessageHandler = async (event: {
		origin: string;
		data: { type: string; text: string };
	}) => {
		if (event.origin !== window.origin) {
			return;
		}

		if (event.data.type === 'action:submit') {
			console.debug(event.data.text);

			if (prompt !== '') {
				cancelPendingNewChatInit();
				await tick();
				submitPrompt(prompt);
			}
		}

		// Replace with your iframe's origin
		if (event.data.type === 'input:prompt') {
			console.debug(event.data.text);

			const inputElement = document.getElementById('chat-input');

			if (inputElement) {
				messageInput?.setText(event.data.text);
				inputElement.focus();
			}
		}

		if (event.data.type === 'input:prompt:submit') {
			console.debug(event.data.text);

			if (event.data.text !== '') {
				cancelPendingNewChatInit();
				await tick();
				submitPrompt(event.data.text);
			}
		}
	};

	const savedModelIds = async () => {
		if (
			$selectedFolder &&
			selectedModels.filter((modelId) => modelId !== '').length > 0 &&
			JSON.stringify($selectedFolder?.data?.model_ids) !== JSON.stringify(selectedModels)
		) {
			const res = await updateFolderById(localStorage.token, $selectedFolder.id, {
				data: {
					model_ids: selectedModels
				}
			});
		}
	};

	$: if (selectedModels !== null) {
		savedModelIds();
	}

	const stopAudio = () => {
		try {
			speechSynthesis.cancel();
			$audioQueue?.stop();
		} catch {}
	};

	onMount(() => {
		loading = true;
		window.addEventListener('message', onMessageHandler);
		$socket?.on('events', chatEventHandler);
		const handleVisibility = () => {
			if (document.visibilityState === 'visible') {
				refreshActiveTasks();
			}
		};
		document.addEventListener('visibilitychange', handleVisibility);
		window.addEventListener('focus', handleVisibility);

		$audioQueue?.destroy();

		const audioQueueInstance = new AudioQueue(document.getElementById('audioElement'));
		audioQueue.set(audioQueueInstance);

		// Reset direct terminal enabled states — selectedTerminalId starts null on every page load
		if ($settings?.terminalServers?.some((s) => s.enabled)) {
			settings.set({
				...$settings,
				terminalServers: ($settings.terminalServers ?? []).map((s) => ({ ...s, enabled: false }))
			});
		}

		const pageSubscribe = page.subscribe(async (p) => {
			if (p.url.pathname === '/') {
				await tick();
				await initNewChat();

				// Re-fetch banners on navigation to homepage so newly configured banners appear
				try {
					banners.set(await getBanners(localStorage.token).catch(() => []));
				} catch (e) {
					console.error('Failed to refresh banners:', e);
				}
			} else {
				initNewChatRunId += 1;
			}

			stopAudio();
		});

		const showControlsSubscribe = showControls.subscribe(async (value) => {
			await tick();
			if (controlPane && !$mobile) {
				try {
					if (value) {
						controlPaneComponent?.openPane();
					} else {
						controlPane.collapse();
					}
				} catch (e) {
					// ignore
				}
			}

			if (!value) {
				showCallOverlay.set(false);
				showArtifacts.set(false);
				showEmbeds.set(false);
				showFilePreview.set(false);
			}
		});

		const selectedFolderSubscribe = selectedFolder.subscribe(async (folder) => {
			await tick();
			if (
				folder?.data?.model_ids &&
				JSON.stringify(selectedModels) !== JSON.stringify(folder.data.model_ids)
			) {
				selectedModels = folder.data.model_ids;

				console.log('Set selectedModels from folder data:', selectedModels);
			}
		});

		const storageChatInput = sessionStorage.getItem(
			`chat-input${chatIdProp ? `-${chatIdProp}` : ''}`
		);

		const init = async () => {
			await resetAuxiliaryPanels();

			if (storageChatInput) {
				prompt = '';
				messageInput?.setText('');

				files = [];
				sessionToolIds = [];
				sessionSkillIds = [];
				selectedToolIds = [];
				selectedFilterIds = [];
				webSearchEnabled = false;
				imageGenerationEnabled = false;
				codeInterpreterEnabled = false;

				try {
					const input = JSON.parse(storageChatInput);

					if (!$temporaryChatEnabled) {
						messageInput?.setText(input.prompt);
						files = sanitizeFilesList(input.files ?? []);
						applySelectedToolIds(input.selectedToolIds ?? []);
						selectedFilterIds = input.selectedFilterIds;
						webSearchEnabled = input.webSearchEnabled;
						imageGenerationEnabled = input.imageGenerationEnabled;
						codeInterpreterEnabled = input.codeInterpreterEnabled;
						thinkingModeEnabled = normalizeThinkingModeEnabled(
							input.thinkingModeEnabled
						);
					}
				} catch (e) {}
			}

			const chatInput = document.getElementById('chat-input');
			chatInput?.focus();
		};
		init();

		return () => {
			try {
				pageSubscribe();
				showControlsSubscribe();
				selectedFolderSubscribe();
				window.removeEventListener('message', onMessageHandler);
				document.removeEventListener('visibilitychange', handleVisibility);
				window.removeEventListener('focus', handleVisibility);
				$socket?.off('events', chatEventHandler);
				audioQueueInstance?.destroy();
				audioQueue.set(null);
			} catch (e) {
				console.error(e);
			}
		};
	});

	// File upload functions

	const uploadGoogleDriveFile = async (fileData) => {
		console.log('Starting uploadGoogleDriveFile with:', {
			id: fileData.id,
			name: fileData.name,
			url: fileData.url,
			headers: {
				Authorization: `Bearer ${token}`
			}
		});

		// Validate input
		if (!fileData?.id || !fileData?.name || !fileData?.url || !fileData?.headers?.Authorization) {
			throw new Error('Invalid file data provided');
		}

		const tempItemId = uuidv4();
		const fileItem = {
			type: 'file',
			file: '',
			id: null,
			url: fileData.url,
			name: fileData.name,
			collection_name: '',
			status: 'uploading',
			error: '',
			itemId: tempItemId,
			size: 0
		};

		try {
			files = [...files, fileItem];
			console.log('Processing web file with URL:', fileData.url);

			// Configure fetch options with proper headers
			const fetchOptions = {
				headers: {
					Authorization: fileData.headers.Authorization,
					Accept: '*/*'
				},
				method: 'GET'
			};

			// Attempt to fetch the file
			console.log('Fetching file content from Google Drive...');
			const fileResponse = await fetch(fileData.url, fetchOptions);

			if (!fileResponse.ok) {
				const errorText = await fileResponse.text();
				throw new Error(`Failed to fetch file (${fileResponse.status}): ${errorText}`);
			}

			// Get content type from response
			const contentType = fileResponse.headers.get('content-type') || 'application/octet-stream';
			console.log('Response received with content-type:', contentType);

			// Convert response to blob
			console.log('Converting response to blob...');
			const fileBlob = await fileResponse.blob();

			if (fileBlob.size === 0) {
				throw new Error('Retrieved file is empty');
			}

			console.log('Blob created:', {
				size: fileBlob.size,
				type: fileBlob.type || contentType
			});

			// Create File object with proper MIME type
			const file = new File([fileBlob], fileData.name, {
				type: fileBlob.type || contentType
			});

			console.log('File object created:', {
				name: file.name,
				size: file.size,
				type: file.type
			});

			if (file.size === 0) {
				throw new Error('Created file is empty');
			}

			// If the file is an audio file, provide the language for STT.
			let metadata = null;
			if (
				(file.type.startsWith('audio/') || file.type.startsWith('video/')) &&
				$settings?.audio?.stt?.language
			) {
				metadata = {
					language: $settings?.audio?.stt?.language
				};
			}

			// Upload file to server
			console.log('Uploading file to server...');
			const uploadedFile = await uploadFile(localStorage.token, file, metadata);

			if (!uploadedFile) {
				throw new Error('Server returned null response for file upload');
			}

			console.log('File uploaded successfully:', uploadedFile);

			// Update file item with upload results
			fileItem.status = 'uploaded';
			fileItem.file = uploadedFile;
			fileItem.id = uploadedFile.id;
			fileItem.size = file.size;
			fileItem.collection_name = uploadedFile?.meta?.collection_name;
			fileItem.url = `${uploadedFile.id}`;

			files = files;
			toast.success($i18n.t('File uploaded successfully'));
		} catch (e) {
			console.error('Error uploading file:', e);
			files = files.filter((f) => f.itemId !== tempItemId);
			toast.error(
				$i18n.t('Error uploading file: {{error}}', {
					error: e.message || 'Unknown error'
				})
			);
		}
	};

	const uploadWeb = async (urls) => {
		if ($user?.role !== 'admin' && !($user?.permissions?.chat?.web_upload ?? true)) {
			toast.error($i18n.t('You do not have permission to upload web content.'));
			return;
		}

		if (!Array.isArray(urls)) {
			urls = [urls];
		}

		// Create file items first
		const fileItems = urls.map((url) => ({
			type: 'text',
			name: url,
			collection_name: '',
			status: 'uploading',
			context: 'full',
			url,
			error: ''
		}));

		// Display all items at once
		files = [...files, ...fileItems];

		for (const fileItem of fileItems) {
			try {
				const res = isYoutubeUrl(fileItem.url)
					? await processYoutubeVideo(localStorage.token, fileItem.url)
					: await processWeb(localStorage.token, '', fileItem.url);

				if (res) {
					fileItem.status = 'uploaded';
					fileItem.collection_name = res.collection_name;
					fileItem.file = {
						...res.file,
						...fileItem.file
					};
				}

				files = [...files];
			} catch (e) {
				files = files.filter((f) => f.name !== url);
				toast.error(`${e}`);
			}
		}
	};

	const onUpload = async (event) => {
		const { type, data } = event;

		if (type === 'google-drive') {
			await uploadGoogleDriveFile(data);
		} else if (type === 'web') {
			await uploadWeb(data);
		}
	};

	const scheduleArtifactContents = () => {
		cancelAnimationFrame(contentsRAF);
		contentsRAF = requestAnimationFrame(() => {
			getContents();
			contentsRAF = null;
		});
	};

	const onHistoryChange = (historyData, artifactsVisible: boolean) => {
		if (!historyData) {
			pendingArtifactRefresh = false;
			artifactContents.set([]);
			return;
		}
		if (!artifactsVisible) {
			pendingArtifactRefresh = true;
			return;
		}
		scheduleArtifactContents();
	};

	$: onHistoryChange(history, $showArtifacts);
	$: if ($showArtifacts && pendingArtifactRefresh && history) {
		pendingArtifactRefresh = false;
		scheduleArtifactContents();
	}

	const getContents = () => {
		const messages = history ? createMessagesList(history, history.currentId) : [];
		let contents = [];
		messages.forEach((message) => {
			if (message?.role !== 'user' && message?.content) {
				const {
					codeBlocks: codeBlocks,
					html: htmlContent,
					css: cssContent,
					js: jsContent
				} = getCodeBlockContents(message.content);

				if (htmlContent || cssContent || jsContent) {
					const renderedContent = `
                        <!DOCTYPE html>
                        <html lang="en">
                        <head>
                            <meta charset="UTF-8">
                            <meta name="viewport" content="width=device-width, initial-scale=1.0">
							<${''}style>
								body {
									background-color: white; /* Ensure the iframe has a white background */
								}

								${cssContent}
							</${''}style>
                        </head>
                        <body>
                            ${htmlContent}

							<${''}script>
                            	${jsContent}
							</${''}script>
                        </body>
                        </html>
                    `;
					contents = [...contents, { type: 'iframe', content: renderedContent }];
				} else {
					// Check for SVG content
					for (const block of codeBlocks) {
						if (block.lang === 'svg' || (block.lang === 'xml' && block.code.includes('<svg'))) {
							contents = [...contents, { type: 'svg', content: block.code }];
						}
					}
				}
			}
		});

		artifactContents.set(contents);
	};

	//////////////////////////
	// Web functions
	//////////////////////////

	const isActiveNewChatInit = (runId: number) => {
		return runId === initNewChatRunId && $page.url.pathname === '/' && !chatIdProp;
	};

	const resetAuxiliaryPanels = async () => {
		selectedGeneratedFilePreviewId.set(null);
		selectedTerminalId.set(null);
		showFileNavPath.set(null);
		showFileNavDir.set(null);
		await showOverview.set(false);
		await showCallOverlay.set(false);
		await showArtifacts.set(false);
		await showEmbeds.set(false);
		await showFilePreview.set(false);
		await showControls.set(false);
	};

	const initNewChat = async () => {
		if ($page.url.pathname !== '/') {
			await goto('/', { replaceState: true, noScroll: true, keepFocus: true });
			return;
		}

		const runId = ++initNewChatRunId;
		loading = true;
		sessionToolIds = [];
		sessionSkillIds = [];

		if ($user?.role !== 'admin' && $user?.permissions?.chat?.temporary_enforced) {
			await temporaryChatEnabled.set(true);
		}

		if ($settings?.temporaryChatByDefault ?? false) {
			if ($temporaryChatEnabled === false) {
				await temporaryChatEnabled.set(true);
			} else if ($temporaryChatEnabled === null) {
				// if set to null set to false; refer to temp chat toggle click handler
				await temporaryChatEnabled.set(false);
			}
		}

		if ($user?.role !== 'admin' && !$user?.permissions?.chat?.temporary) {
			await temporaryChatEnabled.set(false);
		}

		if (!isActiveNewChatInit(runId)) {
			return;
		}

		const availableModels = $models
			.filter((m) => !(m?.info?.meta?.hidden ?? false))
			.map((m) => m.id);

		const defaultModels = $config?.default_models ? $config?.default_models.split(',') : [];

		if ($page.url.searchParams.get('models') || $page.url.searchParams.get('model')) {
			const urlModels = normalizeModelSelection(
				($page.url.searchParams.get('models') || $page.url.searchParams.get('model') || '')?.split(
					','
				)
			);

			if (urlModels.length === 1) {
				if (!$models.find((m) => m.id === urlModels[0])) {
					// Model not found; open model selector and prefill
					const modelSelectorButton = document.getElementById('model-selector-0-button');
					if (modelSelectorButton) {
						modelSelectorButton.click();
						await tick();

						const modelSelectorInput = document.getElementById('model-search-input');
						if (modelSelectorInput) {
							modelSelectorInput.focus();
							modelSelectorInput.value = urlModels[0];
							modelSelectorInput.dispatchEvent(new Event('input'));
						}
					}
				} else {
					// Model found; set it as selected
					selectedModels = urlModels;
				}
			} else {
				// Multiple models; set as selected
				selectedModels = urlModels;
			}

			// Unavailable models filtering
			selectedModels = selectedModels.filter((modelId) =>
				$models.map((m) => m.id).includes(modelId)
			);
		} else {
			if ($selectedFolder?.data?.model_ids) {
				// Set from folder model IDs
				selectedModels = normalizeModelSelection($selectedFolder?.data?.model_ids);
			} else {
				if (sessionStorage.selectedModels) {
					// Set from session storage (temporary selection)
					selectedModels = normalizeModelSelection(JSON.parse(sessionStorage.selectedModels));
					sessionStorage.removeItem('selectedModels');
				} else {
					if ($settings?.models) {
						// Set from user settings
						selectedModels = normalizeModelSelection($settings?.models);
					} else if (defaultModels && defaultModels.length > 0) {
						// Set from default models
						selectedModels = normalizeModelSelection(defaultModels);
					}
				}
			}

			// Unavailable & hidden models filtering
			selectedModels = selectedModels.filter((modelId) => availableModels.includes(modelId));
		}

		// Ensure at least one model is selected
		if (selectedModels.length === 0 || (selectedModels.length === 1 && selectedModels[0] === '')) {
			if (availableModels.length > 0) {
				if (defaultModels && defaultModels.length > 0) {
					selectedModels = defaultModels.filter((modelId) => availableModels.includes(modelId));
				}

				if (
					selectedModels.length === 0 ||
					(selectedModels.length === 1 && selectedModels[0] === '')
				) {
					// Only fall back to first available model if default models didn't resolve
					selectedModels = [availableModels?.at(0) ?? ''];
				}
			} else {
				selectedModels = [''];
			}
		}

		await resetAuxiliaryPanels();

		if (!isActiveNewChatInit(runId)) {
			return;
		}

		autoScroll = true;

		resetInput();
		await chatId.set('');
		await chatTitle.set('');

		history = {
			messages: {},
			currentId: null
		};

		chatFiles = [];
		params = {};
		taskIds = null;
		pendingChatCompletion = null;
		messageQueue = [];

		if ($page.url.searchParams.get('youtube')) {
			await uploadWeb(`https://www.youtube.com/watch?v=${$page.url.searchParams.get('youtube')}`);
		}

		if ($page.url.searchParams.get('load-url')) {
			await uploadWeb($page.url.searchParams.get('load-url'));
		}

		if (!isActiveNewChatInit(runId)) {
			return;
		}

		if ($page.url.searchParams.get('web-search') === 'true') {
			webSearchEnabled = true;
		}

		if ($page.url.searchParams.get('image-generation') === 'true') {
			imageGenerationEnabled = true;
		}

		if ($page.url.searchParams.get('code-interpreter') === 'true') {
			codeInterpreterEnabled = true;
		}

		if ($page.url.searchParams.get('tools')) {
			selectedToolIds = $page.url.searchParams
				.get('tools')
				?.split(',')
				.map((id) => id.trim())
				.filter((id) => id);
		} else if ($page.url.searchParams.get('tool-ids')) {
			selectedToolIds = $page.url.searchParams
				.get('tool-ids')
				?.split(',')
				.map((id) => id.trim())
				.filter((id) => id);
		}

		applySelectedToolIds(selectedToolIds ?? []);

		if ($page.url.searchParams.get('call') === 'true') {
			showFilePreview.set(false);
			showCallOverlay.set(true);
			showControls.set(true);
		}

		if ($page.url.searchParams.get('q')) {
			const q = $page.url.searchParams.get('q') ?? '';
			messageInput?.setText(q);

			if (q) {
				if (($page.url.searchParams.get('submit') ?? 'true') === 'true') {
					await tick();
					submitPrompt(q);
				}
			}
		}

		selectedModels = selectedModels.map((modelId) =>
			$models.map((m) => m.id).includes(normalizeModelId(modelId)) ? normalizeModelId(modelId) : ''
		);

		if (!isActiveNewChatInit(runId)) {
			return;
		}

		loading = false;

		const chatInput = document.getElementById('chat-input');
		setTimeout(() => chatInput?.focus(), 0);
	};

	const loadChat = async () => {
		chatId.set(chatIdProp);

		if ($temporaryChatEnabled) {
			temporaryChatEnabled.set(false);
		}

		chat = await getChatById(
			localStorage.token,
			$chatId,
			buildHistoryRequestParams(HISTORY_TAIL_DEFAULT)
		).catch(async (error) => {
			await goto('/');
			return null;
		});

		if (chat) {
			const requestedChatId = $chatId;
			const tagsPromise = getTagsById(localStorage.token, requestedChatId).catch(async () => []);
			const taskIdsPromise = getTaskIdsByChatId(localStorage.token, requestedChatId)
				.then((taskRes) => normalizeTaskIds(taskRes?.task_ids))
				.catch(() => []);
			pendingTaskIdsLoad = taskIdsPromise;

			const chatContent = chat.chat;

			if (chatContent) {
				sessionToolIds = dedupeIds(chat?.meta?.session_tool_ids ?? []);
				sessionSkillIds = dedupeIds(chat?.meta?.session_skill_ids ?? []);

				selectedModels = normalizeModelSelection(
					(chatContent?.models ?? undefined) !== undefined
						? chatContent.models
						: [chatContent.models ?? '']
				);

				if (!($user?.role === 'admin' || ($user?.permissions?.chat?.multiple_models ?? true))) {
					selectedModels = selectedModels.length > 0 ? [selectedModels[0]] : [''];
				}

				oldSelectedModelIds = structuredClone(selectedModels);

				history = prepareHistory(resolveHistoryFromChatContent(chatContent));
				const metaFromResponse = normalizeHistoryMeta(chat?.meta);
				if (metaFromResponse) {
					historyMeta = metaFromResponse;
				} else {
					const inferredTruncated = inferHistoryTruncation(history);
					historyMeta = {
						truncated: inferredTruncated,
						totalMessages: null,
						canLoadMore: inferredTruncated,
						windowSize: null
					};
				}

				chatTitle.set(chatContent.title);

				params = chatContent?.params ?? {};
				chatFiles = sanitizeFilesList(chatContent?.files ?? []);
				applySelectedToolIds(selectedToolIds ?? []);
				void tagsPromise.then((nextTags) => {
					if (isCurrentChatRequest(requestedChatId)) {
						tags = nextTags;
					}
				});
				void taskIdsPromise.then((nextTaskIds) => {
					if (pendingTaskIdsLoad === taskIdsPromise) {
						pendingTaskIdsLoad = null;
					}

					if (!isCurrentChatRequest(requestedChatId)) {
						return;
					}

					applyTaskIdsToHistory(nextTaskIds);
				});

				autoScroll = true;

				return true;
			} else {
				return null;
			}
		}
	};

	const refreshHistoryFromServer = async (requestedChatId: string) => {
		if (!requestedChatId || requestedChatId.startsWith('local:')) {
			return false;
		}

		const chatRes = await getChatById(
			localStorage.token,
			requestedChatId,
			buildHistoryRequestParams(HISTORY_TAIL_DEFAULT)
		).catch(() => null);

		if (!chatRes?.chat || !isCurrentChatRequest(requestedChatId)) {
			return false;
		}

		const chatContent = chatRes.chat;
		history = prepareHistory(resolveHistoryFromChatContent(chatContent));
		const metaFromResponse = normalizeHistoryMeta(chatRes.meta);
		historyMeta =
			metaFromResponse ?? {
				truncated: false,
				totalMessages: Object.keys(history?.messages ?? {}).length,
				canLoadMore: false,
				windowSize: Object.keys(history?.messages ?? {}).length
			};
		chatFiles = sanitizeFilesList(chatContent?.files ?? []);
		chatTitle.set(chatContent.title);
		return true;
	};

	const scrollToBottom = async (behavior = 'auto') => {
		await tick();
		if (messagesContainerElement) {
			messagesContainerElement.scrollTo({
				top: messagesContainerElement.scrollHeight,
				behavior
			});
		}
	};

	let scrollRAF: number | null = null;
	let contentsRAF: number | null = null;
	const scheduleScrollToBottom = () => {
		if (!scrollRAF) {
			scrollRAF = requestAnimationFrame(async () => {
				scrollRAF = null;
				await scrollToBottom();
			});
		}
	};

	const TOOL_CALL_BLOCK_START_REGEX = /<details\b[^>]*\btype="tool_calls"[^>]*>/i;
	const TOOL_CALL_BLOCK_REGEX = /<details\b[^>]*\btype="tool_calls"[^>]*>[\s\S]*?<\/details>/gim;
	const TOOL_CALL_OPEN_TAG_REGEX = /^<details\b([^>]*)>/i;
	const TOOL_CALL_ATTR_REGEX = /(\w+)="([^"]*)"/g;
	const LEAKED_TOOL_ATTR_LINE_REGEX =
		/(^|\n)\s*(?:type="tool_calls"|name="[^"\n]*"|tool_id="[^"\n]*"|tool_name="[^"\n]*"|arguments="[^"\n]*"|result="[^"\n]*"|done="(?:true|false)"\s+status="[^"\n]*")[^\n]*(?=\n|$)/gi;
	const SOURCE_SECTION_LINE_REGEX =
		/(^|\n)\s*(?:#{1,6}\s*)?(?:[*_]{1,2}\s*)?(参考来源|Sources|References)(?:\s*[*_]{1,2})?\s*[:：]?\s*(?:\n|$)/i;
	const SOURCE_SECTION_INLINE_REGEX =
		/(^|\n)\s*(?:#{1,6}\s*)?(?:[*_]{1,2}\s*)?(参考来源|Sources|References)(?:\s*[*_]{1,2})?\s*[:：]\s*(?:\[[^\]]+\]|\d+[.)]|[-*]|\s*https?:\/\/|$)/i;
	const HISTORICAL_REPLAY_NOTE_START = 'Historical tool attempt retained as plain context only.';
	const HISTORICAL_REPLAY_NOTE_SECOND_SENTENCE = 'Do not replay it as a new tool call.';
	const HISTORICAL_REPLAY_NOTE_LINE_PREFIXES = [
		'Tool:',
		'Status:',
		'Arguments:',
		'Replay was skipped because',
		'Reported output:'
	];

	const getToolCallAttrs = (block: string): Record<string, string> => {
		const openTag = block.match(TOOL_CALL_OPEN_TAG_REGEX)?.[1] ?? '';
		const attrs: Record<string, string> = {};
		for (const item of openTag.matchAll(TOOL_CALL_ATTR_REGEX)) {
			attrs[item[1]] = item[2];
		}
		return attrs;
	};

	const getToolCallSignature = (block: string, attrs: Record<string, string>): string => {
		const callKey = (attrs.call_key || '').trim();
		if (callKey) return `call_key:${callKey}`;

		const id = (attrs.id || '').trim();
		if (id) return `id:${id}`;

		return `block:${block.trim()}`;
	};

	const isTerminalToolCall = (attrs: Record<string, string>): boolean => {
		const status = (attrs.status || '').trim().toLowerCase();
		return ['success', 'error', 'timeout'].includes(status) || attrs.done === 'true';
	};

	const collapseDuplicateToolCallBlocks = (content: string): string => {
		if (!content || !content.includes('type="tool_calls"')) return content;

		const matches = Array.from(content.matchAll(TOOL_CALL_BLOCK_REGEX));
		if (matches.length <= 1) return content;

		const blocks = matches.map((match, index) => {
			const block = match[0] || '';
			const attrs = getToolCallAttrs(block);
			return {
				block,
				attrs,
				start: match.index ?? -1,
				end: (match.index ?? -1) + block.length,
				index
			};
		});

		const chosenBySignature = new Map<
			string,
			{ block: string; attrs: Record<string, string>; start: number; end: number; index: number }
		>();

		for (const item of blocks) {
			if (item.start < 0) continue;

			const signature = getToolCallSignature(item.block, item.attrs);
			const existing = chosenBySignature.get(signature);
			if (!existing) {
				chosenBySignature.set(signature, item);
				continue;
			}

			const existingTerminal = isTerminalToolCall(existing.attrs);
			const nextTerminal = isTerminalToolCall(item.attrs);
			if ((nextTerminal && !existingTerminal) || nextTerminal === existingTerminal) {
				chosenBySignature.set(signature, item);
			}
		}

		const keptBlocks = Array.from(chosenBySignature.values()).sort((a, b) => a.start - b.start);
		let cursor = 0;
		let normalized = '';
		for (const item of keptBlocks) {
			normalized += content.slice(cursor, item.start);
			normalized += item.block;
			cursor = item.end;
		}
		normalized += content.slice(cursor);

		return normalized.replace(/\n{3,}/g, '\n\n').trim();
	};

	const stripInterstitialToolProgressText = (content: string) => {
		if (!content || !content.includes('type="tool_calls"')) return content;

		const matches = Array.from(content.matchAll(TOOL_CALL_BLOCK_REGEX));
		if (matches.length < 2) return content;

		let cursor = 0;
		let normalized = '';

		for (let index = 0; index < matches.length; index += 1) {
			const match = matches[index];
			const block = match[0] ?? '';
			const start = match.index ?? -1;
			if (start < 0) continue;

			normalized += content.slice(cursor, start);
			normalized += block;
			cursor = start + block.length;

			const nextMatch = matches[index + 1];
			if (!nextMatch || nextMatch.index === undefined) {
				continue;
			}

			const between = content.slice(cursor, nextMatch.index);
			const collapsed = between.replace(/\s+/g, '');
			if (!collapsed || between.includes('<details')) {
				normalized += between;
			}
			cursor = nextMatch.index;
		}

		normalized += content.slice(cursor);
		return normalized.replace(/\n{3,}/g, '\n\n').trim();
	};

	const isHistoricalReplayNoteLine = (line: string) => {
		const trimmed = line.trim();
		return HISTORICAL_REPLAY_NOTE_LINE_PREFIXES.some((prefix) => trimmed.startsWith(prefix));
	};

	const extractHistoricalReplayNoteRemainder = (line: string): string | null => {
		const trimmed = line.trim();
		if (!trimmed) {
			return null;
		}

		if (trimmed.startsWith(HISTORICAL_REPLAY_NOTE_START)) {
			let remainder = trimmed.slice(HISTORICAL_REPLAY_NOTE_START.length).trimStart();
			if (remainder.startsWith(HISTORICAL_REPLAY_NOTE_SECOND_SENTENCE)) {
				remainder = remainder
					.slice(HISTORICAL_REPLAY_NOTE_SECOND_SENTENCE.length)
					.trimStart();
			}
			return remainder && !isHistoricalReplayNoteLine(remainder) ? remainder : null;
		}

		if (!trimmed.startsWith('Reported output:')) {
			return null;
		}

		const remainder = trimmed.slice('Reported output:'.length).trim();
		if (!remainder) {
			return null;
		}

		const parts = remainder.split(/(?:\s{2,}|\t+)/, 2);
		if (parts.length < 2) {
			return null;
		}

		const candidate = parts[1]?.trim() ?? '';
		return candidate && !isHistoricalReplayNoteLine(candidate) ? candidate : null;
	};

	const stripHistoricalReplayNote = (content: string) => {
		if (!content || !content.includes(HISTORICAL_REPLAY_NOTE_START)) return content;

		const lines = content.split('\n');
		const kept: string[] = [];
		let inBlock = false;

		for (const line of lines) {
			const trimmed = line.trim();

			if (!inBlock) {
				if (trimmed.startsWith(HISTORICAL_REPLAY_NOTE_START)) {
					inBlock = true;

					const remainder = extractHistoricalReplayNoteRemainder(line);
					if (remainder) {
						kept.push(remainder);
						inBlock = false;
					}
					continue;
				}

				kept.push(line);
				continue;
			}

			if (!trimmed) {
				continue;
			}

			const remainder = extractHistoricalReplayNoteRemainder(line);
			if (remainder) {
				kept.push(remainder);
				inBlock = false;
				continue;
			}

			if (isHistoricalReplayNoteLine(trimmed)) {
				if (trimmed.startsWith('Reported output:')) {
					inBlock = false;
				}
				continue;
			}

			inBlock = false;
			kept.push(line);
		}

		return kept.join('\n').replace(/\n{3,}/g, '\n\n').trim();
	};

	const normalizeAssistantResponseContent = (content: string) => {
		if (!content) return content;

		let normalized = stripHistoricalReplayNote(content);
		if (!normalized) return normalized;

		const needsStructuralNormalization =
			normalized.includes('<details') ||
			normalized.includes('type="tool_calls"') ||
			normalized.includes('tool_id="') ||
			normalized.includes('tool_name="') ||
			normalized.includes('arguments="') ||
			normalized.includes('result="') ||
			normalized.includes('参考来源') ||
			normalized.includes('Sources') ||
			normalized.includes('References');

		if (!needsStructuralNormalization) {
			return normalized;
		}

		const toolCallStartIndex = normalized.search(TOOL_CALL_BLOCK_START_REGEX);
		if (toolCallStartIndex > 0) {
			const leading = normalized.slice(0, toolCallStartIndex);
			if (!leading.includes('<details')) {
				normalized = normalized.slice(toolCallStartIndex);
			}
		}

		normalized = collapseDuplicateToolCallBlocks(normalized);
		normalized = stripInterstitialToolProgressText(normalized);
		normalized = normalizeToolCallContent(normalized);
		normalized = normalized.replace(LEAKED_TOOL_ATTR_LINE_REGEX, '$1').replace(/\n{3,}/g, '\n\n');

		normalized = normalized.replace(
			/(^|[^\n])\s*#{1,6}\s*(参考来源|Sources|References)(?=\s|$)/g,
			(_, prefix: string, heading: string) =>
				`${prefix}\n\n${heading === 'Sources' || heading === 'References' ? '参考来源' : heading}`
		);
		normalized = normalized.replace(
			/(^|\n)#{1,6}\s*(参考来源|Sources|References)(?=\s|$)/g,
			(_, prefix: string, heading: string) =>
				`${prefix}${heading === 'Sources' || heading === 'References' ? '参考来源' : heading}`
		);

		const lineHeadingMatch = normalized.match(SOURCE_SECTION_LINE_REGEX);
		if (lineHeadingMatch?.index !== undefined) {
			normalized = normalized.slice(0, lineHeadingMatch.index).trimEnd();
		} else {
			const inlineHeadingMatch = normalized.match(SOURCE_SECTION_INLINE_REGEX);
			if (inlineHeadingMatch?.index !== undefined) {
				normalized = normalized.slice(0, inlineHeadingMatch.index).trimEnd();
			}
		}

		return normalized;
	};

	const chatCompletedHandler = async (_chatId, modelId, responseMessageId, messages) => {
		const requestContext =
			responseRequestContexts.get(responseMessageId) ?? captureRequestContext();
		const res = await chatCompleted(localStorage.token, {
			model: modelId,
			messages: messages.map((m) => ({
				id: m.id,
				role: m.role,
				content: m.content,
				info: m.info ? m.info : undefined,
				timestamp: m.timestamp,
				...(m.usage ? { usage: m.usage } : {}),
				...(m.sources ? { sources: m.sources } : {}),
				...(m.citations ? { citations: m.citations } : {})
			})),
			filter_ids:
				requestContext.selectedFilterIds.length > 0
					? requestContext.selectedFilterIds
					: undefined,
			model_item: $models.find((m) => m.id === modelId),
			chat_id: _chatId,
			session_id: $socket?.id,
			id: responseMessageId
		}).catch((error) => {
			toast.error(`${error}`);
			messages.at(-1).error = { content: error };

			return null;
		});

		if (res !== null && res.messages) {
			// Update chat history with the new messages
			for (const rawMessage of res.messages) {
				const message = normalizeHistoryMessage(rawMessage);
				if (message?.id) {
					history.messages[message.id] = mergeHistoryMessage(
						history.messages[message.id],
						message
					);
				}
			}
			prepareHistory(history);
		}

		await tick();
		if ($chatId == _chatId) {
			if (!$temporaryChatEnabled) {
				const historyToPersist = sanitizeHistoryForPersistence(structuredClone(history));
				chat = await updateChatById(localStorage.token, _chatId, {
					models: selectedModels,
					messages: createMessagesList(historyToPersist, historyToPersist.currentId),
					history: historyToPersist,
					params: params,
					files: chatFiles
				});

				currentChatPage.set(1);
				await chats.set(await getChatList(localStorage.token, $currentChatPage));
			}
		}

		taskIds = null;
		setActiveChatIndicator(_chatId, false);
		responseRequestContexts.delete(responseMessageId);

		// Drain the next compatible queue batch so per-message request toggles stay intact.
		if (messageQueue.length > 0) {
			await submitQueuedMessages(messageQueue);
		}
	};

	const chatActionHandler = async (_chatId, actionId, modelId, responseMessageId, event = null) => {
		const messages = createMessagesList(history, responseMessageId);

		const res = await chatAction(localStorage.token, actionId, {
			model: modelId,
			messages: messages.map((m) => ({
				id: m.id,
				role: m.role,
				content: m.content,
				info: m.info ? m.info : undefined,
				timestamp: m.timestamp,
				...(m.sources ? { sources: m.sources } : {}),
				...(m.citations ? { citations: m.citations } : {})
			})),
			...(event ? { event: event } : {}),
			model_item: $models.find((m) => m.id === modelId),
			chat_id: _chatId,
			session_id: $socket?.id,
			id: responseMessageId
		}).catch((error) => {
			toast.error(`${error}`);
			messages.at(-1).error = { content: error };
			return null;
		});

		if (res !== null && res.messages) {
			// Update chat history with the new messages
			for (const rawMessage of res.messages) {
				const message = normalizeHistoryMessage(rawMessage);
				if (!message?.id) {
					continue;
				}
				history.messages[message.id] = mergeHistoryMessage(
					history.messages[message.id],
					message
				);
			}
			prepareHistory(history);
		}

		await saveChatHandler(_chatId, history, {
			ensureLoaded: false,
			refreshList: true,
			allowInactiveTarget: true
		});
	};

	const getChatEventEmitter = async (modelId: string, chatId: string = '') => {
		return setInterval(() => {
			$socket?.emit('usage', {
				action: 'chat',
				model: modelId,
				chat_id: chatId
			});
		}, 1000);
	};

	const createMessagePair = async (userPrompt) => {
		messageInput?.setText('');
		ensureSelectedModels();
		if (selectedModels.length === 0 || selectedModels.includes('')) {
			toast.error($i18n.t('Model not selected'));
		} else {
			const modelId = selectedModels[0];
			const model = getModelById(modelId);

			if (!model) {
				toast.error($i18n.t('Model not found'));
				return;
			}

			const messages = createMessagesList(history, history.currentId);
			const parentMessage = messages.length !== 0 ? messages.at(-1) : null;

			const userMessageId = uuidv4();
			const responseMessageId = uuidv4();

			const userMessage = {
				id: userMessageId,
				parentId: parentMessage ? parentMessage.id : null,
				childrenIds: [responseMessageId],
				role: 'user',
				content: userPrompt ? userPrompt : `[PROMPT] ${userMessageId}`,
				timestamp: Math.floor(Date.now() / 1000)
			};

			const responseMessage = {
				id: responseMessageId,
				parentId: userMessageId,
				childrenIds: [],
				role: 'assistant',
				content: `[RESPONSE] ${responseMessageId}`,
				done: true,

				model: modelId,
				modelName: model?.name ?? modelId,
				modelIdx: 0,
				timestamp: Math.floor(Date.now() / 1000)
			};

			if (parentMessage) {
				parentMessage.childrenIds.push(userMessageId);
				history.messages[parentMessage.id] = parentMessage;
			}
			history.messages[userMessageId] = userMessage;
			history.messages[responseMessageId] = responseMessage;

			history.currentId = responseMessageId;

			await tick();

			if (autoScroll) {
				scrollToBottom();
			}

			if (messages.length === 0) {
				await initChatHandler(history);
			} else {
				await saveChatHandler($chatId, history);
			}
		}
	};

	const addMessages = async ({ modelId, parentId, messages }) => {
		const model = getModelById(modelId);

		let parentMessage = history.messages[parentId];
		let currentParentId = parentMessage ? parentMessage.id : null;
		for (const message of messages) {
			let messageId = uuidv4();

			if (message.role === 'user') {
				const userMessage = {
					id: messageId,
					parentId: currentParentId,
					childrenIds: [],
					timestamp: Math.floor(Date.now() / 1000),
					...message
				};

				if (parentMessage) {
					parentMessage.childrenIds.push(messageId);
					history.messages[parentMessage.id] = parentMessage;
				}

				history.messages[messageId] = userMessage;
				parentMessage = userMessage;
				currentParentId = messageId;
			} else {
				const responseMessage = {
					id: messageId,
					parentId: currentParentId,
					childrenIds: [],
					done: true,
					model: model?.id ?? modelId,
					modelName: model?.name ?? modelId,
					modelIdx: 0,
					timestamp: Math.floor(Date.now() / 1000),
					...message
				};

				if (parentMessage) {
					parentMessage.childrenIds.push(messageId);
					history.messages[parentMessage.id] = parentMessage;
				}

				history.messages[messageId] = responseMessage;
				parentMessage = responseMessage;
				currentParentId = messageId;
			}
		}

		history.currentId = currentParentId;
		await tick();

		if (autoScroll) {
			scrollToBottom();
		}

		if (messages.length === 0) {
			await initChatHandler(history);
		} else {
			await saveChatHandler($chatId, history);
		}
	};

	const chatCompletionEventHandler = async (data: any, message: any, chatId: string) => {
		message = { ...(message ?? {}) };
		const { id, done, choices, content, output, selected_model_id, error, usage, metadata } =
			data;
		const hasContent = Object.prototype.hasOwnProperty.call(data ?? {}, 'content');
		let hasVisibleResponseUpdate = false;
		const completionFiles = collectGeneratedFilesFromCompletionData(data);
		const completionEmbeds = mergeEmbedsLists(
			data?.embeds,
			data?.metadata?.embeds,
			data?.choices?.[0]?.message?.metadata?.embeds
		);

		if (completionFiles.length > 0) {
			message.files = mergeFilesLists(message?.files ?? [], completionFiles);
		}

		const didUpdateEmbeds =
			completionEmbeds.length > 0 && mergeEmbedsIntoMessage(message, completionEmbeds);

		// Store raw OR-aligned output items from backend
		if (output) {
			message.output = normalizeToolResponseOutput(output);
			hasVisibleResponseUpdate = true;
		}

		if (error) {
			await handleOpenAIError(error, message);
		}

		const incomingSources = getSourceLikeItems(data);
		if (incomingSources.length > 0) {
			message.sources = mergeMessageSources(message?.sources ?? [], incomingSources);
		}

		const completionMetadata = metadata ?? choices?.[0]?.message?.metadata;
		if (completionMetadata && typeof completionMetadata === 'object') {
			message.metadata = {
				...(message.metadata ?? {}),
				...completionMetadata
			};
			hasVisibleResponseUpdate = true;
		}

		if (choices) {
			if (choices[0]?.message?.content) {
				// Non-stream response
				message.content += choices[0]?.message?.content;
				message.content = normalizeAssistantResponseContent(message.content);
				hasVisibleResponseUpdate = true;
			} else {
				// Stream response
				let value = choices[0]?.delta?.content ?? '';
				if (message.content == '' && value == '\n') {
					console.log('Empty response');
				} else {
					message.content += value;
					message.content = normalizeAssistantResponseContent(message.content);
					hasVisibleResponseUpdate = true;

					if (navigator.vibrate && ($settings?.hapticFeedback ?? false)) {
						navigator.vibrate(5);
					}

					// Emit chat event for TTS (only when call overlay is active)
					if ($showCallOverlay) {
						const messageContentParts = getMessageContentParts(
							removeAllDetails(message.content),
							$config?.audio?.tts?.split_on ?? 'punctuation'
						);
						messageContentParts.pop();

						// dispatch only last sentence and make sure it hasn't been dispatched before
						if (
							messageContentParts.length > 0 &&
							messageContentParts[messageContentParts.length - 1] !== message.lastSentence
						) {
							message.lastSentence = messageContentParts[messageContentParts.length - 1];
							eventTarget.dispatchEvent(
								new CustomEvent('chat', {
									detail: {
										id: message.id,
										content: messageContentParts[messageContentParts.length - 1]
									}
								})
							);
						}
					}
				}
			}
		}

		if (hasContent) {
			// REALTIME_CHAT_SAVE is disabled
			message.content = normalizeAssistantResponseContent(content ?? '');
			hasVisibleResponseUpdate = true;

			if (navigator.vibrate && ($settings?.hapticFeedback ?? false)) {
				navigator.vibrate(5);
			}

			// Emit chat event for TTS (only when call overlay is active)
			if ($showCallOverlay) {
				const messageContentParts = getMessageContentParts(
					removeAllDetails(message.content),
					$config?.audio?.tts?.split_on ?? 'punctuation'
				);
				messageContentParts.pop();

				// dispatch only last sentence and make sure it hasn't been dispatched before
				if (
					messageContentParts.length > 0 &&
					messageContentParts[messageContentParts.length - 1] !== message.lastSentence
				) {
					message.lastSentence = messageContentParts[messageContentParts.length - 1];
					eventTarget.dispatchEvent(
						new CustomEvent('chat', {
							detail: {
								id: message.id,
								content: messageContentParts[messageContentParts.length - 1]
							}
						})
					);
				}
			}
		}

		if (selected_model_id) {
			message.selectedModelId = selected_model_id;
			message.arena = true;
		}

		if (usage) {
			message.usage = usage;
		}

		replaceHistoryMessage(message);

		if (didUpdateEmbeds) {
			await tick();
			setTimeout(() => {
				const embedEl = document.getElementById(`${message.id}-embeds-container`);
				if (embedEl) {
					embedEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
				}
			}, 100);
		}

		if (done) {
			const activeTaskIds = await resolveActiveTaskIds(chatId);
			if (hasRenderableAssistantPayload(message)) {
				pendingChatCompletion = null;
				await finalizeAssistantMessage(chatId, message);
				if (activeTaskIds.length > 0) {
					applyTaskIdsToHistory(activeTaskIds);
				}
			} else if (activeTaskIds.length > 0) {
				message.done = false;
				replaceHistoryMessage(message);
				applyTaskIdsToHistory(activeTaskIds);
				pendingChatCompletion = { chatId, messageId: message.id };
			} else {
				await finalizeAssistantMessage(chatId, message);
			}
		}

		console.log(data);
		await tick();

		if (autoScroll) {
			scheduleScrollToBottom();
		}
	};

	//////////////////////////
	// Chat functions
	//////////////////////////

	const submitPrompt = async (
		userPrompt,
		{
			_raw = false,
			requestContext: requestContextOverride = null
		}: {
			_raw?: boolean;
			requestContext?: ChatRequestContext | null;
		} = {}
	) => {
		console.log('submitPrompt', userPrompt, $chatId);
		// Cancel any in-flight landing-page init so it cannot wipe the first message render.
		cancelPendingNewChatInit();
		ensureSelectedModels();
		const requestContext = normalizeRequestContext(
			requestContextOverride,
			captureRequestContext()
		);

		const _selectedModels = selectedModels.map((modelId) =>
			$models.map((m) => m.id).includes(modelId) ? modelId : ''
		);

		if (JSON.stringify(selectedModels) !== JSON.stringify(_selectedModels)) {
			selectedModels = _selectedModels;
		}

		if (userPrompt === '' && files.length === 0) {
			toast.error($i18n.t('Please enter a prompt'));
			return;
		}
		if (selectedModels.includes('')) {
			toast.error($i18n.t('Model not selected'));
			return;
		}

		if (
			files.length > 0 &&
			files.filter((file) => file.type !== 'image' && file.status === 'uploading').length > 0
		) {
			toast.error(
				$i18n.t(`Oops! There are files still uploading. Please wait for the upload to complete.`)
			);
			return;
		}

		if (
			($config?.file?.max_count ?? null) !== null &&
			files.length + chatFiles.length > $config?.file?.max_count
		) {
			toast.error(
				$i18n.t(`You can only chat with a maximum of {{maxCount}} file(s) at a time.`, {
					maxCount: $config?.file?.max_count
				})
			);
			return;
		}

		if (hasBlockingGeneration) {
			if ($settings?.enableMessageQueue ?? true) {
				// Queue the message
				const _files = structuredClone(files);
				messageQueue = [
					...messageQueue,
					{
						id: uuidv4(),
						prompt: userPrompt,
						files: _files,
						requestContext
					}
				];
				// Clear input
				messageInput?.setText('');
				prompt = '';
				files = [];
				return;
			} else {
				// Interrupt: stop current generation and proceed
				await stopResponse();
				await tick();
			}
		}

		if (history?.currentId) {
			const lastMessage = history.messages[history.currentId];

			if (lastMessage.error && !lastMessage.content) {
				// Error in response
				toast.error($i18n.t(`Oops! There was an error in the previous response.`));
				return;
			}
		}

		messageInput?.setText('');
		prompt = '';

		const messages = createMessagesList(history, history.currentId);
		const _files = structuredClone(files);

		chatFiles.push(
			..._files.filter(
				(item) =>
					['doc', 'text', 'note', 'chat', 'folder', 'collection'].includes(item.type) ||
					(item.type === 'file' && !(item?.content_type ?? '').startsWith('image/'))
			)
		);
		chatFiles = chatFiles.filter(
			// Remove duplicates
			(item, index, array) =>
				array.findIndex((i) => JSON.stringify(i) === JSON.stringify(item)) === index
		);

		files = [];
		messageInput?.setText('');

		// Create user message
		let userMessageId = uuidv4();
		let userMessage = {
			id: userMessageId,
			parentId: messages.length !== 0 ? messages.at(-1).id : null,
			childrenIds: [],
			role: 'user',
			content: userPrompt,
			files: _files.length > 0 ? _files : undefined,
			timestamp: Math.floor(Date.now() / 1000), // Unix epoch
			models: selectedModels
		};

		// Add message to history and Set currentId to messageId
		history.messages[userMessageId] = userMessage;
		history.currentId = userMessageId;

		// Append messageId to childrenIds of parent message
		if (messages.length !== 0) {
			history.messages[messages.at(-1).id].childrenIds.push(userMessageId);
		}

		history = {
			...history,
			messages: { ...history.messages }
		};

		// focus on chat input
		const chatInput = document.getElementById('chat-input');
		chatInput?.focus();

		saveSessionSelectedModels();

		await sendMessage(history, userMessageId, { newChat: true, requestContext });
	};

	const sendMessage = async (
		_history,
		parentId: string,
		{
			messages = null,
			modelId = null,
			modelIdx = null,
			newChat = false,
			requestContext = null
		}: {
			messages?: any[] | null;
			modelId?: string | null;
			modelIdx?: number | null;
			newChat?: boolean;
			requestContext?: ChatRequestContext | null;
		} = {}
	) => {
		if (!newChat) {
			await ensureHistoryLoaded();
			_history = structuredClone(history);
		}

		if (autoScroll) {
			scrollToBottom();
		}

		let _chatId = JSON.parse(JSON.stringify($chatId));
		let navigateToCreatedChat = false;
		const isInitialNewChatMessage =
			newChat && _history?.currentId ? _history.messages[_history.currentId]?.parentId === null : false;
		_history = structuredClone(_history);

		const responseMessageIds: Record<PropertyKey, string> = {};
		// If modelId is provided, use it, else use selected model
		let selectedModelIds = modelId
			? [modelId]
			: atSelectedModel !== undefined
				? [atSelectedModel.id]
				: selectedModels;

		const effectiveRequestContext = normalizeRequestContext(
			requestContext,
			captureRequestContext()
		);

		selectedModelIds = selectedModelIds.map((selectedModelId) =>
			getEffectiveModelId(selectedModelId, effectiveRequestContext.thinkingModeEnabled)
		);

		// Create response messages for each selected model
		const forkConversationBranch = selectedModelIds.length > 1;
		for (const [_modelIdx, modelId] of selectedModelIds.entries()) {
			const model = getModelById(modelId);
			if (model) {
				let responseMessageId = uuidv4();
				const responseConversationId = resolveResponseConversationId({
					historyData: history,
					chatId: _chatId,
					userMessageId: parentId,
					responseMessageId,
					forkBranch: forkConversationBranch
				});
				let responseMessage = {
					parentId: parentId,
					id: responseMessageId,
					childrenIds: [],
					role: 'assistant',
					content: '',
					...(responseConversationId ? { conversationId: responseConversationId } : {}),
					model: model?.id ?? modelId,
					modelName: model?.name ?? modelId,
					modelIdx: modelIdx ? modelIdx : _modelIdx,
					timestamp: Math.floor(Date.now() / 1000) // Unix epoch
				};

				// Add message to history and Set currentId to messageId
				history.messages[responseMessageId] = responseMessage;
				history.currentId = responseMessageId;

				// Append messageId to childrenIds of parent message
				if (parentId !== null && history.messages[parentId]) {
					// Add null check before accessing childrenIds
					history.messages[parentId].childrenIds = [
						...history.messages[parentId].childrenIds,
						responseMessageId
					];
				}

				responseMessageIds[`${modelId}-${modelIdx ? modelIdx : _modelIdx}`] = responseMessageId;
				responseRequestContexts.set(responseMessageId, effectiveRequestContext);
			}
		}

		history = {
			...history,
			messages: { ...history.messages }
		};

		// Create new chat if newChat is true and first user message
		if (isInitialNewChatMessage) {
			_chatId = await initChatHandler(_history);
			navigateToCreatedChat =
				!chatIdProp && $page.url.pathname === '/' && !_chatId.startsWith('local:');
		}

		await tick();

		_history = structuredClone(history);
		// Persist the latest branch before sending, but do not block request dispatch
		// on a large history save when the user is waiting for the next turn to start.
		const preSendSavePromise = saveChatHandler(_chatId, _history, {
			ensureLoaded: false,
			refreshList: false,
			allowInactiveTarget: true
		}).catch((error) => {
			console.error(error);
		});

		if (navigateToCreatedChat) {
			await goto(`/c/${_chatId}`, { replaceState: true, noScroll: true, keepFocus: true });
		}

		await Promise.all(
			selectedModelIds.map(async (modelId, _modelIdx) => {
				console.log('modelId', modelId);
				const model = getModelById(modelId);

				if (model) {
					// If there are image files, check if model is vision capable
					// Skip this check if image generation is enabled, as images may be for editing or are generated outputs in the history
					const hasImages = createMessagesList(_history, parentId).some((message) =>
						message.files?.some(
							(file) => file.type === 'image' || (file?.content_type ?? '').startsWith('image/')
						)
					);

						if (
							hasImages &&
							!(model.info?.meta?.capabilities?.vision ?? true) &&
							!effectiveRequestContext.featureToggles.imageGenerationEnabled
						) {
							toast.error(
								$i18n.t('Model {{modelName}} is not vision capable', {
									modelName: model.name ?? model.id
								})
							);
						}

					let responseMessageId =
						responseMessageIds[`${modelId}-${modelIdx ? modelIdx : _modelIdx}`];
					const chatEventEmitter = await getChatEventEmitter(model.id, _chatId);

					scrollToBottom();
					await sendMessageSocket(
						model,
						messages && messages.length > 0
							? messages
							: createMessagesList(_history, responseMessageId),
						_history,
						responseMessageId,
						_chatId,
						effectiveRequestContext
					);

					if (chatEventEmitter) clearInterval(chatEventEmitter);
				} else {
					toast.error($i18n.t(`Model {{modelId}} not found`, { modelId }));
				}
			})
		);

		await preSendSavePromise;

		currentChatPage.set(1);
		chats.set(await getChatList(localStorage.token, $currentChatPage));
	};

	const captureRequestContext = (): ChatRequestContext =>
		normalizeRequestContext({
			thinkingModeEnabled,
			selectedToolIds: [...selectedToolIds],
			selectedFilterIds: [...selectedFilterIds],
			sessionSkillIds: [...sessionSkillIds],
			selectedTerminalId: $selectedTerminalId ?? null,
			finalAnswerOnly: false,
			featureToggles: {
				imageGenerationEnabled,
				webSearchEnabled,
				codeInterpreterEnabled
			}
		});

	const normalizeSubmitDetail = (
		detail: unknown
	): { prompt: string; requestContext: ChatRequestContext | null } => {
		if (typeof detail === 'string') {
			return { prompt: detail, requestContext: null };
		}

		if (!detail || typeof detail !== 'object') {
			return { prompt: '', requestContext: null };
		}

		const candidate = detail as {
			prompt?: unknown;
			requestContext?: Partial<ChatRequestContext> | null;
		};

		return {
			prompt: typeof candidate.prompt === 'string' ? candidate.prompt : '',
			requestContext: candidate.requestContext
				? normalizeRequestContext(candidate.requestContext, captureRequestContext())
				: null
		};
	};

	const getFeatures = (requestContext: ChatRequestContext | null = null) => {
		if (requestContext?.finalAnswerOnly) {
			return {};
		}

		const featureToggles = requestContext?.featureToggles ?? {
			imageGenerationEnabled,
			webSearchEnabled,
			codeInterpreterEnabled
		};
		let features = {};

		if ($config?.features)
			features = {
				voice: $showCallOverlay,
				image_generation:
					$config?.features?.enable_image_generation &&
					($user?.permissions?.features?.image_generation ?? true)
						? featureToggles.imageGenerationEnabled
						: false,
				code_interpreter:
					$config?.features?.enable_code_interpreter &&
					($user?.role === 'admin' || $user?.permissions?.features?.code_interpreter)
						? featureToggles.codeInterpreterEnabled
						: false,
				web_search:
					$config?.features?.enable_web_search && ($user?.permissions?.features?.web_search ?? true)
						? featureToggles.webSearchEnabled
						: false
			};

		const currentModels = atSelectedModel?.id ? [atSelectedModel.id] : selectedModels;
		if (
			currentModels.filter(
				(model) => $models.find((m) => m.id === model)?.info?.meta?.capabilities?.web_search ?? true
			).length === currentModels.length
		) {
			if ($config?.features?.enable_web_search && ($settings?.webSearch ?? false) === 'always') {
				features = { ...features, web_search: true };
			}
		}

		if ($settings?.memory ?? false) {
			features = { ...features, memory: true };
		}

		return features;
	};

	const getStopTokens = () => {
		const stop = params?.stop ?? $settings?.params?.stop;
		if (!stop) return undefined;

		const tokens = Array.isArray(stop) ? stop : stop.split(',').map((s) => s.trim());

		return tokens
			.filter(Boolean)
			.map((token) => decodeURIComponent(JSON.parse(`"${token.replace(/"/g, '\\"')}"`)));
	};

	const sendMessageSocket = async (
		model,
		_messages,
		_history,
		responseMessageId,
		_chatId,
		requestContext: ChatRequestContext
	) => {
		const responseMessage = _history.messages[responseMessageId];
		const userMessage = _history.messages[responseMessage.parentId];

		const chatMessageFiles = _messages
			.filter((message) => message.files)
			.flatMap((message) => message.files);

		// Filter chatFiles to only include files that are in the chatMessageFiles
		chatFiles = chatFiles.filter((item) => {
			const fileExists = chatMessageFiles.some((messageFile) => messageFile.id === item.id);
			return fileExists;
		});

		let files = structuredClone(chatFiles);
		files.push(
			...(userMessage?.files ?? []).filter(
				(item) =>
					['doc', 'text', 'note', 'chat', 'collection'].includes(item.type) ||
					(item.type === 'file' && !(item?.content_type ?? '').startsWith('image/'))
			)
		);
		// Remove duplicates
		files = files.filter(
			(item, index, array) =>
				array.findIndex((i) => JSON.stringify(i) === JSON.stringify(item)) === index
		);

		scrollToBottom();
		eventTarget.dispatchEvent(
			new CustomEvent('chat:start', {
				detail: {
					id: responseMessageId
				}
			})
		);
		await tick();

		let userLocation;
		if ($settings?.userLocation) {
			userLocation = await getAndUpdateUserLocation(localStorage.token).catch((err) => {
				console.error(err);
				return undefined;
			});
		}

		const stream =
			model?.info?.params?.stream_response ??
			params?.stream_response ??
			$settings?.params?.stream_response ??
			true;

		let messages = [
			params?.system || $settings.system
				? {
						role: 'system',
						content: `${params?.system ?? $settings?.system ?? ''}`
					}
				: undefined,
			..._messages.map((message) => ({
				...message,
				content: processDetails(message.content),
				// Include output for temp chats (backend will use it and strip before LLM)
				...(message.output ? { output: message.output } : {})
			}))
		].filter((message) => message);

		messages = messages
			.map((message, idx, arr) => {
				const imageFiles = dedupeImageFiles(
					(message?.files ?? []).filter(
						(file) => file.type === 'image' || (file?.content_type ?? '').startsWith('image/')
					)
				);

				return {
					role: message.role,
					...(message.role === 'assistant' && message.output
						? { output: message.output }
						: {}),
					...(message.statusHistory ? { statusHistory: message.statusHistory } : {}),
					...(message.status ? { status: message.status } : {}),
					...(message.role === 'user' && imageFiles.length > 0
						? {
								content: [
									{
										type: 'text',
										text: message?.merged?.content ?? message.content
									},
									...imageFiles.map((file) => ({
										type: 'image_url',
										image_url: {
											url: file.url
										}
									}))
								]
							}
						: {
								content: message?.merged?.content ?? message.content
							})
				};
			})
			.filter((message) => {
				if (message?.role === 'user') {
					return true;
				}

				if (typeof message?.content === 'string' && message.content.trim()) {
					return true;
				}

				return Array.isArray(message?.output) && message.output.length > 0;
			});

		const finalAnswerOnly = Boolean(requestContext.finalAnswerOnly);
		const toolIds = [];
		const toolServerIds = [];

		if (!finalAnswerOnly) {
			for (const toolId of dedupeIds(requestContext.selectedToolIds)) {
				if (toolId.startsWith('direct_server:')) {
					let serverId = toolId.replace('direct_server:', '');
					if (!isNaN(parseInt(serverId))) {
						toolServerIds.push(parseInt(serverId));
					} else {
						toolServerIds.push(serverId);
					}
				} else {
					toolIds.push(toolId);
				}
			}
		}

		const effectiveModelId = getEffectiveModelId(model.id, requestContext.thinkingModeEnabled);

		// Parse skill mentions (<$skillId|label>) from user messages
		const skillMentionRegex = /<\$([^|>]+)\|?[^>]*>/g;
		const skillIds = [];
		if (!finalAnswerOnly) {
			for (const message of messages) {
				const content =
					typeof message.content === 'string' ? message.content : (message.content?.[0]?.text ?? '');
				for (const match of content.matchAll(skillMentionRegex)) {
					if (!skillIds.includes(match[1])) {
						skillIds.push(match[1]);
					}
				}
			}
		}

		// Strip skill mentions from message content
		if (skillIds.length > 0) {
			messages = messages.map((message) => {
				if (typeof message.content === 'string') {
					return {
						...message,
						content: message.content.replace(/<\$[^>]+>/g, '').trim()
					};
				} else if (Array.isArray(message.content)) {
					return {
						...message,
						content: message.content.map((part) =>
							part.type === 'text'
								? { ...part, text: part.text.replace(/<\$[^>]+>/g, '').trim() }
								: part
						)
					};
				}
				return message;
			});
		}

		// Use the user-selected terminal from the dropdown
		const activeTerminalId = finalAnswerOnly ? null : (requestContext.selectedTerminalId ?? null);
		const effectiveSkillIds = finalAnswerOnly
			? []
			: dedupeIds([...requestContext.sessionSkillIds, ...skillIds]);
		const clientCapabilities = getClientCapabilities({
			chatId: _chatId,
			temporaryChatEnabled: $temporaryChatEnabled,
			canShareChat: $user?.role === 'admin' || ($user?.permissions?.chat?.share ?? true),
			canDraftTool: $user?.role === 'admin' || Boolean($user?.permissions?.workspace?.tools),
			canDraftSkill: $user?.role === 'admin' || Boolean($user?.permissions?.workspace?.skills)
		});
		const requestConversationId =
			getMessageConversationId(responseMessage) ?? normalizeConversationId(_chatId);

		const res = await generateOpenAIChatCompletion(
			localStorage.token,
			{
				stream: stream,
				model: effectiveModelId,
				thinking_mode_enabled: requestContext.thinkingModeEnabled,
				thinking: {
					type: requestContext.thinkingModeEnabled ? 'enabled' : 'disabled'
				},
				messages: messages,
				params: {
					...$settings?.params,
					...params,
					stop:
						(params?.stop ?? $settings?.params?.stop ?? undefined)
							? (params?.stop.split(',').map((token) => token.trim()) ?? $settings.params.stop).map(
									(str) => decodeURIComponent(JSON.parse('"' + str.replace(/\"/g, '\\"') + '"'))
								)
							: undefined
				},

				files: !finalAnswerOnly && (files?.length ?? 0) > 0 ? files : undefined,

				filter_ids:
					!finalAnswerOnly && requestContext.selectedFilterIds.length > 0
						? requestContext.selectedFilterIds
						: undefined,
				tool_ids: toolIds.length > 0 ? toolIds : undefined,
				skill_ids: effectiveSkillIds.length > 0 ? effectiveSkillIds : undefined,
				terminal_id: activeTerminalId ?? undefined,
				tool_servers: finalAnswerOnly
					? []
					: [
							...($toolServers ?? []).filter(
								(server, idx) => toolServerIds.includes(idx) || toolServerIds.includes(server?.id)
							),
							// Direct terminal servers — always included when enabled (not routed through selectedToolIds)
							...($terminalServers ?? []).filter((t) => !t.id)
						],
				features: getFeatures(requestContext),
				variables: {
					...getPromptVariables(
						$user?.name,
						$settings?.userLocation ? userLocation : undefined,
						$user?.email
					)
				},
				model_item: getModelById(effectiveModelId),
				client_capabilities: clientCapabilities,

				session_id: $socket?.id,
				chat_id: _chatId,
				...(requestConversationId ? { conversation_id: requestConversationId } : {}),

				id: responseMessageId,
				parent_id: userMessage?.id ?? null,
				parent_message: userMessage,

				background_tasks: {
					...(!$temporaryChatEnabled &&
					(messages.length == 1 ||
						(messages.length == 2 &&
							messages.at(0)?.role === 'system' &&
							messages.at(1)?.role === 'user')) &&
					(getEffectiveModelId(
						selectedModels[0],
						requestContext.thinkingModeEnabled
					) === effectiveModelId ||
						atSelectedModel !== undefined)
						? {
								title_generation: $settings?.title?.auto ?? true,
								tags_generation: $settings?.autoTags ?? true
							}
						: {}),
					follow_up_generation: $settings?.autoFollowUps ?? true
				},

				...(stream && (model.info?.meta?.capabilities?.usage ?? false)
					? {
							stream_options: {
								include_usage: true
							}
						}
					: {})
			},
			`${WEBUI_BASE_URL}/api`
		).catch(async (error) => {
			console.log(error);

			let errorMessage = error;
			if (error?.error?.message) {
				errorMessage = error.error.message;
			} else if (error?.message) {
				errorMessage = error.message;
			}

			if (typeof errorMessage === 'object') {
				errorMessage = $i18n.t(`Uh-oh! There was an issue with the response.`);
			}

			toast.error(`${errorMessage}`);
			responseMessage.error = {
				content: error
			};

			responseMessage.done = true;

			history.messages[responseMessageId] = responseMessage;
			history.currentId = responseMessageId;

			return null;
		});

		if (res) {
			if (res.error) {
				await handleOpenAIError(res.error, responseMessage);
			} else {
				const newTaskId =
					typeof res.task_id === 'string' && res.task_id.trim() ? res.task_id.trim() : null;
				if (newTaskId) {
					if (Array.isArray(taskIds)) {
						taskIds = [...taskIds, newTaskId];
					} else {
						taskIds = [newTaskId];
					}
					setActiveChatIndicator(_chatId, true);
				}
			}
		}

		await tick();
		scrollToBottom();
	};

	const handleOpenAIError = async (error, responseMessage) => {
		let errorMessage = '';
		let innerError;

		if (error) {
			innerError = error;
		}

		console.error(innerError);
		if ('detail' in innerError) {
			// FastAPI error
			toast.error(innerError.detail);
			errorMessage = innerError.detail;
		} else if ('error' in innerError) {
			// OpenAI error
			if ('message' in innerError.error) {
				toast.error(innerError.error.message);
				errorMessage = innerError.error.message;
			} else {
				toast.error(innerError.error);
				errorMessage = innerError.error;
			}
		} else if ('message' in innerError) {
			// OpenAI error
			toast.error(innerError.message);
			errorMessage = innerError.message;
		}

		responseMessage.error = {
			content: $i18n.t(`Uh-oh! There was an issue with the response.`) + '\n' + errorMessage
		};
		responseMessage.done = true;

		if (responseMessage.statusHistory) {
			responseMessage.statusHistory = responseMessage.statusHistory.filter(
				(status) => status.action !== 'knowledge_search'
			);
		}

		history.messages[responseMessage.id] = responseMessage;
	};

	const stopResponse = async () => {
		let activeTaskIds = normalizeTaskIds(taskIds);

		// Fallback: recover task IDs from backend if local state missed them.
		if (activeTaskIds.length === 0 && $chatId && !$chatId.startsWith('local:')) {
			const taskRes = await getTaskIdsByChatId(localStorage.token, $chatId).catch((error) => {
				console.error(error);
				return null;
			});
			activeTaskIds = normalizeTaskIds(taskRes?.task_ids);
		}

		if (activeTaskIds.length > 0) {
			await Promise.all(
				activeTaskIds.map((taskId) =>
					stopTask(localStorage.token, taskId).catch((error) => {
						console.error(error);
						return null;
					})
				)
			);
			taskIds = null;
			setActiveChatIndicator($chatId, false);
		}
		pendingChatCompletion = null;

		const responseMessage = history.messages[history.currentId];
		// Force-finish in-progress assistant messages for the current branch in UI.
		if (responseMessage?.parentId && history.messages[responseMessage.parentId]) {
			for (const messageId of history.messages[responseMessage.parentId].childrenIds) {
				history.messages[messageId].done = true;
			}
			history.messages[history.currentId] = responseMessage;
		}

		if (autoScroll) {
			scrollToBottom();
		}

		if (generating) {
			generating = false;
			generationController?.abort();
			generationController = null;
		}
	};

	const answerNowResponse = async () => {
		const responseMessage = history.messages?.[history.currentId];
		if (!responseMessage || responseMessage.role !== 'assistant' || !responseMessage.parentId) {
			await stopResponse();
			return;
		}

		const parentId = responseMessage.parentId;
		const responseId = responseMessage.id;
		const modelId = responseMessage.selectedModelId ?? responseMessage.model ?? null;
		const requestContext = normalizeRequestContext(
			{
				thinkingModeEnabled,
				selectedToolIds: [],
				selectedFilterIds: [],
				sessionSkillIds: [],
				selectedTerminalId: null,
				finalAnswerOnly: true,
				featureToggles: {
					imageGenerationEnabled: false,
					webSearchEnabled: false,
					codeInterpreterEnabled: false
				}
			},
			captureRequestContext()
		);
		const immediateAnswerInstruction =
			'请立即基于目前已经完成的检索、附件读取、工具结果和上下文给出最终回答。不要再调用工具；如果信息不足，请明确说明已有信息和限制。';

		await stopResponse();
		await tick();

		await sendMessage(history, parentId, {
			modelId,
			requestContext,
			messages: [
				...createMessagesList(history, responseId),
				{
					role: 'user',
					content: immediateAnswerInstruction
				}
			]
		});
	};

	const submitMessage = async (parentId, prompt) => {
		let userPrompt = prompt;
		let userMessageId = uuidv4();

		let userMessage = {
			id: userMessageId,
			parentId: parentId,
			childrenIds: [],
			role: 'user',
			content: userPrompt,
			models: selectedModels,
			timestamp: Math.floor(Date.now() / 1000) // Unix epoch
		};

		if (parentId !== null) {
			history.messages[parentId].childrenIds = [
				...history.messages[parentId].childrenIds,
				userMessageId
			];
		}

		history.messages[userMessageId] = userMessage;
		history.currentId = userMessageId;

		await tick();

		if (autoScroll) {
			scrollToBottom();
		}

		await sendMessage(history, userMessageId);
	};

	const regenerateResponse = async (message, suggestionPrompt = null) => {
		console.log('regenerateResponse');

		if (history.currentId) {
			await ensureHistoryLoaded();
			message = history.messages[message.id] ?? message;
			let userMessage = history.messages[message.parentId];

			if (!userMessage) {
				toast.error($i18n.t('Parent message not found'));
				return;
			}

			if (autoScroll) {
				scrollToBottom();
			}

			await sendMessage(history, userMessage.id, {
				...(suggestionPrompt
					? {
							messages: [
								...createMessagesList(history, message.id),
								{
									role: 'user',
									content: suggestionPrompt
								}
							]
						}
					: {}),
				...((userMessage?.models ?? [...selectedModels]).length > 1
					? {
							// If multiple models are selected, use the model from the message
							modelId: message.model,
							modelIdx: message.modelIdx
						}
					: {})
			});
		}
	};

	const continueResponse = async () => {
		const _chatId = JSON.parse(JSON.stringify($chatId));
		await ensureHistoryLoaded();

		if (history.currentId && history.messages[history.currentId].done == true) {
			const responseMessage = history.messages[history.currentId];
			responseMessage.done = false;
			await tick();

			const model = getModelById(responseMessage?.selectedModelId ?? responseMessage.model);

			if (model) {
				const requestContext = captureRequestContext();
				await sendMessageSocket(
					model,
					createMessagesList(history, responseMessage.id),
					history,
					responseMessage.id,
					_chatId,
					requestContext
				);
			}
		}
	};

	const mergeResponses = async (messageId, responses, _chatId) => {
		console.log('mergeResponses', messageId, responses);
		const message = history.messages[messageId];
		const mergedResponse = {
			status: true,
			content: ''
		};
		message.merged = mergedResponse;
		history.messages[messageId] = message;

		try {
			generating = true;
			const [res, controller] = await generateMoACompletion(
				localStorage.token,
				message.model ?? '',
				message.parentId ? history.messages[message.parentId].content : '',
				responses
			);

			if (res && res.ok && res.body && generating) {
				generationController = controller as AbortController;
				const textStream = await createOpenAITextStream(
					res.body,
					Boolean($settings?.splitLargeChunks ?? false)
				);
				for await (const update of textStream) {
					const { value, done, sources, error, usage } = update;
					if (error || done) {
						generating = false;
						generationController = null;
						break;
					}

					if (mergedResponse.content == '' && value == '\n') {
						continue;
					} else {
						mergedResponse.content += value;
						history.messages[messageId] = message;
					}

					if (autoScroll) {
						scheduleScrollToBottom();
					}
				}

				await saveChatHandler(_chatId, history);
			} else {
				console.error(res);
			}
		} catch (e) {
			console.error(e);
		}
	};

	const initChatHandler = async (history: any) => {
		let _chatId = $chatId;

		if (!$temporaryChatEnabled) {
			chat = await createNewChat(
				localStorage.token,
				{
					id: _chatId,
					title: $i18n.t('New Chat'),
					models: selectedModels,
					system: $settings.system ?? undefined,
					params: params,
					history: history,
					messages: createMessagesList(history, history.currentId),
					tags: [],
					timestamp: Date.now()
				},
				($selectedFolder as any)?.id ?? null,
				getChatSessionMeta()
			);

			_chatId = chat.id;
			await chatId.set(_chatId);

			await tick();
			void refreshChatListPageOne();
		} else {
			_chatId = `local:${$socket?.id}`; // Use socket id for temporary chat
			await chatId.set(_chatId);
		}
		await tick();

		return _chatId;
	};

	const refreshChatListPageOne = async () => {
		currentChatPage.set(1);
		const nextChats = await getChatList(localStorage.token, 1).catch((error) => {
			console.error(error);
			return null;
		});

		if (nextChats) {
			await chats.set(nextChats);
		}
	};

	const saveChatHandler = async (
		_chatId: string,
		historyData: any,
		{
			ensureLoaded = true,
			refreshList = true,
			allowInactiveTarget = false
		}: {
			ensureLoaded?: boolean;
			refreshList?: boolean;
			allowInactiveTarget?: boolean;
		} = {}
	) => {
		if (!_chatId || _chatId.startsWith('local:') || $temporaryChatEnabled) {
			return;
		}

		const bindVisibleChat = isVisibleChatTarget(_chatId);
		if (!bindVisibleChat && !allowInactiveTarget) {
			return;
		}

		if (ensureLoaded && $chatId === _chatId) {
			await ensureHistoryLoaded();
		}

		const historyToPersist = sanitizeHistoryForPersistence(
			structuredClone(historyData ?? history)
		);
		const updatedChat = await updateChatById(localStorage.token, _chatId, {
			models: selectedModels,
			history: historyToPersist,
			messages: createMessagesList(historyToPersist, historyToPersist.currentId),
			params: params,
			files: chatFiles
		});

		if (bindVisibleChat && updatedChat) {
			chat = updatedChat;
		}

		if (refreshList) {
			await refreshChatListPageOne();
		} else {
			void refreshChatListPageOne();
		}
	};

	const MAX_DRAFT_LENGTH = 5000;
	let saveDraftTimeout: ReturnType<typeof setTimeout> | null = null;

	const saveDraft = async (draft, chatId = null) => {
		if (saveDraftTimeout) {
			clearTimeout(saveDraftTimeout);
		}

		if (draft.prompt !== null && draft.prompt.length < MAX_DRAFT_LENGTH) {
			saveDraftTimeout = setTimeout(async () => {
				await sessionStorage.setItem(
					`chat-input${chatId ? `-${chatId}` : ''}`,
					JSON.stringify(draft)
				);
			}, 500);
		} else {
			sessionStorage.removeItem(`chat-input${chatId ? `-${chatId}` : ''}`);
		}
	};

	const getEffectiveModelId = (
		modelId: string,
		thinkingModeEnabledOverride: boolean = thinkingModeEnabled
	): string => {
		const normalizedModelId = normalizeModelId(modelId);

		if (!thinkingModeEnabledOverride || !normalizedModelId) {
			return normalizedModelId;
		}

		if (normalizedModelId.endsWith('-thinking')) {
			return normalizedModelId;
		}

		const thinkingCandidate = `${normalizedModelId}-thinking`;
		return $models.some((model) => model.id === thinkingCandidate)
			? thinkingCandidate
			: normalizedModelId;
	};

	const getModelById = (modelId: string): Model | undefined => {
		const directModel = $models.find((m) => m.id === modelId);
		if (directModel) return directModel;

		if (modelId.endsWith('-thinking')) {
			const baseModelId = modelId.slice(0, -'-thinking'.length);
			return $models.find((m) => m.id === baseModelId);
		}

		return undefined;
	};

	const clearDraft = async (chatId = null) => {
		if (saveDraftTimeout) {
			clearTimeout(saveDraftTimeout);
		}
		await sessionStorage.removeItem(`chat-input${chatId ? `-${chatId}` : ''}`);
	};

	const moveChatHandler = async (chatId, folderId) => {
		if (chatId && folderId) {
			const res = await updateChatFolderIdById(localStorage.token, chatId, folderId).catch(
				(error) => {
					toast.error(`${error}`);
					return null;
				}
			);

			if (res) {
				currentChatPage.set(1);
				await chats.set(await getChatList(localStorage.token, $currentChatPage));
				await pinnedChats.set(await getPinnedChatList(localStorage.token));

				toast.success($i18n.t('Chat moved successfully'));
			}
		} else {
			toast.error($i18n.t('Failed to move chat'));
		}
	};

	const archiveChatHandler = async (id: string) => {
		try {
			await archiveChatById(localStorage.token, id);
			currentChatPage.set(1);
			initNewChat();
			await goto('/');
			getChatList(localStorage.token, $currentChatPage).then((chats) => {
				chats.set(chats);
			});
			getPinnedChatList(localStorage.token).then((pinnedChats) => {
				pinnedChats.set(pinnedChats);
			});
			toast.success($i18n.t('Chat archived.'));
		} catch (error) {
			console.error('Error archiving chat:', error);
			toast.error($i18n.t('Failed to archive chat.'));
		}
	};
</script>

<svelte:head>
	<title>
		{$settings.showChatTitleInTab !== false && $chatTitle
			? `${$chatTitle.length > 30 ? `${$chatTitle.slice(0, 30)}...` : $chatTitle} • ${$WEBUI_NAME}`
			: `${$WEBUI_NAME}`}
	</title>
</svelte:head>

<audio id="audioElement" src="" style="display: none;"></audio>

<EventConfirmDialog
	bind:show={showEventConfirmation}
	title={eventConfirmationTitle}
	message={eventConfirmationMessage}
	input={eventConfirmationInput}
	inputPlaceholder={eventConfirmationInputPlaceholder}
	inputValue={eventConfirmationInputValue}
	inputType={eventConfirmationInputType}
	on:confirm={(e) => {
		if (e.detail) {
			eventCallback(e.detail);
		} else {
			eventCallback(true);
		}
	}}
	on:cancel={() => {
		eventCallback(false);
	}}
/>

<CapabilityInstallDialog
	bind:show={showCapabilityInstallDialog}
	title={capabilityInstallRequest?.title ?? ''}
	message={capabilityInstallRequest?.message ?? ''}
	resourceType={capabilityInstallRequest?.resource_type ?? 'tool'}
	resourceName={capabilityInstallRequest?.name ?? ''}
	installLabel={capabilityInstallRequest?.install_label ?? ''}
	sessionLabel={capabilityInstallRequest?.session_label ?? ''}
	cancelLabel={capabilityInstallRequest?.cancel_label ?? ''}
	on:decision={(e) => {
		handleCapabilityInstallDecision(e.detail.decision);
	}}
/>

<div
	class="h-screen max-h-[100dvh] transition-width duration-200 ease-in-out {$showSidebar
		? '  md:max-w-[calc(100%-var(--sidebar-width))]'
		: ' '} w-full max-w-full flex flex-col"
	id="chat-container"
	class:landing-ambient-mode={showLandingAmbientBackground}
>
	{#if !loading}
		<div in:fade={{ duration: 50 }} class="w-full h-full flex flex-col">
			{#if showLandingAmbientBackground}
				<LandingAmbientBackground />
			{/if}

			{#if $selectedFolder && $selectedFolder?.meta?.background_image_url}
				<div
					class="absolute top-0 left-0 w-full h-full bg-cover bg-center bg-no-repeat"
					style="background-image: url({$selectedFolder?.meta?.background_image_url})  "
				/>

				<div
					class="absolute top-0 left-0 w-full h-full bg-linear-to-t from-white to-white/85 dark:from-gray-900 dark:to-gray-900/90 z-0"
				/>
			{:else if $settings?.backgroundImageUrl ?? $config?.license_metadata?.background_image_url ?? null}
				<div
					class="absolute top-0 left-0 w-full h-full bg-cover bg-center bg-no-repeat"
					style="background-image: url({$settings?.backgroundImageUrl ??
						$config?.license_metadata?.background_image_url})  "
				/>

				<div
					class="absolute top-0 left-0 w-full h-full bg-linear-to-t from-white to-white/85 dark:from-gray-900 dark:to-gray-900/90 z-0"
				/>
			{/if}

			<PaneGroup direction="horizontal" class="w-full h-full">
				<Pane defaultSize={50} minSize={30} class="h-full flex relative max-w-full flex-col">
					<FilesOverlay show={dragged} />
					<Navbar
						bind:this={navbarElement}
						showModelSelector={false}
						chat={{
							id: $chatId,
							chat: {
								title: $chatTitle,
								models: selectedModels,
								system: $settings.system ?? undefined,
								params: params,
								history: history,
								timestamp: Date.now()
							}
						}}
						{history}
						title={$chatTitle}
						bind:selectedModels
						shareEnabled={!!history.currentId}
						{initNewChat}
						{archiveChatHandler}
						{moveChatHandler}
						onSaveTempChat={async () => {
							try {
								if (!history?.currentId || !Object.keys(history.messages).length) {
									toast.error($i18n.t('No conversation to save'));
									return;
								}
								const messages = createMessagesList(history, history.currentId);
								const title =
									messages.find((m) => m.role === 'user')?.content ?? $i18n.t('New Chat');

								const savedChat = await createNewChat(
									localStorage.token,
									{
										id: uuidv4(),
										title: title.length > 50 ? `${title.slice(0, 50)}...` : title,
										models: selectedModels,
										params: params,
										history: history,
										messages: messages,
										timestamp: Date.now()
									},
									null,
									getChatSessionMeta()
								);

								if (savedChat) {
									temporaryChatEnabled.set(false);
									chatId.set(savedChat.id);
									chats.set(await getChatList(localStorage.token, $currentChatPage));

									await goto(`/c/${savedChat.id}`);
									toast.success($i18n.t('Conversation saved successfully'));
								}
							} catch (error) {
								console.error('Error saving conversation:', error);
								toast.error($i18n.t('Failed to save conversation'));
							}
						}}
					/>

					<div id="chat-pane" class="flex flex-col flex-auto z-10 w-full @container overflow-auto">
						{#if ($settings?.landingPageMode === 'chat' && !$selectedFolder) || messageCount > 0}
							<div
								class=" pb-2.5 flex flex-col justify-between w-full flex-auto overflow-auto h-0 max-w-full z-10 scrollbar-hidden"
								id="messages-container"
								bind:this={messagesContainerElement}
								on:scroll={(e) => {
									autoScroll =
										messagesContainerElement.scrollHeight - messagesContainerElement.scrollTop <=
										messagesContainerElement.clientHeight + 5;
								}}
							>
								<div class=" h-full w-full flex flex-col">
									<Messages
										chatId={$chatId}
										bind:history
										bind:autoScroll
										bind:prompt
										{historyMeta}
										loadMoreHistory={loadMoreHistory}
										ensureHistoryLoaded={ensureHistoryLoaded}
										setInputText={(text) => {
											messageInput?.setText(text);
										}}
										{selectedModels}
										{atSelectedModel}
										{sendMessage}
										{showMessage}
										{submitMessage}
										{continueResponse}
										{regenerateResponse}
										{mergeResponses}
										{chatActionHandler}
										{addMessages}
										topPadding={true}
										bottomPadding={files.length > 0}
										{onSelect}
									/>
								</div>
							</div>

							<div class=" pb-2 z-10">
								<MessageInput
									bind:this={messageInput}
									{history}
									{hasBlockingGeneration}
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
									toolServers={$toolServers}
									{generating}
									{stopResponse}
									{answerNowResponse}
									{createMessagePair}
									{onUpload}
									{messageQueue}
									onQueueSendNow={async (id) => {
										const item = messageQueue.find((m) => m.id === id);
										if (item) {
											// Remove from queue
											messageQueue = messageQueue.filter((m) => m.id !== id);
											// Stop current generation first
											await stopResponse();
											await tick();
											// Set files and submit
											restoreQueuedComposerState(item.requestContext);
											files = sanitizeFilesList(structuredClone(item.files ?? []));
											await tick();
											await submitPrompt(item.prompt, {
												requestContext: item.requestContext
											});
										}
									}}
									onQueueEdit={(id) => {
										const item = messageQueue.find((m) => m.id === id);
										if (item) {
											// Remove from queue
											messageQueue = messageQueue.filter((m) => m.id !== id);
											// Set files and restore prompt to input
											files = sanitizeFilesList(structuredClone(item.files ?? []));
											restoreQueuedComposerState(item.requestContext);
											messageInput?.setText(item.prompt);
										}
									}}
									onQueueDelete={(id) => {
										messageQueue = messageQueue.filter((m) => m.id !== id);
									}}
									onChange={(data) => {
										if (!$temporaryChatEnabled) {
											saveDraft(data, $chatId);
										}
									}}
									on:submit={async (e) => {
										const { prompt: submittedPrompt, requestContext } =
											normalizeSubmitDetail(e.detail);
										clearDraft();
										if (submittedPrompt || files.length > 0) {
											cancelPendingNewChatInit();
											await tick();
											submitPrompt(submittedPrompt.replaceAll('\n\n', '\n'), {
												requestContext
											});
										}
									}}
								/>

								<div
									class="absolute bottom-1 text-xs text-gray-500 text-center line-clamp-1 right-0 left-0"
								>
									<!-- {$i18n.t('LLMs can make mistakes. Verify important information.')} -->
								</div>
							</div>
						{:else}
							<div class="flex items-center h-full">
								<Placeholder
									{history}
									{selectedModels}
									{thinkingModelId}
									bind:messageInput
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
									toolServers={$toolServers}
									{stopResponse}
									{answerNowResponse}
									{createMessagePair}
									{onSelect}
									{onUpload}
									onChange={(data) => {
										if (!$temporaryChatEnabled) {
											saveDraft(data);
										}
									}}
									on:submit={async (e) => {
										const { prompt: submittedPrompt, requestContext } =
											normalizeSubmitDetail(e.detail);
										clearDraft();
										if (submittedPrompt || files.length > 0) {
											cancelPendingNewChatInit();
											await tick();
											submitPrompt(submittedPrompt.replaceAll('\n\n', '\n'), {
												requestContext
											});
										}
									}}
								/>
							</div>
						{/if}
					</div>
				</Pane>

				<ChatControls
					bind:this={controlPaneComponent}
					bind:history
					bind:chatFiles
					bind:params
					bind:files
					bind:pane={controlPane}
					chatId={$chatId}
					modelId={selectedModelIds?.at(0) ?? null}
					models={selectedModelIds.reduce((a, e, i, arr) => {
						const model = $models.find((m) => m.id === e);
						if (model) {
							return [...a, model];
						}
						return a;
					}, [])}
					{submitPrompt}
					{stopResponse}
					{showMessage}
					{eventTarget}
					{codeInterpreterEnabled}
				/>
			</PaneGroup>
		</div>
	{:else if loading}
		<div class=" flex items-center justify-center h-full w-full">
			<div class="m-auto">
				<Spinner className="size-5" />
			</div>
		</div>
	{/if}
</div>

<style>
	/* On the landing page, keep the input bar "floating" and stable:
	   don't let moving background blobs refract through backdrop-filter. */
	:global(#chat-container.landing-ambient-mode #message-input-container) {
		backdrop-filter: none !important;
		-webkit-backdrop-filter: none !important;
		background: linear-gradient(
				160deg,
				rgba(255, 255, 255, 0.92) 0%,
				rgba(255, 255, 255, 0.8) 48%,
				rgba(255, 255, 255, 0.86) 100%
			),
			radial-gradient(circle at 12% -12%, rgba(90, 152, 255, 0.09), rgba(90, 152, 255, 0) 52%),
			radial-gradient(circle at 88% 122%, rgba(239, 91, 109, 0.07), rgba(239, 91, 109, 0) 58%);
		border-color: rgba(255, 255, 255, 0.6) !important;
		box-shadow:
			0 22px 70px -48px rgba(15, 23, 42, 0.55),
			0 12px 26px -30px rgba(15, 23, 42, 0.45),
			inset 0 1px 0 rgba(255, 255, 255, 0.75);
	}

	:global(.dark #chat-container.landing-ambient-mode #message-input-container) {
		background: linear-gradient(
				165deg,
				rgba(14, 18, 28, 0.9) 0%,
				rgba(10, 13, 20, 0.78) 52%,
				rgba(14, 18, 28, 0.88) 100%
			),
			radial-gradient(circle at 12% -12%, rgba(90, 152, 255, 0.12), rgba(90, 152, 255, 0) 56%),
			radial-gradient(circle at 88% 122%, rgba(239, 91, 109, 0.1), rgba(239, 91, 109, 0) 62%);
		border-color: rgba(255, 255, 255, 0.14) !important;
		box-shadow:
			0 26px 78px -52px rgba(0, 0, 0, 0.8),
			0 12px 26px -34px rgba(0, 0, 0, 0.7),
			inset 0 1px 0 rgba(255, 255, 255, 0.12);
	}
</style>
