<script lang="ts">
	import { toast } from 'svelte-sonner';
	import dayjs from 'dayjs';
	import { goto } from '$app/navigation';
	import { createEventDispatcher, onDestroy } from 'svelte';
	import { onMount, tick, getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType, t } from 'i18next';

	const i18n = getContext<Writable<i18nType>>('i18n');

	const dispatch = createEventDispatcher();

	import { createNewFeedback, getFeedbackById, updateFeedbackById } from '$lib/apis/evaluations';
	import { getChatById } from '$lib/apis/chats';
	import { generateTags } from '$lib/apis';
	import { createNewSkill } from '$lib/apis/skills';
	import { createNewTool } from '$lib/apis/tools';
	import { downloadFileBlob } from '$lib/apis/terminal';

	import {
		audioQueue,
		config,
		models,
		selectedGeneratedFilePreviewId,
		settings,
		showArtifacts,
		showCallOverlay,
		showControls,
		showEmbeds,
		showFilePreview,
		showOverview,
		selectedTerminalId,
		terminalServers,
		temporaryChatEnabled,
		TTSWorker,
		user
	} from '$lib/stores';
	import { synthesizeOpenAISpeech } from '$lib/apis/audio';
	import { imageGenerations } from '$lib/apis/images';
	import {
		copyToClipboard as _copyToClipboard,
		approximateToHumanReadable,
		getMessageContentParts,
		normalizeLeakedFormatting,
		sanitizeResponseContent,
		createMessagesList,
		formatDate,
		removeDetails,
		removeAllDetails
	} from '$lib/utils';
	import { WEBUI_API_BASE_URL, WEBUI_VERSION } from '$lib/constants';
	import {
		type GeneratedFileItem,
		collectGeneratedFilesFromMessage,
		getToolCallArtifactEvidence,
		getToolCallAttrs,
		inferFileName,
		isFileGeneratingToolId,
		isDownloadRef,
		parseNestedJSON,
		isPrimaryDocumentArtifact,
		normalizeVisualUrlForMatching,
		triggerGeneratedFileDownload,
		resolveToolCallStatus,
		shouldHideHelperArtifact
	} from '$lib/utils/generated-files';
	import { openGeneratedFilePreview } from '$lib/utils/generated-file-preview';
	import {
		type ParsedToolDraft,
		getToolVersionRequirement,
		parseToolDraftFromMessageContent
	} from '$lib/utils/tool-drafts';
	import {
		type ParsedSkillDraft,
		parseSkillDraftFromMessageContent
	} from '$lib/utils/skill-drafts';

	import Name from './Name.svelte';
	import ProfileImage from './ProfileImage.svelte';
	import Skeleton from './Skeleton.svelte';
	import FileItem from '$lib/components/common/FileItem.svelte';
	import Image from '$lib/components/common/Image.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import RateComment from './RateComment.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import WebSearchResults from './ResponseMessage/WebSearchResults.svelte';
	import Sparkles from '$lib/components/icons/Sparkles.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import ChevronRight from '$lib/components/icons/ChevronRight.svelte';
	import ChevronUp from '$lib/components/icons/ChevronUp.svelte';
	import Document from '$lib/components/icons/Document.svelte';
	import WrenchSolid from '$lib/components/icons/WrenchSolid.svelte';

	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import DeleteConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';

	import Error from './Error.svelte';
	import Citations from './Citations.svelte';
	import SourceContextNotice from './SourceContextNotice.svelte';
	import CodeExecutions from './CodeExecutions.svelte';
	import ContentRenderer from './ContentRenderer.svelte';
	import { KokoroWorker } from '$lib/workers/KokoroWorker';
	import FollowUps from './ResponseMessage/FollowUps.svelte';
	import StatusHistory from './ResponseMessage/StatusHistory.svelte';
	import { fade } from 'svelte/transition';
	import RegenerateMenu from './ResponseMessage/RegenerateMenu.svelte';
	import FullHeightIframe from '$lib/components/common/FullHeightIframe.svelte';
	import AccessControl from '$lib/components/workspace/common/AccessControl.svelte';
	import ToolCallDisplay from '$lib/components/common/ToolCallDisplay.svelte';
	import { isHiddenHelperToolCall, normalizeToolId } from '$lib/utils/tool-display';

	type MessageStatus = {
		done?: boolean;
		action?: string;
		description?: string;
		status?: string;
		hidden?: boolean;
		urls?: string[];
		items?: unknown[];
		query?: string;
		queries?: string[];
		count?: number;
	};

	interface MessageType {
		id: string;
		model: string;
		content: string;
		files?: { type: string; url: string }[];
		timestamp: number;
		role: string;
		statusHistory?: MessageStatus[];
		status?: MessageStatus;
		done: boolean;
		error?: boolean | { content: string };
		sources?: any[];
		citations?: any[];
		metadata?: Record<string, any>;
		code_executions?: {
			uuid: string;
			name: string;
			code: string;
			language?: string;
			result?: {
				error?: string;
				output?: string;
				files?: { name: string; url: string }[];
			};
		}[];
		info?: {
			openai?: boolean;
			prompt_tokens?: number;
			completion_tokens?: number;
			total_tokens?: number;
			eval_count?: number;
			eval_duration?: number;
			prompt_eval_count?: number;
			prompt_eval_duration?: number;
			total_duration?: number;
			load_duration?: number;
			usage?: unknown;
		};
		annotation?: { type: string; rating: number };
	}

	export let chatId = '';
	export let history;
	export let messageId;
	export let selectedModels = [];

	const getErrorKey = (value: unknown): string => {
		if (!value) return '';
		if (typeof value === 'string') return value;
		if (typeof value === 'object') {
			const content = (value as any).content;
			if (typeof content === 'string') return content;
			const message = (value as any).message;
			if (typeof message === 'string') return message;
		}
		return 'error';
	};

	const safeStringify = (value: unknown): string => {
		if (value === null || value === undefined) return '';
		if (typeof value === 'string') return value;
		try {
			return JSON.stringify(value);
		} catch {
			return String(value);
		}
	};

	const hashString = (value: string): string => {
		let hash = 2166136261;
		for (let index = 0; index < value.length; index += 1) {
			hash ^= value.charCodeAt(index);
			hash = Math.imul(hash, 16777619);
		}
		return (hash >>> 0).toString(36);
	};

	const OUTPUT_SIGNATURE_MAX_DEPTH = 2;
	const OUTPUT_SIGNATURE_MAX_ARRAY_ITEMS = 4;
	const OUTPUT_SIGNATURE_MAX_OBJECT_KEYS = 8;
	const OUTPUT_SIGNATURE_HEAD_CHARS = 160;
	const OUTPUT_SIGNATURE_TAIL_CHARS = 96;

	const buildStringSignature = (value: string): string => {
		if (!value) return '0:0';
		if (value.length <= OUTPUT_SIGNATURE_HEAD_CHARS + OUTPUT_SIGNATURE_TAIL_CHARS) {
			return `${value.length}:${hashString(value)}`;
		}

		const sample = `${value.slice(0, OUTPUT_SIGNATURE_HEAD_CHARS)}::${value.slice(
			-OUTPUT_SIGNATURE_TAIL_CHARS
		)}`;
		return `${value.length}:${hashString(sample)}`;
	};

	const buildStructuredSignature = (value: unknown, depth = 0): string => {
		if (value === null || value === undefined) return '';
		if (typeof value === 'string') return `s:${buildStringSignature(value)}`;
		if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') {
			return `${typeof value}:${String(value)}`;
		}

		if (Array.isArray(value)) {
			if (depth >= OUTPUT_SIGNATURE_MAX_DEPTH) {
				return `a:${value.length}`;
			}
			return `a:${value.length}[${value
				.slice(0, OUTPUT_SIGNATURE_MAX_ARRAY_ITEMS)
				.map((item) => buildStructuredSignature(item, depth + 1))
				.join('|')}]`;
		}

		if (typeof value === 'object') {
			const record = value as Record<string, unknown>;
			const keys = Object.keys(record).sort();
			if (depth >= OUTPUT_SIGNATURE_MAX_DEPTH) {
				return `o:${keys.length}:${keys
					.slice(0, OUTPUT_SIGNATURE_MAX_OBJECT_KEYS)
					.join(',')}`;
			}
			return `o:${keys.length}{${keys
				.slice(0, OUTPUT_SIGNATURE_MAX_OBJECT_KEYS)
				.map((key) => `${key}=${buildStructuredSignature(record[key], depth + 1)}`)
				.join('|')}}`;
		}

		return `${typeof value}:${String(value)}`;
	};

	const buildOutputSignature = (value: unknown): string => {
		if (!Array.isArray(value)) return '';
		return value
			.map((item) => {
				if (!item || typeof item !== 'object') return String(item ?? '');
				const record = item as Record<string, unknown>;
				return [
					record.type ?? '',
					record.id ?? '',
					record.call_id ?? '',
					record.status ?? '',
					record.name ?? '',
					record.tool_name ?? '',
					buildStructuredSignature(record.arguments),
					buildStructuredSignature(record.output),
					buildStructuredSignature(record.result),
					buildStructuredSignature(record.files),
					buildStructuredSignature(record.embeds),
					buildStructuredSignature(record.content),
					buildStructuredSignature(record.summary),
					buildStructuredSignature(record.error),
					buildStructuredSignature(record.metadata)
				].join(':');
			})
			.join('|');
	};

	const buildMessageFileSignature = (value: unknown): string => {
		if (!Array.isArray(value)) return '';
		return value
			.map((item) => {
				if (!item || typeof item !== 'object') return String(item ?? '');
				const record = item as Record<string, unknown>;
				return buildStructuredSignature({
					id: record.id ?? '',
					type: record.type ?? '',
					name: record.name ?? record.filename ?? record.fileName ?? '',
					url: record.url ?? '',
					download_url: record.download_url ?? '',
					bridge_url: record.bridge_url ?? '',
					generated_file_url: record.generated_file_url ?? '',
					path: record.path ?? ''
				});
			})
			.join('|');
	};

	const buildMessageEmbedSignature = (value: unknown): string => {
		if (!Array.isArray(value)) return '';
		return value
			.map((item) =>
				typeof item === 'string' ? buildStringSignature(item.trim()) : buildStructuredSignature(item)
			)
			.join('|');
	};

	const buildMessageMetaKey = (source: MessageType): string => {
		if (!source) return '';
		const info = source.info ?? {};
		const status = source.status ?? {};
		const annotation = source.annotation ?? {};
		const outputSignature = buildOutputSignature((source as any).output);
		const filesSignature = buildMessageFileSignature((source as any).files);
		const metadataSignature = buildStructuredSignature((source as any).metadata);
		const followUpsLength = Array.isArray((source as any).followUps)
			? (source as any).followUps.length
			: 0;
		const referencesSignature = buildStructuredSignature({
			sources: (source as any).sources,
			citations: (source as any).citations
		});
		const statusHistorySignature = buildStructuredSignature((source as any).statusHistory);
		const codeExecutionsLength = Array.isArray((source as any).code_executions)
			? (source as any).code_executions.length
			: 0;
		const embedsSignature = buildMessageEmbedSignature((source as any).embeds);

		return [
			source.done ? '1' : '0',
			getErrorKey(source.error),
			buildStructuredSignature(status),
			statusHistorySignature,
			outputSignature,
			filesSignature,
			metadataSignature,
			followUpsLength,
			referencesSignature,
			codeExecutionsLength,
			embedsSignature,
			`${annotation?.type ?? ''}:${annotation?.rating ?? ''}`,
			`${info?.prompt_tokens ?? ''}:${info?.completion_tokens ?? ''}:${info?.total_tokens ?? ''}`,
			`${info?.eval_count ?? ''}:${info?.eval_duration ?? ''}:${info?.total_duration ?? ''}:${info?.load_duration ?? ''}`
		].join('|');
	};

	let message: MessageType = structuredClone(history.messages[messageId]);
	let lastMessageMetaKey = buildMessageMetaKey(message);
	$: if (history.messages) {
		const source = history.messages[messageId];
		if (source) {
			const metaKey = buildMessageMetaKey(source);
			// Fast path: O(1) check on the fields that change most often (content during streaming, done at end)
			if (
				message.content !== source.content ||
				message.done !== source.done ||
				metaKey !== lastMessageMetaKey
			) {
				lastMessageMetaKey = metaKey;
				message = structuredClone(source);
			}
		}
	}

	const isVisibleMessageStatus = (
		status: MessageStatus | null | undefined
	): status is MessageStatus => Boolean(status) && status?.hidden !== true;

	const getNormalizedStatusHistory = (
		source: MessageType | null | undefined
	): MessageStatus[] => {
		const historyItems = Array.isArray(source?.statusHistory)
			? source.statusHistory.filter(Boolean)
			: [];
		if (historyItems.length > 0) {
			if (!historyItems.some(isVisibleMessageStatus) && isVisibleMessageStatus(source?.status)) {
				return [source.status];
			}
			return historyItems;
		}

		return source?.status ? [source.status] : [];
	};

	const shouldRenderStatusHistory = (history: MessageStatus[]): boolean => {
		const visibleStatuses = history.filter(isVisibleMessageStatus);
		if (visibleStatuses.length === 0) return false;

		const latestStatus = history.at(-1);
		if (latestStatus?.hidden === true && visibleStatuses.every((status) => status.action === 'chat')) {
			return false;
		}

		return true;
	};

	const hasUsableDocument = (source: any): boolean =>
		Array.isArray(source?.document) &&
		source.document.some((item: unknown) => {
			if (typeof item === 'string') return item.trim().length > 0;
			return item !== null && item !== undefined;
		});

	const isDiagnosticOnlyReference = (source: any): boolean => {
		if (!source || typeof source !== 'object') return true;
		const hasDiagnostics =
			Array.isArray(source?.retrieval_diagnostics) ||
			Array.isArray(source?.metadata?.retrieval_diagnostics);
		return !hasUsableDocument(source) && hasDiagnostics;
	};

	const getRetrievalMetadata = (source: MessageType | null | undefined): Record<string, any> | null => {
		const metadata = source?.metadata;
		return metadata && typeof metadata === 'object' ? metadata : null;
	};

	const toReferenceList = (value: unknown): any[] => {
		if (Array.isArray(value)) return value;
		return value && typeof value === 'object' ? [value] : [];
	};

	const getRenderableSources = (source: MessageType | null | undefined): any[] => {
		const metadata = getRetrievalMetadata(source);
		const primarySources = [
			...toReferenceList(source?.sources),
			...toReferenceList(source?.citations)
		].filter((item: any) => item && typeof item === 'object' && item?.type !== 'code_execution');
		const canonicalReferences = Array.isArray(metadata?.canonical_references)
			? metadata.canonical_references
			: [];
		const candidates = primarySources.length > 0 ? primarySources : canonicalReferences;
		return candidates.filter((item) => !isDiagnosticOnlyReference(item));
	};

	export let siblings;

	export let setInputText: Function = () => {};
	export let gotoMessage: Function = () => {};
	export let showPreviousMessage: Function;
	export let showNextMessage: Function;

	export let updateChat: Function;
	export let editMessage: Function;
	export let saveMessage: Function;
	export let rateMessage: Function;
	export let actionMessage: Function;
	export let deleteMessage: Function;

	export let submitMessage: Function;
	export let continueResponse: Function;
	export let regenerateResponse: Function;

	export let addMessages: Function;

	export let isLastMessage = true;
	export let readOnly = false;
	export let editCodeBlock = true;
	export let topPadding = false;

	let citationsElement: any;

	let contentContainerElement: HTMLDivElement;
	let buttonsContainerElement: HTMLDivElement;
	let showDeleteConfirm = false;

	let model: any = null;
	$: {
		const modelId = message?.model;
		model = $models.find((m) => m.id === modelId);
		if (!model && typeof modelId === 'string' && modelId.endsWith('-thinking')) {
			const baseModelId = modelId.slice(0, -'-thinking'.length);
			model = $models.find((m) => m.id === baseModelId);
		}
	}

	let edit = false;
	let editedContent = '';
	let editTextAreaElement: HTMLTextAreaElement;

	let messageIndexEdit = false;

	let speaking = false;
	let speakingIdx: number | undefined;

	let loadingSpeech = false;

	let showRateComment = false;
	let generatedFiles: GeneratedFileItem[] = [];
	let displayGeneratedFiles: GeneratedFileItem[] = [];
	let visibleMessageFiles: any[] = [];
	let visibleMessageEmbeds: string[] = [];
	let statusUpdatesEnabled = true;
	let normalizedStatusHistory: MessageStatus[] = [];
	let hasVisibleStatusHistory = false;
	let retrievalMetadata: Record<string, any> | null = null;
	let renderableSources: any[] = [];
	let generatedFilesKey = '';
	let generatedFilesListKey = '';
	let parsedContentKey = '';
	let parsedGeneratedFilesKey = '';
	let parsedOutputKey = '';
	let parsedFilesKey = '';
	let parsedEmbedsKey = '';
	let parsedProcessStatusTick = -1;
	let parsedSkillDraftKey = '';
	let parsedSkillDraft: ParsedSkillDraft | null = null;
	let editableSkillDraftKey = '';
	let editableSkillDraft: {
		id: string;
		name: string;
		description: string;
		content: string;
		meta: ParsedSkillDraft['meta'];
		access_grants: any[];
	} | null = null;
	let parsedToolDraftKey = '';
	let parsedToolDraft: ParsedToolDraft | null = null;
	let editableToolDraftKey = '';
	let editableToolDraft: {
		id: string;
		name: string;
		content: string;
		meta: ParsedToolDraft['meta'];
		access_grants: any[];
	} | null = null;
	let showCreateSkillConfirm = false;
	let showCreateToolConfirm = false;
	let creatingSkillDraft = false;
	let creatingToolDraft = false;
	let canSharePublicSkill = false;
	let canSharePublicTool = false;

	$: statusUpdatesEnabled = model?.info?.meta?.capabilities?.status_updates ?? true;
	$: normalizedStatusHistory = statusUpdatesEnabled ? getNormalizedStatusHistory(message) : [];
	$: hasVisibleStatusHistory = shouldRenderStatusHistory(normalizedStatusHistory);
	$: retrievalMetadata = getRetrievalMetadata(message);
	$: renderableSources = getRenderableSources(message);

	const cloneSkillDraftForEditing = (draft: ParsedSkillDraft) => ({
		id: draft.id,
		name: draft.name,
		description: draft.description,
		content: draft.content,
		meta: {
			...draft.meta,
			tags: Array.isArray(draft.meta?.tags) ? [...draft.meta.tags] : [],
			dependencies: Array.isArray(draft.meta?.dependencies) ? [...draft.meta.dependencies] : []
		},
		access_grants: Array.isArray(draft.access_grants) ? structuredClone(draft.access_grants) : []
	});

	const cloneToolDraftForEditing = (draft: ParsedToolDraft) => ({
		id: draft.id,
		name: draft.name,
		content: draft.content,
		meta: {
			...draft.meta,
			manifest: { ...(draft.meta?.manifest ?? {}) },
			dependencies: Array.isArray(draft.meta?.dependencies) ? [...draft.meta.dependencies] : []
		},
		access_grants: Array.isArray(draft.access_grants) ? structuredClone(draft.access_grants) : []
	});

	const buildFilesKey = (messageData: MessageType): string => {
		if (!messageData) return '';
		const rawContent = messageData.content ?? '';
		const hasToolCalls = rawContent.includes('type="tool_calls"');
		const contentKey = hasToolCalls ? rawContent : '';
		const outputKey = buildOutputSignature((messageData as any)?.output);
		const files = Array.isArray((messageData as any)?.files) ? (messageData as any).files : [];
		const filesKey = files
			.map((file: any) =>
				[
					file?.id ?? '',
					file?.url ?? '',
					file?.path ?? '',
					file?.name ?? '',
					file?.filename ?? '',
					file?.fileName ?? '',
					file?.size ?? ''
				].join(':')
			)
			.join('|');
		return `${hasToolCalls ? '1' : '0'}::${contentKey}::${outputKey}::${filesKey}`;
	};

	const buildGeneratedFilesListKey = (files: GeneratedFileItem[]): string =>
		files.map((file) => file.id).join('|');

	$: {
		const nextKey = buildFilesKey(message);
		if (nextKey !== generatedFilesKey) {
			generatedFilesKey = nextKey;
			generatedFiles = collectGeneratedFilesFromMessage(message);
			generatedFilesListKey = buildGeneratedFilesListKey(generatedFiles);
		}
	}

	const shouldInlineAssistantImageArtifact = (file: GeneratedFileItem): boolean => {
		return file.source === 'assistant' && file.isImage === true && !!file.url;
	};

	$: displayGeneratedFiles = generatedFiles.filter(
		(file) => !shouldInlineAssistantImageArtifact(file)
	);

	type ContentVisualSignatures = {
		imageUrls: Set<string>;
		tableSignatures: Set<string>;
		embedUrls: Set<string>;
	};

	const HTML_IMAGE_TAG_REGEX = /<img\b[^>]*\bsrc=(['"])(.*?)\1[^>]*>/gi;
	const HTML_IFRAME_TAG_REGEX = /<iframe\b[^>]*\bsrc=(['"])(.*?)\1[^>]*>/gi;
	const HTML_TABLE_TAG_REGEX = /<table\b[\s\S]*?<\/table>/gi;
	const MARKDOWN_IMAGE_REGEX = /!\[[^\]]*]\((\S+?)(?:\s+["'][^"']*["'])?\)/g;
	const MARKDOWN_TABLE_ALIGNMENT_REGEX =
		/^\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$/;
	const STANDALONE_EMBED_URL_REGEX =
		/^(?:https?:\/\/|\/|data:|blob:|(?:api|openai)\/v1\/|v1\/)/i;

	const normalizeVisualTextForMatching = (value: string): string =>
		value
			.replace(/\s+/g, ' ')
			.trim()
			.toLowerCase();

	const buildNormalizedVisualUrlSetKey = (urls: string[]): string =>
		Array.from(new Set(urls.filter(Boolean))).sort().join('|');

	const buildTableSignature = (rows: string[][]): string => {
		const normalizedRows = rows
			.map((row) =>
				row
					.map((cell) => normalizeVisualTextForMatching(cell))
					.filter(Boolean)
					.join('|')
			)
			.filter(Boolean);

		return normalizedRows.join('||');
	};

	const extractTableSignatureFromHtml = (html: string): string => {
		const normalizedHtml = (html ?? '').trim();
		if (!normalizedHtml || !normalizedHtml.includes('<table')) return '';

		if (typeof DOMParser !== 'undefined') {
			try {
				const doc = new DOMParser().parseFromString(normalizedHtml, 'text/html');
				const table = doc.querySelector('table');
				if (!table) return '';

				const rows = Array.from(table.querySelectorAll('tr'))
					.map((row) =>
						Array.from(row.querySelectorAll('th,td')).map((cell) => cell.textContent ?? '')
					)
					.filter((row) => row.some((cell) => normalizeVisualTextForMatching(cell)));

				return buildTableSignature(rows);
			} catch {
				// Fall through to regex-based extraction.
			}
		}

		const rows = Array.from(normalizedHtml.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi), (rowMatch) =>
			Array.from(rowMatch[1].matchAll(/<t[hd]\b[^>]*>([\s\S]*?)<\/t[hd]>/gi), (cellMatch) =>
				cellMatch[1].replace(/<[^>]+>/g, ' ')
			)
		).filter((row) => row.some((cell) => normalizeVisualTextForMatching(cell)));

		return buildTableSignature(rows);
	};

	const extractImageUrlsFromHtml = (html: string): string[] =>
		Array.from(html.matchAll(HTML_IMAGE_TAG_REGEX), (match) =>
			normalizeVisualUrlForMatching(match[2])
		).filter(Boolean);

	const extractIframeUrlsFromHtml = (html: string): string[] =>
		Array.from(html.matchAll(HTML_IFRAME_TAG_REGEX), (match) =>
			normalizeVisualUrlForMatching(match[2])
		).filter(Boolean);

	const extractStandaloneEmbedUrl = (value: string): string => {
		const normalized = value.trim();
		if (!normalized || normalized.includes('<') || !STANDALONE_EMBED_URL_REGEX.test(normalized)) {
			return '';
		}

		return normalizeVisualUrlForMatching(normalized);
	};

	const extractTableRowCells = (line: string): string[] => {
		const trimmed = line.trim();
		if (!trimmed.includes('|')) return [];

		const segments = trimmed
			.split('|')
			.map((segment) => segment.trim())
			.filter((segment, index, array) => {
				if (segment) return true;
				return index !== 0 && index !== array.length - 1;
			});

		return segments;
	};

	const extractTableSignaturesFromMarkdown = (content: string): string[] => {
		const lines = content.split(/\r?\n/);
		const signatures: string[] = [];

		for (let index = 0; index < lines.length - 1; index += 1) {
			const headerLine = lines[index]?.trim() ?? '';
			const alignmentLine = lines[index + 1]?.trim() ?? '';
			if (!headerLine.startsWith('|') || !MARKDOWN_TABLE_ALIGNMENT_REGEX.test(alignmentLine)) {
				continue;
			}

			const rows = [extractTableRowCells(headerLine)];
			let nextIndex = index + 2;
			while (nextIndex < lines.length) {
				const rowLine = lines[nextIndex]?.trim() ?? '';
				if (!rowLine.startsWith('|') || !rowLine.includes('|')) {
					break;
				}
				rows.push(extractTableRowCells(rowLine));
				nextIndex += 1;
			}

			const signature = buildTableSignature(rows);
			if (signature) {
				signatures.push(signature);
			}

			index = nextIndex - 1;
		}

		return signatures;
	};

	const collectContentVisualSignatures = (content: string): ContentVisualSignatures => {
		const imageUrls = new Set<string>();
		const tableSignatures = new Set<string>();
		const embedUrls = new Set<string>();
		const normalizedContent = content.trim();

		if (!normalizedContent) {
			return { imageUrls, tableSignatures, embedUrls };
		}

		Array.from(normalizedContent.matchAll(MARKDOWN_IMAGE_REGEX), (match) =>
			normalizeVisualUrlForMatching(match[1])
		)
			.filter(Boolean)
			.forEach((url) => imageUrls.add(url));

		extractTableSignaturesFromMarkdown(normalizedContent).forEach((signature) =>
			tableSignatures.add(signature)
		);

		extractImageUrlsFromHtml(normalizedContent).forEach((url) => imageUrls.add(url));
		extractIframeUrlsFromHtml(normalizedContent).forEach((url) => embedUrls.add(url));
		Array.from(normalizedContent.matchAll(HTML_TABLE_TAG_REGEX), (match) => match[0]).forEach(
			(tableHtml) => {
				const signature = extractTableSignatureFromHtml(tableHtml);
				if (signature) tableSignatures.add(signature);
			}
		);

		return { imageUrls, tableSignatures, embedUrls };
	};

	const renderedAssistantContentForVisualMatching = (state: {
		placeInlineGeneratedFiles: boolean;
		finalMessageContent: string;
		finalContentBeforeGeneratedFiles: string;
		finalContentAfterGeneratedFiles: string;
	}): string =>
		(
			state.placeInlineGeneratedFiles
				? [state.finalContentBeforeGeneratedFiles, state.finalContentAfterGeneratedFiles]
				: [state.finalMessageContent]
		)
			.filter((value) => typeof value === 'string' && value.trim().length > 0)
			.join('\n\n')
			.trim();

	let renderedContentVisualSignatures: ContentVisualSignatures = {
		imageUrls: new Set<string>(),
		tableSignatures: new Set<string>(),
		embedUrls: new Set<string>()
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

	const getEmbedCanonicalKey = (embed: string): string => {
		const normalizedEmbed = embed.trim();
		if (!normalizedEmbed) return '';

		const tableSignature = extractTableSignatureFromHtml(normalizedEmbed);
		if (tableSignature) {
			return `table:${tableSignature}`;
		}

		const imageKey = buildNormalizedVisualUrlSetKey(extractImageUrlsFromHtml(normalizedEmbed));
		if (imageKey) {
			return `image:${imageKey}`;
		}

		const iframeKey = buildNormalizedVisualUrlSetKey(extractIframeUrlsFromHtml(normalizedEmbed));
		if (iframeKey) {
			return `iframe:${iframeKey}`;
		}

		const standaloneUrl = extractStandaloneEmbedUrl(normalizedEmbed);
		if (standaloneUrl) {
			return `url:${standaloneUrl}`;
		}

		return `html:${normalizedEmbed}`;
	};

	const dedupeEmbedsForDisplay = (embeds: unknown[]): string[] => {
		const deduped: string[] = [];
		const indexByKey = new Map<string, number>();

		for (const embed of embeds) {
			if (typeof embed !== 'string') continue;
			const normalized = embed.trim();
			if (!normalized) continue;

			const key = getEmbedCanonicalKey(normalized);
			const existingIndex = indexByKey.get(key);
			if (existingIndex === undefined) {
				indexByKey.set(key, deduped.length);
				deduped.push(normalized);
				continue;
			}

			if (scoreEmbedContent(normalized) >= scoreEmbedContent(deduped[existingIndex] ?? '')) {
				deduped[existingIndex] = normalized;
			}
		}

		return deduped;
	};

	const isEmbedDuplicatedByContent = (
		embed: string,
		contentVisualSignatures: ContentVisualSignatures
	): boolean => {
		const normalizedEmbed = embed.trim();
		if (!normalizedEmbed) return true;

		const tableSignature = extractTableSignatureFromHtml(normalizedEmbed);
		if (tableSignature && contentVisualSignatures.tableSignatures.has(tableSignature)) {
			return true;
		}

		const imageUrls = extractImageUrlsFromHtml(normalizedEmbed);
		if (imageUrls.length > 0) {
			return imageUrls.every((url) => contentVisualSignatures.imageUrls.has(url));
		}

		const iframeUrls = extractIframeUrlsFromHtml(normalizedEmbed);
		if (iframeUrls.length > 0) {
			return iframeUrls.every((url) => contentVisualSignatures.embedUrls.has(url));
		}

		const standaloneUrl = extractStandaloneEmbedUrl(normalizedEmbed);
		if (standaloneUrl) {
			return contentVisualSignatures.embedUrls.has(standaloneUrl);
		}

		return false;
	};

	const isMessageFileDuplicatedByContent = (
		file: Record<string, unknown>,
		contentVisualSignatures: ContentVisualSignatures
	): boolean => {
		const candidateUrls = [
			file?.url,
			file?.download_url,
			file?.bridge_url,
			file?.generated_file_url,
			file?.path
		]
			.map((candidate) => normalizeVisualUrlForMatching(candidate))
			.filter(Boolean);

		return candidateUrls.some((url) => contentVisualSignatures.imageUrls.has(url));
	};

	const normalizeGeneratedFileMatchToken = (value: unknown): string => {
		if (typeof value !== 'string') return '';
		return value
			.trim()
			.replace(/^[\-\u2022*]+\s*/, '')
			.replace(/^['"`“”‘’]+|['"`“”‘’]+$/g, '')
			.replace(/[.,;:!?，。！？；：）】》」』]+$/g, '')
			.trim()
			.toLowerCase();
	};

	const normalizeWorkbookVariantMatchToken = (value: string): string => {
		if (!/\.(?:xlsx|xls)$/i.test(value)) return value;
		return value.replace(/(?:[_\-\s]|\s*\()\d+\)?(?=\.[^.]+$)/, '');
	};

	const buildGeneratedFileMatchKeys = (files: GeneratedFileItem[]): Set<string> => {
		const matchKeys = new Set<string>();

		for (const file of files) {
			for (const candidate of [
				file.name,
				inferFileName(file.url ?? file.path ?? ''),
				file.url,
				file.path
			]) {
				const normalized = normalizeGeneratedFileMatchToken(candidate);
				if (normalized) {
					matchKeys.add(normalized);
					const variantNormalized = normalizeWorkbookVariantMatchToken(normalized);
					if (variantNormalized) {
						matchKeys.add(variantNormalized);
					}
				}
			}
		}

		return matchKeys;
	};

	const isMessageFileDuplicatedByGeneratedFiles = (
		file: Record<string, unknown>,
		matchKeys: Set<string>
	): boolean => {
		for (const candidate of [
			file?.name,
			file?.filename,
			file?.fileName,
			file?.url,
			file?.path,
			inferFileName(String(file?.url ?? file?.path ?? file?.id ?? ''))
		]) {
			const normalized = normalizeGeneratedFileMatchToken(candidate);
			if (normalized && matchKeys.has(normalized)) {
				return true;
			}
			const variantNormalized = normalizeWorkbookVariantMatchToken(normalized);
			if (variantNormalized && matchKeys.has(variantNormalized)) {
				return true;
			}
		}

		return false;
	};

	const updateVisibleMessageAttachments = (renderState: {
		finalMessageContent: string;
		finalContentBeforeGeneratedFiles: string;
		finalContentAfterGeneratedFiles: string;
		placeInlineGeneratedFiles: boolean;
	}) => {
		renderedContentVisualSignatures = collectContentVisualSignatures(
			renderedAssistantContentForVisualMatching({
				placeInlineGeneratedFiles: renderState.placeInlineGeneratedFiles,
				finalMessageContent: renderState.finalMessageContent,
				finalContentBeforeGeneratedFiles: renderState.finalContentBeforeGeneratedFiles,
				finalContentAfterGeneratedFiles: renderState.finalContentAfterGeneratedFiles
			})
		);

		const messageFiles = Array.isArray(message?.files) ? message.files : [];
		const hasPrimaryDocumentArtifact = displayGeneratedFiles.some((file) =>
			isPrimaryDocumentArtifact(file)
		);
		if (message?.role !== 'assistant' || messageFiles.length === 0) {
			visibleMessageFiles = messageFiles;
		} else {
			const matchKeys = buildGeneratedFileMatchKeys(displayGeneratedFiles);
			visibleMessageFiles = messageFiles.filter(
				(file: Record<string, unknown>) =>
					!isMessageFileDuplicatedByGeneratedFiles(file, matchKeys) &&
					!isMessageFileDuplicatedByContent(file, renderedContentVisualSignatures) &&
					!shouldHideHelperArtifact(
						{
							name:
								(typeof file?.name === 'string' && file.name) ||
								(typeof file?.filename === 'string' && file.filename) ||
								(typeof file?.fileName === 'string' && file.fileName) ||
								inferFileName(String(file?.url ?? file?.path ?? file?.id ?? '')),
							url: typeof file?.url === 'string' ? file.url : undefined,
							path: typeof file?.path === 'string' ? file.path : undefined,
							source: 'assistant'
						},
						hasPrimaryDocumentArtifact
					)
			);
		}

		const messageEmbeds = Array.isArray(message?.embeds) ? dedupeEmbedsForDisplay(message.embeds) : [];
		if (message?.role !== 'assistant' || messageEmbeds.length === 0) {
			visibleMessageEmbeds = messageEmbeds;
		} else {
			visibleMessageEmbeds = messageEmbeds.filter(
				(embed: unknown) =>
					typeof embed === 'string' &&
					embed.trim().length > 0 &&
					!isEmbedDuplicatedByContent(embed, renderedContentVisualSignatures)
			);
		}
	};

	type ActiveTerminal = { url: string; key: string } | null;

	$: systemTerminal = $selectedTerminalId
		? (($terminalServers ?? []).find((terminal: any) => terminal.id === $selectedTerminalId) ??
			null)
		: (($terminalServers ?? [])[0] ?? null);
	$: directTerminal =
		($settings?.terminalServers ?? []).find(
			(server: any) => server.url === $selectedTerminalId && server.enabled
		) ?? null;
	$: activeTerminal = (
		directTerminal
			? { url: directTerminal.url, key: directTerminal.api_key }
			: systemTerminal
				? { url: systemTerminal.url, key: systemTerminal.key }
				: null
	) as ActiveTerminal;

	const openGeneratedFile = (file: GeneratedFileItem) => {
		openGeneratedFilePreview(file.id, {
			showControls,
			showFilePreview,
			selectedGeneratedFilePreviewId,
			showOverview,
			showArtifacts,
			showEmbeds,
			showCallOverlay
		});
	};

	const downloadGeneratedFile = async (file: GeneratedFileItem) => {
		if (file.downloadMode === 'terminal' && file.path && activeTerminal) {
			const result = await downloadFileBlob(activeTerminal.url, activeTerminal.key, file.path);
			if (!result) return;

			const objectUrl = URL.createObjectURL(result.blob);
			const link = document.createElement('a');
			link.href = objectUrl;
			link.download = result.filename || file.name;
			link.click();
			URL.revokeObjectURL(objectUrl);
			return;
		}

		if (!file.url) return;
		await triggerGeneratedFileDownload(file.url, file.name);
	};

	const getGeneratedFileBadgeLabel = (file: GeneratedFileItem): string => {
		const extensionMatch = file.name.match(/\.([A-Za-z0-9]{1,16})$/);
		if (extensionMatch?.[1]) {
			return extensionMatch[1].toUpperCase();
		}
		return file.isImage ? 'IMAGE' : 'FILE';
	};

	const getGeneratedFileSummary = (file: GeneratedFileItem, active: boolean): string => {
		const badgeLabel = getGeneratedFileBadgeLabel(file);
		const actionLabel = active ? '预览已打开' : '点击查看预览';
		return badgeLabel ? `${badgeLabel} · ${actionLabel}` : actionLabel;
	};

	const getGeneratedFileContainerClass = (active: boolean): string =>
		active
			? 'border-blue-200 bg-blue-50/90 shadow-xs dark:border-blue-900 dark:bg-blue-950/40'
			: 'border-gray-200/90 bg-gray-50/85 hover:border-gray-300 hover:bg-gray-100/85 dark:border-gray-800 dark:bg-gray-900/80 dark:hover:border-gray-700 dark:hover:bg-gray-900';

	const INLINE_GENERATED_FILES_MARKER = '<!--__GENERATED_FILES__-->';

	const GENERATED_FILE_LINK_REGEX =
		/(?:^|https?:\/\/[^/]+\/)(?:api\/v1|openai\/v1|v1)\/files\/[^)\s?#]+(?:\/content)?(?:[?#][^)\s]*)?$/i;

	const isGeneratedFileLinkTarget = (value: string): boolean => {
		const normalized = value.trim();
		if (!normalized) return false;
		if (normalized.includes('sandbox:/mnt/data/')) return true;
		if (GENERATED_FILE_LINK_REGEX.test(normalized)) return true;
		if (isDownloadRef(normalized) && /(?:\/files\/|\/content(?:[?#].*)?$)/i.test(normalized)) {
			return true;
		}
		return false;
	};

	const normalizeSourcesHeading = (content: string): string => {
		if (!content) return '';
		return content.replace(
			/(^|\n)(#{1,6}\s*)?Sources\s*(?=\n|$)/gim,
			(_, prefix: string) => `${prefix}参考来源`
		);
	};

	const normalizeStructuredDetailsTags = (content: string): string => {
		if (!content) return '';
		return content
			.replace(/<details(?=[a-zA-Z_:][-a-zA-Z0-9_:.]*=)/gi, '<details ')
			.replace(/<summary(?=[a-zA-Z_:][-a-zA-Z0-9_:.]*=)/gi, '<summary ');
	};

	const TOOL_CALL_BLOCK_REGEX = /<details\b[^>]*\btype="tool_calls"[^>]*>[\s\S]*?<\/details>/gim;
	const REASONING_BLOCK_REGEX = /<details\b[^>]*\btype="reasoning"[^>]*>[\s\S]*?<\/details>/gim;
	const REASONING_OPEN_BLOCK_REGEX = /<details\b[^>]*\btype="reasoning"[^>]*>/gi;
	const REASONING_SUMMARY_REGEX = /<summary>[\s\S]*?<\/summary>/i;
	const REASONING_CLOSE_TAG = '</details>';

	type ProcessToolCallItem = { key: string; attrs: Record<string, string> };
	type ProcessToolVisualTiming = { firstSeenAt: number };

	const MIN_PROCESS_RUNNING_MS = 450;
	const processToolVisualTimingByKeyByMessageId = new Map<
		string,
		Map<string, ProcessToolVisualTiming>
	>();

	const upsertToolCallAttr = (openTag: string, name: string, value: string): string => {
		const attrRegex = new RegExp(`\\s${name}="[^"]*"`, 'i');
		if (attrRegex.test(openTag)) {
			return openTag.replace(attrRegex, ` ${name}="${value}"`);
		}
		return openTag.replace(/>$/, ` ${name}="${value}">`);
	};

	const promoteFileGeneratingToolCallBlocks = (
		content: string,
		files: GeneratedFileItem[]
	): string => {
		if (!content || !content.includes('type="tool_calls"')) return content;
		if (!files.some((file) => isPrimaryDocumentArtifact(file))) return content;

		return content.replace(TOOL_CALL_BLOCK_REGEX, (block) => {
			const openTagMatch = block.match(/^<details\b[^>]*>/i);
			if (!openTagMatch) return block;

			const attrs = getToolCallAttrs(block);
			if (!isFileGeneratingToolId(attrs.tool_id || attrs.tool_name || attrs.name)) {
				return block;
			}

			const resolvedStatus = resolveToolCallStatus(attrs, { promoteArtifactRunning: false });
			if (
				resolvedStatus === 'success' ||
				resolvedStatus === 'error' ||
				resolvedStatus === 'timeout'
			) {
				return block;
			}

			let normalizedOpenTag = upsertToolCallAttr(openTagMatch[0], 'status', 'success');
			normalizedOpenTag = upsertToolCallAttr(normalizedOpenTag, 'done', 'true');
			return `${normalizedOpenTag}${block.slice(openTagMatch[0].length)}`;
		});
	};

	const getToolCallKey = (attrs: Record<string, string>): string => {
		const id = (attrs.id || '').trim();
		if (id) return `id:${id}`;
		const callKey = (attrs.call_key || '').trim();
		if (callKey) return `call_key:${callKey}`;
		const name = (attrs.tool_id || attrs.name || '').trim();
		const args = (attrs.arguments || '').trim();
		if (!name && !args) return '';
		return `name_args:${name}|${args}`;
	};

	const getToolCallFallbackKey = (attrs: Record<string, string>): string => {
		const name = (attrs.tool_id || attrs.name || '').trim();
		const args = (attrs.arguments || '').trim();
		if (!name && !args) return '';
		return `name_args:${name}|${args}`;
	};

	const normalizeOutputToolStatus = (value: unknown): string => {
		if (typeof value !== 'string') return '';
		const normalized = value.trim().toLowerCase();
		if (!normalized) return '';
		if (normalized === 'in_progress') return 'running';
		if (normalized === 'completed') return 'success';
		return normalized;
	};

	const normalizeToolCallPayloadValue = (value: unknown): string => {
		if (value === null || value === undefined) return '';
		if (typeof value === 'string') return value;
		return safeStringify(value);
	};

	const extractToolCallOutputText = (value: unknown): string => {
		if (typeof value === 'string') return value;
		if (Array.isArray(value)) {
			return value
				.map((part) => {
					if (typeof part === 'string') return part;
					if (!part || typeof part !== 'object') return '';
					const record = part as Record<string, unknown>;
					const text = record.text ?? record.output ?? record.content;
					if (typeof text === 'string') return text;
					if (text === null || text === undefined) return '';
					return safeStringify(text);
				})
				.filter(Boolean)
				.join('\n')
				.trim();
		}
		if (value && typeof value === 'object') {
			return safeStringify(value);
		}
		return String(value ?? '');
	};

	const parseStructuredToolPayload = (value: unknown): unknown => {
		if (Array.isArray(value)) {
			return parseNestedJSON(extractToolCallOutputText(value));
		}
		return parseNestedJSON(value);
	};

	const getToolPayloadRecord = (value: unknown): Record<string, unknown> | null => {
		const parsed = parseStructuredToolPayload(value);
		return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
			? (parsed as Record<string, unknown>)
			: null;
	};

	const getSearchQueryFromToolPayload = (value: unknown): string => {
		const record = getToolPayloadRecord(value);
		if (!record) return '';

		for (const key of ['query', 'q', 'keyword', 'keywords', 'search_query']) {
			const candidate = record[key];
			if (typeof candidate === 'string' && candidate.trim()) {
				return candidate.trim();
			}
		}

		return '';
	};

	const normalizeToolCallDisplayPayload = (
		toolName: string,
		rawArguments: unknown,
		rawResult: unknown
	): { argumentsValue: unknown; resultValue: unknown } => {
		if (normalizeToolId(toolName) !== 'internet_search') {
			return {
				argumentsValue: rawArguments,
				resultValue: rawResult
			};
		}

		const query =
			getSearchQueryFromToolPayload(rawArguments) || getSearchQueryFromToolPayload(rawResult);
		const parsedArguments = getToolPayloadRecord(rawArguments);
		const parsedResult = getToolPayloadRecord(rawResult);

		let argumentsValue: unknown = rawArguments;
		if (query && !getSearchQueryFromToolPayload(rawArguments)) {
			argumentsValue = { ...(parsedArguments ?? {}), query };
		}

		let resultValue: unknown = rawResult;
		if (parsedResult && Object.keys(parsedResult).some((key) => key !== 'query')) {
			const nextResult = { ...parsedResult };
			delete nextResult.query;
			resultValue = nextResult;
		}

		return { argumentsValue, resultValue };
	};

	const isHiddenProcessToolCall = (
		toolName: string,
		rawArguments: unknown
	): boolean => {
		const parsedArgs = getToolPayloadRecord(rawArguments);
		return isHiddenHelperToolCall({
			toolId: toolName,
			toolName,
			legacyName: toolName,
			parsedArgs
		});
	};

	const isHiddenProcessToolCallAttrs = (attrs: Record<string, string>): boolean => {
		const toolName = String(attrs.tool_id || attrs.tool_name || attrs.name || '').trim();
		if (!toolName) return false;
		return isHiddenProcessToolCall(toolName, attrs.arguments);
	};

	const collectToolCallsFromOutput = (value: unknown): ProcessToolCallItem[] => {
		if (!Array.isArray(value)) return [];
		const grouped = new Map<
			string,
			{ call?: Record<string, unknown>; output?: Record<string, unknown> }
		>();

		for (const entry of value) {
			if (!entry || typeof entry !== 'object') continue;
			const record = entry as Record<string, unknown>;
			const type = record.type;
			if (type !== 'function_call' && type !== 'function_call_output') continue;
			const callId = String(record.call_id ?? record.id ?? '').trim();
			const key = callId || `${type}:${record.id ?? ''}`;
			const group = grouped.get(key) ?? {};
			if (type === 'function_call') {
				group.call = record;
			} else {
				group.output = record;
			}
			grouped.set(key, group);
		}

		const items: ProcessToolCallItem[] = [];
		for (const [fallbackKey, group] of grouped.entries()) {
			const callItem = group.call ?? {};
			const outputItem = group.output ?? {};
			const toolName = String((callItem.name ?? outputItem.name ?? 'tool') as string).trim();
			const statusValue = normalizeOutputToolStatus(outputItem.status ?? callItem.status ?? '');
			const hasTerminalOutput =
				group.output !== undefined &&
				[
					outputItem.output,
					outputItem.result,
					outputItem.files,
					outputItem.embeds
				].some((candidate) => {
					if (candidate === null || candidate === undefined) return false;
					if (typeof candidate === 'string') return candidate.trim().length > 0;
					if (Array.isArray(candidate)) return candidate.length > 0;
					if (typeof candidate === 'object') return Object.keys(candidate).length > 0;
					return true;
				});
			const done =
				statusValue === 'running'
					? 'false'
					: statusValue
						? 'true'
						: hasTerminalOutput
							? 'true'
							: 'false';
			const normalizedPayload = normalizeToolCallDisplayPayload(
				toolName,
				callItem.arguments,
				outputItem.output ?? outputItem.result ?? ''
			);
			const attrs: Record<string, string> = {
				type: 'tool_calls',
				id: String(callItem.id ?? outputItem.id ?? ''),
				call_key: String(callItem.call_id ?? outputItem.call_id ?? ''),
				name: toolName,
				tool_id: toolName,
				tool_name: toolName,
				arguments: normalizeToolCallPayloadValue(normalizedPayload.argumentsValue),
				result: extractToolCallOutputText(normalizedPayload.resultValue),
				files: normalizeToolCallPayloadValue(outputItem.files),
				embeds: normalizeToolCallPayloadValue(outputItem.embeds),
				status: statusValue,
				done
			};
			const key = getToolCallKey(attrs) || getToolCallFallbackKey(attrs) || fallbackKey;
			items.push({ key: key || `${toolName}-${items.length}`, attrs });
		}

		return items;
	};

	const outputHasStructuredAssistantItems = (value: unknown): boolean => {
		if (!Array.isArray(value)) return false;

		const hasVisibleNonToolStructuredItems = value.some((item) => {
			if (!item || typeof item !== 'object') return false;
			const record = item as Record<string, unknown>;
			return (
				record.type !== 'message' &&
				record.type !== 'function_call' &&
				record.type !== 'function_call_output'
			);
		});

		if (hasVisibleNonToolStructuredItems) return true;

		return collectToolCallsFromOutput(value).some(
			(item) => !isHiddenProcessToolCallAttrs(item.attrs ?? {})
		);
	};

	const outputHasAssistantMessageItem = (value: unknown): boolean =>
		Array.isArray(value) &&
		value.some((item) => {
			if (!item || typeof item !== 'object') return false;
			const record = item as Record<string, unknown>;
			return record.type === 'message' && record.role === 'assistant';
		});

	const getVisibleAssistantContent = (content: string, output: unknown): string => {
		const outputMessageText = extractAssistantMessageTextFromOutput(output);
		if (outputMessageText) return outputMessageText;
		if (!content) return '';
		if (!outputHasStructuredAssistantItems(output)) return content;
		return outputHasAssistantMessageItem(output) ? content : '';
	};

	const getProcessToolCallStatus = (item: ProcessToolCallItem): string =>
		resolveToolCallStatus(item.attrs ?? {}, {
			promoteArtifactRunning: getToolCallArtifactEvidence(item.attrs ?? {})
		});

	const getProcessToolCallSectionStatus = (items: ProcessToolCallItem[]): string => {
		if (items.some((item) => getProcessToolCallStatus(item) === 'running')) {
			return 'running';
		}
		if (items.some((item) => getProcessToolCallStatus(item) === 'error')) {
			return 'error';
		}
		if (items.some((item) => getProcessToolCallStatus(item) === 'timeout')) {
			return 'timeout';
		}
		return items.length > 0 ? 'success' : '';
	};

	const getProcessToolCallSectionSummary = (items: ProcessToolCallItem[]): string => {
		const count = items.length;
		if (!count) return '';

		const status = getProcessToolCallSectionStatus(items);
		if (status === 'running') {
			return `${count} 个工具执行中`;
		}
		if (status === 'error') {
			return `${count} 个工具，包含失败`;
		}
		if (status === 'timeout') {
			return `${count} 个工具，包含超时`;
		}
		return `${count} 个工具已完成`;
	};

	const getProcessToolCallSectionBadgeClass = (status: string): string => {
		if (status === 'success') {
			return 'border-emerald-200 bg-emerald-100 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300';
		}
		if (status === 'error') {
			return 'border-rose-200 bg-rose-100 text-rose-700 dark:border-rose-900 dark:bg-rose-950/60 dark:text-rose-300';
		}
		if (status === 'timeout') {
			return 'border-amber-200 bg-amber-100 text-amber-700 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300';
		}
		return 'border-blue-200 bg-blue-100 text-blue-700 dark:border-blue-900 dark:bg-blue-950/60 dark:text-blue-300';
	};

	const getProcessToolCallSectionBadgeLabel = (status: string): string => {
		if (status === 'success') return '已完成';
		if (status === 'error') return '失败';
		if (status === 'timeout') return '超时';
		return '执行中';
	};

	const getProcessToolCallTimelineDotClass = (status: string): string => {
		if (status === 'success') {
			return 'bg-emerald-500 ring-emerald-100 dark:bg-emerald-400 dark:ring-emerald-950/80';
		}
		if (status === 'error') {
			return 'bg-rose-500 ring-rose-100 dark:bg-rose-400 dark:ring-rose-950/80';
		}
		if (status === 'timeout') {
			return 'bg-amber-500 ring-amber-100 dark:bg-amber-400 dark:ring-amber-950/80';
		}
		return 'bg-blue-500 ring-blue-100 dark:bg-blue-400 dark:ring-blue-950/80';
	};

	const mergeToolCallAttrs = (
		base: Record<string, string>,
		next: Record<string, string>
	): Record<string, string> => {
		const merged = { ...base };
		for (const [key, value] of Object.entries(next)) {
			if (value) {
				merged[key] = value;
			}
		}
		return merged;
	};

	const filterHiddenProcessToolCalls = (
		items: ProcessToolCallItem[]
	): ProcessToolCallItem[] =>
		items.filter((item) => !isHiddenProcessToolCallAttrs(item.attrs ?? {}));

	const mergeProcessToolCalls = (
		contentItems: ProcessToolCallItem[],
		outputItems: ProcessToolCallItem[]
	): ProcessToolCallItem[] => {
		if (outputItems.length === 0) {
			return contentItems;
		}

		const merged = [...contentItems];
		const indexByKey = new Map(merged.map((item, index) => [item.key, index]));

		for (const outputItem of outputItems) {
			const existingIndex = indexByKey.get(outputItem.key);
			if (existingIndex === undefined) {
				indexByKey.set(outputItem.key, merged.length);
				merged.push(outputItem);
				continue;
			}
			const existing = merged[existingIndex];
			merged[existingIndex] = {
				key: existing.key,
				attrs: mergeToolCallAttrs(existing.attrs, outputItem.attrs)
			};
		}

		return merged;
	};

	const extractProcessToolCallItemsFromMarkup = (markup: string): ProcessToolCallItem[] => {
		if (!markup) return [];

		return Array.from(markup.matchAll(TOOL_CALL_BLOCK_REGEX))
			.map((match, index) => {
				const block = match[0] || '';
				const attrs = getToolCallAttrs(block);
				const key =
					(attrs.call_key || '').trim() ||
					(attrs.id || '').trim() ||
					`${(attrs.tool_id || attrs.name || '').trim()}-${index}`;

				return {
					key,
					attrs
				};
			})
			.filter((item) => Object.keys(item.attrs).length > 0);
	};

	const dedupeToolCallBlocks = (content: string): string => {
		if (!content || !content.includes('type="tool_calls"')) return content;

		const matches = Array.from(content.matchAll(TOOL_CALL_BLOCK_REGEX));
		if (matches.length === 0) return content;
		const blocks = matches.map((match) => {
			const block = match[0] || '';
			const attrs = getToolCallAttrs(block);
			const isPending = (attrs.done || '').toLowerCase() !== 'true';
			const name = (attrs.tool_id || attrs.name || '').trim();
			return {
				index: match.index ?? -1,
				block,
				attrs,
				isPending,
				name
			};
		});

		const completedKeys = new Set<string>();
		const completedFallbackKeys = new Set<string>();
		for (const item of blocks) {
			const { attrs, isPending } = item;
			if (isPending) continue;
			const key = getToolCallKey(attrs);
			if (key) completedKeys.add(key);
			const fallbackKey = getToolCallFallbackKey(attrs);
			if (fallbackKey) completedFallbackKeys.add(fallbackKey);
		}

		let cursor = 0;
		let out = '';
		for (let i = 0; i < blocks.length; i += 1) {
			const { index, block, attrs, isPending } = blocks[i];
			if (index < 0) continue;

			out += content.slice(cursor, index);
			cursor = index + block.length;

			const key = getToolCallKey(attrs);
			const fallbackKey = getToolCallFallbackKey(attrs);
			const shouldDrop =
				isPending &&
				((key && completedKeys.has(key)) ||
					(fallbackKey && completedFallbackKeys.has(fallbackKey)));

			if (!shouldDrop) {
				out += block;
			}
		}

		out += content.slice(cursor);
		return out.replace(/\n{3,}/g, '\n\n').trim();
	};

	const getStandaloneGeneratedFileLineValue = (value: string): string => {
		const normalized = normalizeGeneratedFileMatchToken(value);
		if (!normalized) return '';

		const labeledMatch = normalized.match(
			/^(?:文件名|输出文件|生成文件|output[_ ]?file|file(?:name)?|generated file)\s*[:：]\s*(.+)$/i
		);
		if (labeledMatch?.[1]) {
			return normalizeGeneratedFileMatchToken(labeledMatch[1]);
		}

		return normalized;
	};

	const isStandaloneGeneratedFileLine = (
		line: string,
		generatedFiles: GeneratedFileItem[]
	): boolean => {
		if (!generatedFiles.length) return false;
		return buildGeneratedFileMatchKeys(generatedFiles).has(
			getStandaloneGeneratedFileLineValue(line)
		);
	};

	const stripDownloadSection = (content: string, generatedFiles: GeneratedFileItem[]): string => {
		if (!content) return '';

		const allowInlineFiles = generatedFiles.length > 0;
		const lines = content.split('\n');
		const output: string[] = [];
		let markerInserted = false;

		for (const line of lines) {
			const trimmed = line.trim();
			const isMarkdownImageLine = trimmed.startsWith('![');
			const linkMatch = trimmed.match(/\[[^\]]+\]\(([^)]+)\)/);
			const linkTarget = linkMatch?.[1] || '';
			const hasGeneratedFileLink =
				!isMarkdownImageLine && isGeneratedFileLinkTarget(linkTarget);

			const hasDownloadLabel =
				allowInlineFiles &&
				/(文件下载|下载链接|下载地址|生成文件（可下载）|生成文件\(可下载\))/i.test(trimmed);

			if (
				hasDownloadLabel ||
				hasGeneratedFileLink ||
				isStandaloneGeneratedFileLine(trimmed, generatedFiles)
			) {
				if (allowInlineFiles && !markerInserted) {
					output.push(INLINE_GENERATED_FILES_MARKER);
					markerInserted = true;
				}
				continue;
			}

			output.push(line);
		}

		return output
			.join('\n')
			.replace(/\n{3,}/g, '\n\n')
			.trim();
	};

	const splitToolCallSection = (content: string): { process: string; final: string } => {
		if (!content) return { process: '', final: '' };

		const matches = Array.from(content.matchAll(TOOL_CALL_BLOCK_REGEX));

		if (matches.length === 0) {
			return {
				process: '',
				final: content.trim()
			};
		}

		const process = matches
			.map((match) => (match[0] || '').trim())
			.filter(Boolean)
			.join('\n\n')
			.trim();

		let cursor = 0;
		let final = '';
		for (const match of matches) {
			const start = match.index ?? -1;
			if (start < 0) continue;
			final += content.slice(cursor, start);
			cursor = start + (match[0] || '').length;
		}
		final += content.slice(cursor);

		return {
			process,
			final: final.replace(/\n{3,}/g, '\n\n').trim()
		};
	};

	const escapeHtmlForStructuredBlock = (value: string): string =>
		value
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
			.replace(/"/g, '&quot;');

	const serializeReasoningTextBlock = (
		reasoningText: string,
		options: { done: boolean; duration?: number }
	): string => {
		const text = reasoningText.trim();
		if (!text) return '';

		const display = escapeHtmlForStructuredBlock(
			text
				.split(/\r?\n/)
				.map((line) => (line.startsWith('>') ? line : `> ${line}`))
				.join('\n')
		);

		if (options.done) {
			return `<details type="reasoning" done="true" duration="${options.duration ?? 0}">\n<summary>Thought for ${options.duration ?? 0} seconds</summary>\n${display}\n</details>`;
		}

		return `<details type="reasoning" done="false">\n<summary>Thinking...</summary>\n${display}\n</details>`;
	};

	const extractOutputTextParts = (value: unknown): string => {
		if (!Array.isArray(value)) return '';

		return value
			.map((part) => {
				if (!part || typeof part !== 'object') return '';
				const record = part as Record<string, unknown>;
				const text = record.text;
				if (typeof text === 'string') return text;
				if (text === null || text === undefined) return '';
				return safeStringify(text);
			})
			.filter(Boolean)
			.join('');
	};

	const extractAssistantMessageTextFromOutput = (value: unknown): string => {
		if (!Array.isArray(value)) return '';

		for (let index = value.length - 1; index >= 0; index -= 1) {
			const item = value[index];
			if (!item || typeof item !== 'object') continue;

			const record = item as Record<string, unknown>;
			if (record.type !== 'message' || record.role !== 'assistant') continue;

			const contentText = extractOutputTextParts(record.content);
			if (contentText.trim()) return contentText.trim();

			const summaryText = extractOutputTextParts(record.summary);
			if (summaryText.trim()) return summaryText.trim();
		}

		return '';
	};

	const serializeReasoningOutputForDisplay = (value: unknown): string => {
		if (!Array.isArray(value)) return '';

		const blocks: string[] = [];

		value.forEach((entry, index) => {
			if (!entry || typeof entry !== 'object') return;
			const item = entry as Record<string, unknown>;
			if (item.type !== 'reasoning') return;

			const sourceParts =
				Array.isArray(item.summary) && item.summary.length > 0 ? item.summary : item.content;
			const reasoningText = extractOutputTextParts(sourceParts).trim();
			if (!reasoningText) return;

			const duration = typeof item.duration === 'number' ? item.duration : undefined;
			const status = typeof item.status === 'string' ? item.status : '';
			const isLastItem = index === value.length - 1;

			if (status === 'completed' || duration !== undefined || !isLastItem) {
				blocks.push(serializeReasoningTextBlock(reasoningText, { done: true, duration }));
				return;
			}

			blocks.push(serializeReasoningTextBlock(reasoningText, { done: false }));
		});

		return blocks.join('\n\n').trim();
	};

	const extractCompletedReasoningBlocks = (content: string): string[] => {
		if (!content || !content.includes('type="reasoning"')) return [];
		return Array.from(content.matchAll(REASONING_BLOCK_REGEX))
			.map((match) => (match[0] || '').trim())
			.filter(Boolean);
	};

	const extractReasoningBlocksFromMarkup = (markup: string): string[] => {
		if (!markup || !markup.includes('type="reasoning"')) return [];
		return Array.from(markup.matchAll(REASONING_BLOCK_REGEX))
			.map((match) => (match[0] || '').trim())
			.filter(Boolean);
	};

	const dedupeStructuredBlocks = (blocks: string[]): string[] => {
		const seen = new Set<string>();
		return blocks.filter((block) => {
			const normalized = block.trim();
			if (!normalized || seen.has(normalized)) return false;
			seen.add(normalized);
			return true;
		});
	};

	const mergeOutputReasoningIntoContent = (content: string, output: unknown): string => {
		const normalizedContent = normalizeStructuredDetailsTags(content);
		const completedContentBlocks = extractCompletedReasoningBlocks(normalizedContent);
		const outputBlocks = extractReasoningBlocksFromMarkup(serializeReasoningOutputForDisplay(output));
		const activeContentBlock =
			outputBlocks.length === 0
				? serializeReasoningTextBlock(extractActiveReasoningText(normalizedContent), {
						done: false
					})
				: '';
		const reasoningBlocks = dedupeStructuredBlocks([
			...outputBlocks,
			...completedContentBlocks,
			activeContentBlock
		]);
		const reasoningMarkup = reasoningBlocks.join('\n\n').trim();
		if (!reasoningMarkup) return normalizedContent;

		const contentWithoutReasoning = normalizedContent.includes('type="reasoning"')
			? stripActiveReasoningBlock(normalizedContent)
					.replace(REASONING_BLOCK_REGEX, '')
					.replace(/\n{3,}/g, '\n\n')
					.trim()
			: normalizedContent.trim();

		return contentWithoutReasoning
			? `${reasoningMarkup}\n\n${contentWithoutReasoning}`.trim()
			: reasoningMarkup;
	};

	const getCanonicalAssistantContent = (rawContent: string, output: unknown): string =>
		mergeOutputReasoningIntoContent(getVisibleAssistantContent(rawContent, output), output);

	const normalizeLegacyAssistantContent = (
		content: string,
		generatedFiles: GeneratedFileItem[]
	): string =>
		promoteFileGeneratingToolCallBlocks(
			dedupeToolCallBlocks(normalizeSourcesHeading(content)),
			generatedFiles
		);

	const resolveAssistantRenderState = (
		rawContent: string,
		output: unknown,
		generatedFiles: GeneratedFileItem[]
	): {
		processToolCallItems: ProcessToolCallItem[];
		finalMessageContent: string;
		finalContentBeforeGeneratedFiles: string;
		finalContentAfterGeneratedFiles: string;
		placeInlineGeneratedFiles: boolean;
	} => {
		// `message.output` is the canonical structured source of tool/process state.
		// Legacy tool-call markup in `content` is preserved only as a compatibility
		// fallback for historical chats and mixed records.
		const canonicalContent = getCanonicalAssistantContent(rawContent, output);
		const legacyCompatibleContent = normalizeLegacyAssistantContent(
			canonicalContent,
			generatedFiles
		);
		const { process, final } = splitToolCallSection(legacyCompatibleContent);
		const legacyProcessToolCallItems = extractProcessToolCallItemsFromMarkup(process);
		const structuredProcessToolCallItems = collectToolCallsFromOutput(output);
		const mergedProcessToolCallItems = filterHiddenProcessToolCalls(
			mergeProcessToolCalls(legacyProcessToolCallItems, structuredProcessToolCallItems)
		);

		const cleanedFinal = normalizeLeakedFormatting(
			stripDownloadSection(final, generatedFiles)
		);
		const renderState = {
			processToolCallItems: mergedProcessToolCallItems,
			finalMessageContent: cleanedFinal,
			finalContentBeforeGeneratedFiles: '',
			finalContentAfterGeneratedFiles: '',
			placeInlineGeneratedFiles: false
		};

		if (!cleanedFinal.includes(INLINE_GENERATED_FILES_MARKER)) {
			return renderState;
		}

		const [before = '', after = ''] = cleanedFinal.split(INLINE_GENERATED_FILES_MARKER, 2);
		return {
			...renderState,
			finalContentBeforeGeneratedFiles: before.trim(),
			finalContentAfterGeneratedFiles: after.trim(),
			placeInlineGeneratedFiles: true
		};
	};

	let processToolCallItems: ProcessToolCallItem[] = [];
	let processStatusTick = 0;
	let processStatusTimer: ReturnType<typeof setTimeout> | null = null;
	let finalMessageContent = '';
	let finalContentBeforeGeneratedFiles = '';
	let finalContentAfterGeneratedFiles = '';
	let placeInlineGeneratedFiles = false;
	let toolCallSectionOpen = false;
	let toolCallSectionStateKey = '';
	let toolCallSectionUserToggled = false;

	const getLatestReasoningOpenBlock = (content: string): { start: number; end: number } | null => {
		if (!content || !content.includes('type="reasoning"')) return null;

		let lastMatch: RegExpExecArray | null = null;
		for (const match of content.matchAll(REASONING_OPEN_BLOCK_REGEX)) {
			lastMatch = match;
		}

		if (!lastMatch || lastMatch.index === undefined) return null;

		return {
			start: lastMatch.index,
			end: lastMatch.index + lastMatch[0].length
		};
	};

	const stripActiveReasoningBlock = (content: string): string => {
		const latestBlock = getLatestReasoningOpenBlock(content);
		if (!latestBlock) return content;
		if (content.indexOf(REASONING_CLOSE_TAG, latestBlock.end) !== -1) return content;
		return content.slice(0, latestBlock.start).trimEnd();
	};

	const extractActiveReasoningText = (content: string): string => {
		const latestBlock = getLatestReasoningOpenBlock(content);
		if (!latestBlock) return '';
		if (content.indexOf(REASONING_CLOSE_TAG, latestBlock.end) !== -1) return '';

		return normalizeLeakedFormatting(
			content
				.slice(latestBlock.end)
				.replace(REASONING_SUMMARY_REGEX, '')
				.replace(/<\/?[^>]+>/g, ' ')
				.replace(/&nbsp;/gi, ' ')
		)
			.replace(/\n{3,}/g, '\n\n')
			.trim();
	};

	const clearProcessStatusTimer = () => {
		if (processStatusTimer) {
			clearTimeout(processStatusTimer);
			processStatusTimer = null;
		}
	};

	const scheduleProcessStatusRefresh = (delayMs: number) => {
		if (delayMs <= 0 || processStatusTimer) return;
		processStatusTimer = setTimeout(() => {
			processStatusTimer = null;
			processStatusTick += 1;
		}, delayMs);
	};

	const getProcessTimingMapForMessage = (id: string): Map<string, ProcessToolVisualTiming> => {
		if (!id) return new Map();
		let timingMap = processToolVisualTimingByKeyByMessageId.get(id);
		if (!timingMap) {
			timingMap = new Map<string, ProcessToolVisualTiming>();
			processToolVisualTimingByKeyByMessageId.set(id, timingMap);
		}
		return timingMap;
	};

	const getEffectiveProcessToolCallAttrs = (
		item: ProcessToolCallItem | undefined
	): Record<string, string> | null => {
		if (!item) return null;

		const hasArtifacts = getToolCallArtifactEvidence(item.attrs);
		const rawStatus = resolveToolCallStatus(item.attrs, {
			promoteArtifactRunning: hasArtifacts
		});
		const now = Date.now();
		const timingMap = getProcessTimingMapForMessage(message?.id);
		let timing = timingMap.get(item.key);
		if (!timing) {
			timing = { firstSeenAt: now };
			timingMap.set(item.key, timing);
		}

		if (rawStatus === 'running') {
			clearProcessStatusTimer();
			return item.attrs;
		}

		const elapsedMs = now - timing.firstSeenAt;
		// Keep fast successful tools briefly visible as running so progress remains perceptible.
		if (rawStatus === 'success' && !hasArtifacts && elapsedMs < MIN_PROCESS_RUNNING_MS) {
			scheduleProcessStatusRefresh(MIN_PROCESS_RUNNING_MS - elapsedMs);
			return {
				...item.attrs,
				status: 'running',
				done: 'false'
			};
		}

		clearProcessStatusTimer();
		return item.attrs;
	};

	const computeParsedContent = (rawContent: string, output: unknown) => {
		const renderState = resolveAssistantRenderState(rawContent, output, generatedFiles);
		const mergedProcessToolCallItems = renderState.processToolCallItems;

		const timingMap = getProcessTimingMapForMessage(message?.id);
		const activeProcessKeys = new Set(mergedProcessToolCallItems.map((item) => item.key));
		for (const key of Array.from(timingMap.keys())) {
			if (!activeProcessKeys.has(key)) {
				timingMap.delete(key);
			}
		}

		processToolCallItems = mergedProcessToolCallItems.map((item) => ({
			...item,
			attrs: getEffectiveProcessToolCallAttrs(item) ?? item.attrs
		}));

		finalMessageContent = renderState.finalMessageContent;
		finalContentBeforeGeneratedFiles = renderState.finalContentBeforeGeneratedFiles;
		finalContentAfterGeneratedFiles = renderState.finalContentAfterGeneratedFiles;
		placeInlineGeneratedFiles = renderState.placeInlineGeneratedFiles;
		updateVisibleMessageAttachments(renderState);
	};

	$: {
		processStatusTick;
		const rawContent = message?.content ?? '';
		const outputKey = buildOutputSignature(message?.output);
		const filesKey = buildMessageFileSignature(message?.files);
		const embedsKey = buildMessageEmbedSignature(message?.embeds);
		if (
			rawContent !== parsedContentKey ||
			generatedFilesListKey !== parsedGeneratedFilesKey ||
			outputKey !== parsedOutputKey ||
			filesKey !== parsedFilesKey ||
			embedsKey !== parsedEmbedsKey ||
			processStatusTick !== parsedProcessStatusTick
		) {
			parsedContentKey = rawContent;
			parsedGeneratedFilesKey = generatedFilesListKey;
			parsedOutputKey = outputKey;
			parsedFilesKey = filesKey;
			parsedEmbedsKey = embedsKey;
			parsedProcessStatusTick = processStatusTick;
			computeParsedContent(rawContent, message?.output);
		}
	}

	$: {
		const nextToolCallStateKey = `${message?.id ?? ''}::${processToolCallItems
			.map((item) => `${item.key}:${getProcessToolCallStatus(item)}`)
			.join('|')}`;
		const messageKeyPrefix = `${message?.id ?? ''}::`;
		const isNewMessage = !toolCallSectionStateKey.startsWith(messageKeyPrefix);
		const hasRunningTool = processToolCallItems.some(
			(item) => getProcessToolCallStatus(item) === 'running'
		);

		if (nextToolCallStateKey !== toolCallSectionStateKey) {
			if (isNewMessage) {
				toolCallSectionOpen = hasRunningTool;
				toolCallSectionUserToggled = false;
			} else if (!toolCallSectionUserToggled && hasRunningTool) {
				toolCallSectionOpen = true;
			}
			toolCallSectionStateKey = nextToolCallStateKey;
		}
	}

	$: {
		const nextKey = `${message?.done ? '1' : '0'}::${message?.content ?? ''}`;
		if (nextKey !== parsedSkillDraftKey) {
			parsedSkillDraftKey = nextKey;
			parsedSkillDraft =
				message?.done && !message?.error
					? parseSkillDraftFromMessageContent(message.content ?? '')
					: null;
		}
	}

	$: {
		const nextKey = `${message?.done ? '1' : '0'}::${message?.content ?? ''}`;
		if (nextKey !== parsedToolDraftKey) {
			parsedToolDraftKey = nextKey;
			parsedToolDraft =
				message?.done && !message?.error
					? parseToolDraftFromMessageContent(message.content ?? '')
					: null;
		}
	}

	$: {
		const nextKey = parsedSkillDraft ? `${message?.id ?? ''}::${parsedSkillDraft.id}` : '';
		if (!nextKey) {
			editableSkillDraftKey = '';
			editableSkillDraft = null;
		} else if (nextKey !== editableSkillDraftKey) {
			editableSkillDraftKey = nextKey;
			editableSkillDraft = cloneSkillDraftForEditing(parsedSkillDraft);
		}
	}

	$: {
		const nextKey = parsedToolDraft ? `${message?.id ?? ''}::${parsedToolDraft.id}` : '';
		if (!nextKey) {
			editableToolDraftKey = '';
			editableToolDraft = null;
		} else if (nextKey !== editableToolDraftKey) {
			editableToolDraftKey = nextKey;
			editableToolDraft = cloneToolDraftForEditing(parsedToolDraft);
		}
	}

	$: canSharePublicSkill = $user?.role === 'admin' || !!$user?.permissions?.sharing?.public_skills;
	$: canSharePublicTool = $user?.role === 'admin' || !!$user?.permissions?.sharing?.public_tools;

	$: if (
		editableSkillDraft &&
		!canSharePublicSkill &&
		editableSkillDraft.meta.visibility === 'public'
	) {
		editableSkillDraft = {
			...editableSkillDraft,
			meta: {
				...editableSkillDraft.meta,
				visibility: 'restricted'
			}
		};
	}

	$: if (editableSkillDraft && $user?.role !== 'admin' && editableSkillDraft.meta.is_default) {
		editableSkillDraft = {
			...editableSkillDraft,
			meta: {
				...editableSkillDraft.meta,
				is_default: false
			}
		};
	}

	$: if (
		editableToolDraft &&
		!canSharePublicTool &&
		editableToolDraft.meta.visibility === 'public'
	) {
		editableToolDraft = {
			...editableToolDraft,
			meta: {
				...editableToolDraft.meta,
				visibility: 'restricted'
			}
		};
	}

	$: if (editableToolDraft && $user?.role !== 'admin' && editableToolDraft.meta.is_default) {
		editableToolDraft = {
			...editableToolDraft,
			meta: {
				...editableToolDraft.meta,
				is_default: false
			}
		};
	}

	const copyToClipboard = async (text) => {
		text = removeDetails(text, ['tool_calls']);
		text = removeAllDetails(text);
		text = text.replace(/\n{3,}/g, '\n\n').trim();

		if (($config?.ui?.response_watermark ?? '').trim() !== '') {
			text = `${text}\n\n${$config?.ui?.response_watermark}`;
		}

		const res = await _copyToClipboard(text, null, $settings?.copyFormatted ?? false);
		if (res) {
			toast.success($i18n.t('Copying to clipboard was successful!'));
		}
	};

	const createSkillDraftHandler = async () => {
		if (!editableSkillDraft || creatingSkillDraft) {
			return;
		}

		const draft = {
			...editableSkillDraft,
			id: editableSkillDraft.id.trim(),
			name: editableSkillDraft.name.trim(),
			description: editableSkillDraft.description.trim(),
			content: editableSkillDraft.content,
			meta: {
				...editableSkillDraft.meta,
				category: editableSkillDraft.meta.category.trim(),
				dependencies: Array.isArray(editableSkillDraft.meta.dependencies)
					? editableSkillDraft.meta.dependencies.map((value) => `${value}`.trim()).filter(Boolean)
					: []
			}
		};

		if (!draft.id || !draft.name || !draft.content.trim()) {
			toast.error($i18n.t('Skill ID, name, and content are required'));
			return;
		}

		creatingSkillDraft = true;

		const createdSkill = await createNewSkill(localStorage.token, {
			id: draft.id,
			name: draft.name,
			description: draft.description,
			meta: draft.meta,
			content: draft.content,
			access_grants: draft.access_grants
		}).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		creatingSkillDraft = false;

		if (!createdSkill) {
			return;
		}

		toast.success($i18n.t('Skill created successfully'));
		showCreateSkillConfirm = false;
		await goto(`/workspace/skills/edit?id=${encodeURIComponent(createdSkill.id)}`);
	};

	const createToolDraftHandler = async () => {
		if (!editableToolDraft || creatingToolDraft) {
			return;
		}

		const draft = {
			...editableToolDraft,
			id: editableToolDraft.id.trim(),
			name: editableToolDraft.name.trim(),
			content: editableToolDraft.content,
			meta: {
				...editableToolDraft.meta,
				description: editableToolDraft.meta.description.trim(),
				category: editableToolDraft.meta.category.trim(),
				dependencies: Array.isArray(editableToolDraft.meta.dependencies)
					? editableToolDraft.meta.dependencies.map((value) => `${value}`.trim()).filter(Boolean)
					: []
			}
		};

		if (!draft.id || !draft.name || !draft.content.trim()) {
			toast.error($i18n.t('Tool ID, name, and code are required'));
			return;
		}

		const requiredVersion = getToolVersionRequirement(draft.content, WEBUI_VERSION);
		if (requiredVersion) {
			toast.error(
				$i18n.t(
					'Application version (v{{OPEN_WEBUI_VERSION}}) is lower than required version (v{{REQUIRED_VERSION}})',
					{
						OPEN_WEBUI_VERSION: WEBUI_VERSION,
						REQUIRED_VERSION: requiredVersion
					}
				)
			);
			return;
		}

		creatingToolDraft = true;

		const createdTool = await createNewTool(localStorage.token, {
			id: draft.id,
			name: draft.name,
			meta: draft.meta,
			content: draft.content,
			access_grants: draft.access_grants
		}).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		creatingToolDraft = false;

		if (!createdTool) {
			return;
		}

		toast.success($i18n.t('Tool created successfully'));
		showCreateToolConfirm = false;
	};

	const stopAudio = () => {
		try {
			speechSynthesis.cancel();
			$audioQueue.stop();
		} catch {}

		if (speaking) {
			speaking = false;
			speakingIdx = undefined;
		}
	};

	const speak = async () => {
		if (!(message?.content ?? '').trim().length) {
			toast.info($i18n.t('No content to speak'));
			return;
		}

		speaking = true;
		const content = removeAllDetails(message.content);

		// Get voice: model-specific > user settings > config default
		const getVoiceId = () => {
			// Check for model-specific TTS voice first
			if (model?.info?.meta?.tts?.voice) {
				return model.info.meta.tts.voice;
			}
			// Fall back to user settings or config default
			if ($settings?.audio?.tts?.defaultVoice === $config.audio.tts.voice) {
				return $settings?.audio?.tts?.voice ?? $config?.audio?.tts?.voice;
			}
			return $config?.audio?.tts?.voice;
		};

		if ($config.audio.tts.engine === '') {
			let voices = [];
			const getVoicesLoop = setInterval(() => {
				voices = speechSynthesis.getVoices();
				if (voices.length > 0) {
					clearInterval(getVoicesLoop);

					const voiceId = getVoiceId();
					const voice = voices?.filter((v) => v.voiceURI === voiceId)?.at(0) ?? undefined;

					console.log(voice);

					const speech = new SpeechSynthesisUtterance(content);
					speech.rate = $settings.audio?.tts?.playbackRate ?? 1;

					console.log(speech);

					speech.onend = () => {
						speaking = false;
						if ($settings.conversationMode) {
							document.getElementById('voice-input-button')?.click();
						}
					};

					if (voice) {
						speech.voice = voice;
					}

					speechSynthesis.speak(speech);
				}
			}, 100);
		} else {
			$audioQueue.setId(`${message.id}`);
			$audioQueue.setPlaybackRate($settings.audio?.tts?.playbackRate ?? 1);
			$audioQueue.onStopped = () => {
				speaking = false;
				speakingIdx = undefined;
			};

			loadingSpeech = true;
			const messageContentParts: string[] = getMessageContentParts(
				content,
				$config?.audio?.tts?.split_on ?? 'punctuation'
			);

			if (!messageContentParts.length) {
				console.log('No content to speak');
				toast.info($i18n.t('No content to speak'));

				speaking = false;
				loadingSpeech = false;
				return;
			}

			const voiceId = getVoiceId();
			console.debug('Prepared message content for TTS', messageContentParts, 'voice:', voiceId);

			if ($settings.audio?.tts?.engine === 'browser-kokoro') {
				if (!$TTSWorker) {
					await TTSWorker.set(
						new KokoroWorker({
							dtype: $settings.audio?.tts?.engineConfig?.dtype ?? 'fp32'
						})
					);

					await $TTSWorker.init();
				}

				for (const [idx, sentence] of messageContentParts.entries()) {
					const url = await $TTSWorker
						.generate({
							text: sentence,
							voice: voiceId
						})
						.catch((error) => {
							console.error(error);
							toast.error(`${error}`);

							speaking = false;
							loadingSpeech = false;
						});

					if (url && speaking) {
						$audioQueue.enqueue(url);
						loadingSpeech = false;
					}
				}
			} else {
				for (const [idx, sentence] of messageContentParts.entries()) {
					const res = await synthesizeOpenAISpeech(localStorage.token, voiceId, sentence).catch(
						(error) => {
							console.error(error);
							toast.error(`${error}`);

							speaking = false;
							loadingSpeech = false;
						}
					);

					if (res && speaking) {
						const blob = await res.blob();
						const url = URL.createObjectURL(blob);

						$audioQueue.enqueue(url);
						loadingSpeech = false;
					}
				}
			}
		}
	};

	let preprocessedDetailsCache = [];

	function preprocessForEditing(content: string): string {
		// Replace <details>...</details> with unique ID placeholder
		const detailsBlocks = [];
		let i = 0;

		content = content.replace(/<details[\s\S]*?<\/details>/gi, (match) => {
			detailsBlocks.push(match);
			return `<details id="__DETAIL_${i++}__"/>`;
		});

		// Store original blocks in the editedContent or globally (see merging later)
		preprocessedDetailsCache = detailsBlocks;

		return content;
	}

	function postprocessAfterEditing(content: string): string {
		const restoredContent = content.replace(
			/<details id="__DETAIL_(\d+)__"\/>/g,
			(_, index) => preprocessedDetailsCache[parseInt(index)] || ''
		);

		return restoredContent;
	}

	const editMessageHandler = async () => {
		edit = true;

		editedContent = preprocessForEditing(message.content);

		await tick();

		const messagesContainer = document.getElementById('messages-container');
		const savedScrollTop = messagesContainer?.scrollTop;

		editTextAreaElement.style.height = '';
		editTextAreaElement.style.height = `${editTextAreaElement.scrollHeight}px`;

		if (messagesContainer) messagesContainer.scrollTop = savedScrollTop;
	};

	const editMessageConfirmHandler = async () => {
		const messageContent = postprocessAfterEditing(editedContent ? editedContent : '');
		editMessage(message.id, { content: messageContent }, false);

		edit = false;
		editedContent = '';

		await tick();
	};

	const saveAsCopyHandler = async () => {
		const messageContent = postprocessAfterEditing(editedContent ? editedContent : '');

		editMessage(message.id, { content: messageContent });

		edit = false;
		editedContent = '';

		await tick();
	};

	const cancelEditMessage = async () => {
		edit = false;
		editedContent = '';
		await tick();
	};

	let feedbackLoading = false;

	const feedbackHandler = async (rating: number | null = null, details: object | null = null) => {
		feedbackLoading = true;
		console.log('Feedback', rating, details);

		const updatedMessage = {
			...message,
			annotation: {
				...(message?.annotation ?? {}),
				...(rating !== null ? { rating: rating } : {}),
				...(details ? details : {})
			}
		};

		const chat = await getChatById(localStorage.token, chatId).catch((error) => {
			toast.error(`${error}`);
		});
		if (!chat) {
			return;
		}

		const messages = createMessagesList(history, message.id);

		let feedbackItem = {
			type: 'rating',
			data: {
				...(updatedMessage?.annotation ? updatedMessage.annotation : {}),
				model_id: message?.selectedModelId ?? message.model,
				...(history.messages[message.parentId].childrenIds.length > 1
					? {
							sibling_model_ids: history.messages[message.parentId].childrenIds
								.filter((id) => id !== message.id)
								.map((id) => history.messages[id]?.selectedModelId ?? history.messages[id].model)
						}
					: {})
			},
			meta: {
				arena: message ? message.arena : false,
				model_id: message.model,
				message_id: message.id,
				message_index: messages.length,
				chat_id: chatId
			},
			snapshot: {
				chat: chat
			}
		};

		const baseModels = [
			feedbackItem.data.model_id,
			...(feedbackItem.data.sibling_model_ids ?? [])
		].reduce((acc, modelId) => {
			const model = $models.find((m) => m.id === modelId);
			if (model) {
				acc[model.id] = model?.info?.base_model_id ?? null;
			} else {
				// Log or handle cases where corresponding model is not found
				console.warn(`Model with ID ${modelId} not found`);
			}
			return acc;
		}, {});
		feedbackItem.meta.base_models = baseModels;

		let feedback = null;
		if (message?.feedbackId) {
			feedback = await updateFeedbackById(
				localStorage.token,
				message.feedbackId,
				feedbackItem
			).catch((error) => {
				toast.error(`${error}`);
			});
		} else {
			feedback = await createNewFeedback(localStorage.token, feedbackItem).catch((error) => {
				toast.error(`${error}`);
			});

			if (feedback) {
				updatedMessage.feedbackId = feedback.id;
			}
		}

		console.log(updatedMessage);
		saveMessage(message.id, updatedMessage);

		await tick();

		if (!details) {
			showRateComment = true;

			if (!updatedMessage.annotation?.tags && (message?.content ?? '') !== '') {
				// attempt to generate tags
				const tags = await generateTags(localStorage.token, message.model, messages, chatId).catch(
					(error) => {
						console.error(error);
						return [];
					}
				);
				console.log(tags);

				if (tags) {
					updatedMessage.annotation.tags = tags;
					feedbackItem.data.tags = tags;

					saveMessage(message.id, updatedMessage);
					await updateFeedbackById(
						localStorage.token,
						updatedMessage.feedbackId,
						feedbackItem
					).catch((error) => {
						toast.error(`${error}`);
					});
				}
			}
		}

		feedbackLoading = false;
	};

	const deleteMessageHandler = async () => {
		deleteMessage(message.id);
	};

	$: if (!edit) {
		(async () => {
			await tick();
		})();
	}

	const buttonsWheelHandler = (event: WheelEvent) => {
		if (buttonsContainerElement) {
			if (buttonsContainerElement.scrollWidth <= buttonsContainerElement.clientWidth) {
				// If the container is not scrollable, horizontal scroll
				return;
			} else {
				event.preventDefault();

				if (event.deltaY !== 0) {
					// Adjust horizontal scroll position based on vertical scroll
					buttonsContainerElement.scrollLeft += event.deltaY;
				}
			}
		}
	};

	const contentCopyHandler = (e) => {
		if (contentContainerElement) {
			e.preventDefault();
			// Get the selected HTML
			const selection = window.getSelection();
			if (!selection || selection.rangeCount === 0) {
				return;
			}
			const range = selection.getRangeAt(0);
			const tempDiv = document.createElement('div');

			// Remove background, color, and font styles
			tempDiv.appendChild(range.cloneContents());

			// Exclude tool-call cards from copy result.
			tempDiv
				.querySelectorAll('[data-tool-call-container="true"], [data-tool-call-content="true"]')
				.forEach((el) => el.remove());

			tempDiv.querySelectorAll('table').forEach((table) => {
				table.style.borderCollapse = 'collapse';
				table.style.width = 'auto';
				table.style.tableLayout = 'auto';
			});

			tempDiv.querySelectorAll('th').forEach((th) => {
				th.style.whiteSpace = 'nowrap';
				th.style.padding = '4px 8px';
			});

			// Put cleaned HTML + plain text into clipboard
			e.clipboardData.setData('text/html', tempDiv.innerHTML);
			e.clipboardData.setData('text/plain', (tempDiv.innerText || '').trim());
		}
	};

	onMount(async () => {
		// console.log('ResponseMessage mounted');

		await tick();
		if (buttonsContainerElement) {
			buttonsContainerElement.addEventListener('wheel', buttonsWheelHandler);
		}

		if (contentContainerElement) {
			contentContainerElement.addEventListener('copy', contentCopyHandler);
		}
	});

	onDestroy(() => {
		clearProcessStatusTimer();
		if (message?.id) {
			processToolVisualTimingByKeyByMessageId.delete(message.id);
		}

		if (buttonsContainerElement) {
			buttonsContainerElement.removeEventListener('wheel', buttonsWheelHandler);
		}

		if (contentContainerElement) {
			contentContainerElement.removeEventListener('copy', contentCopyHandler);
		}
	});
</script>

<DeleteConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete message?')}
	on:confirm={() => {
		deleteMessageHandler();
	}}
/>

<ConfirmDialog
	bind:show={showCreateSkillConfirm}
	title={`${$i18n.t('Create')} ${$i18n.t('Skills')}`}
	confirmLabel={$i18n.t('Create')}
	cancelLabel={$i18n.t('Cancel')}
	onConfirm={createSkillDraftHandler}
>
	{#if editableSkillDraft}
		<div
			class="max-h-[60vh] space-y-4 overflow-y-auto pr-1 text-sm text-gray-600 dark:text-gray-300"
		>
			<p>
				This will create the skill in your workspace. You can keep chatting here and edit it later
				from Workspace.
			</p>

			<div class="grid gap-3 sm:grid-cols-2">
				<label
					class="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
				>
					<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
						{$i18n.t('Name')}
					</div>
					<input
						bind:value={editableSkillDraft.name}
						class="mt-1 w-full bg-transparent text-[13px] text-gray-900 outline-hidden dark:text-gray-100"
						type="text"
						placeholder={$i18n.t('Skill name')}
					/>
				</label>

				<label
					class="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
				>
					<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
						{$i18n.t('Skill ID')}
					</div>
					<input
						bind:value={editableSkillDraft.id}
						class="mt-1 w-full break-all bg-transparent font-mono text-[13px] text-gray-900 outline-hidden dark:text-gray-100"
						type="text"
						placeholder={$i18n.t('Skill ID')}
						autocapitalize="off"
						autocorrect="off"
						spellcheck="false"
					/>
				</label>

				<label
					class="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
				>
					<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
						{$i18n.t('Visibility')}
					</div>
					<select
						bind:value={editableSkillDraft.meta.visibility}
						class="mt-1 w-full rounded-lg border border-gray-200 bg-white px-2 py-1 text-[13px] font-medium text-gray-900 outline-hidden dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
					>
						{#if canSharePublicSkill || editableSkillDraft.meta.visibility === 'public'}
							<option value="public">{$i18n.t('Public')}</option>
						{/if}
						<option value="restricted">{$i18n.t('Restricted')}</option>
						<option value="hidden">{$i18n.t('Private')}</option>
					</select>
				</label>

				<label
					class="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
				>
					<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
						{$i18n.t('Category')}
					</div>
					<input
						bind:value={editableSkillDraft.meta.category}
						class="mt-1 w-full bg-transparent text-[13px] text-gray-900 outline-hidden dark:text-gray-100"
						type="text"
						placeholder={$i18n.t('Category')}
					/>
				</label>
			</div>

			<label class="block">
				<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
					{$i18n.t('Description')}
				</div>
				<textarea
					bind:value={editableSkillDraft.description}
					class="mt-1 w-full resize-y rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 text-[13px] text-gray-700 outline-hidden dark:border-gray-800 dark:bg-gray-900/80 dark:text-gray-200"
					rows="3"
					placeholder={$i18n.t('Description')}
				></textarea>
			</label>

			<label class="block">
				<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
					{$i18n.t('Dependencies')}
				</div>
				<input
					class="mt-1 w-full rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 text-[13px] text-gray-700 outline-hidden dark:border-gray-800 dark:bg-gray-900/80 dark:text-gray-200"
					type="text"
					value={(editableSkillDraft.meta.dependencies ?? []).join(', ')}
					placeholder={$i18n.t('Comma-separated skill dependencies')}
					on:input={(event) => {
						editableSkillDraft.meta.dependencies = event.currentTarget.value
							.split(',')
							.map((value) => value.trim())
							.filter(Boolean);
						editableSkillDraft = { ...editableSkillDraft };
					}}
				/>
			</label>

			<div
				class="flex flex-wrap items-center gap-4 rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
			>
				<label class="flex items-center gap-2">
					<Switch bind:state={editableSkillDraft.meta.published} />
					<span>{$i18n.t('Published')}</span>
				</label>

				{#if $user?.role === 'admin'}
					<label class="flex items-center gap-2">
						<Switch bind:state={editableSkillDraft.meta.is_default} />
						<span>{$i18n.t('Default')}</span>
					</label>
				{/if}
			</div>

			<div
				class="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
			>
				<div class="mb-3 flex items-center justify-between gap-3">
					<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
						{$i18n.t('Permissions')}
					</div>
					<div class="text-xs text-gray-500 dark:text-gray-400">
						{editableSkillDraft.access_grants?.length ?? 0}
					</div>
				</div>

				<AccessControl
					bind:accessGrants={editableSkillDraft.access_grants}
					accessRoles={['read', 'write']}
					share={$user?.permissions?.sharing?.skills || $user?.role === 'admin'}
					sharePublic={canSharePublicSkill}
					shareUsers={($user?.permissions?.access_grants?.allow_users ?? true) ||
						$user?.role === 'admin'}
				/>
			</div>

			<label class="block">
				<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
					Content
				</div>
				<textarea
					bind:value={editableSkillDraft.content}
					class="mt-1 min-h-72 w-full resize-y rounded-2xl border border-gray-200 bg-gray-950 px-4 py-3 font-mono text-[12px] leading-5 text-gray-100 outline-hidden dark:border-gray-800"
					rows="16"
					spellcheck="false"
				></textarea>
			</label>
		</div>
	{/if}
</ConfirmDialog>

<ConfirmDialog
	bind:show={showCreateToolConfirm}
	title={`${$i18n.t('Create')} ${$i18n.t('Tools')}`}
	confirmLabel={$i18n.t('Create')}
	cancelLabel={$i18n.t('Cancel')}
	onConfirm={createToolDraftHandler}
>
	{#if editableToolDraft}
		<div
			class="max-h-[60vh] space-y-4 overflow-y-auto pr-1 text-sm text-gray-600 dark:text-gray-300"
		>
			<p>
				{$i18n.t(
					'This will create the tool in your workspace. You can keep chatting here and edit it later from Workspace.'
				)}
			</p>

			<div class="grid gap-3 sm:grid-cols-2">
				<label
					class="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
				>
					<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
						{$i18n.t('Name')}
					</div>
					<input
						bind:value={editableToolDraft.name}
						class="mt-1 w-full bg-transparent text-[13px] text-gray-900 outline-hidden dark:text-gray-100"
						type="text"
						placeholder={$i18n.t('Tool name')}
					/>
				</label>

				<label
					class="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
				>
					<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
						{$i18n.t('Tool ID')}
					</div>
					<input
						bind:value={editableToolDraft.id}
						class="mt-1 w-full break-all bg-transparent font-mono text-[13px] text-gray-900 outline-hidden dark:text-gray-100"
						type="text"
						placeholder={$i18n.t('Tool ID')}
						autocapitalize="off"
						autocorrect="off"
						spellcheck="false"
					/>
				</label>

				<label
					class="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
				>
					<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
						{$i18n.t('Visibility')}
					</div>
					<select
						bind:value={editableToolDraft.meta.visibility}
						class="mt-1 w-full rounded-lg border border-gray-200 bg-white px-2 py-1 text-[13px] font-medium text-gray-900 outline-hidden dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
					>
						{#if canSharePublicTool || editableToolDraft.meta.visibility === 'public'}
							<option value="public">{$i18n.t('Public')}</option>
						{/if}
						<option value="restricted">{$i18n.t('Restricted')}</option>
						<option value="hidden">{$i18n.t('Private')}</option>
					</select>
				</label>

				<label
					class="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
				>
					<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
						{$i18n.t('Category')}
					</div>
					<input
						bind:value={editableToolDraft.meta.category}
						class="mt-1 w-full bg-transparent text-[13px] text-gray-900 outline-hidden dark:text-gray-100"
						type="text"
						placeholder={$i18n.t('Category')}
					/>
				</label>
			</div>

			<label class="block">
				<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
					{$i18n.t('Description')}
				</div>
				<textarea
					bind:value={editableToolDraft.meta.description}
					class="mt-1 w-full resize-y rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 text-[13px] text-gray-700 outline-hidden dark:border-gray-800 dark:bg-gray-900/80 dark:text-gray-200"
					rows="3"
					placeholder={$i18n.t('Description')}
				></textarea>
			</label>

			<label class="block">
				<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
					{$i18n.t('Dependencies')}
				</div>
				<input
					class="mt-1 w-full rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 text-[13px] text-gray-700 outline-hidden dark:border-gray-800 dark:bg-gray-900/80 dark:text-gray-200"
					type="text"
					value={(editableToolDraft.meta.dependencies ?? []).join(', ')}
					placeholder={$i18n.t('Comma-separated tool dependencies')}
					on:input={(event) => {
						editableToolDraft.meta.dependencies = event.currentTarget.value
							.split(',')
							.map((value) => value.trim())
							.filter(Boolean);
						editableToolDraft = { ...editableToolDraft };
					}}
				/>
			</label>

			<div
				class="flex flex-wrap items-center gap-4 rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
			>
				<label class="flex items-center gap-2">
					<Switch bind:state={editableToolDraft.meta.published} />
					<span>{$i18n.t('Published')}</span>
				</label>

				{#if $user?.role === 'admin'}
					<label class="flex items-center gap-2">
						<Switch bind:state={editableToolDraft.meta.is_default} />
						<span>{$i18n.t('Default')}</span>
					</label>
				{/if}
			</div>

			<div
				class="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900/80"
			>
				<div class="mb-3 flex items-center justify-between gap-3">
					<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
						{$i18n.t('Permissions')}
					</div>
					<div class="text-xs text-gray-500 dark:text-gray-400">
						{editableToolDraft.access_grants?.length ?? 0}
					</div>
				</div>

				<AccessControl
					bind:accessGrants={editableToolDraft.access_grants}
					accessRoles={['read', 'write']}
					share={$user?.permissions?.sharing?.tools || $user?.role === 'admin'}
					sharePublic={canSharePublicTool}
					shareUsers={($user?.permissions?.access_grants?.allow_users ?? true) ||
						$user?.role === 'admin'}
				/>
			</div>

			<label class="block">
				<div class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
					{$i18n.t('Code')}
				</div>
				<textarea
					bind:value={editableToolDraft.content}
					class="mt-1 min-h-72 w-full resize-y rounded-2xl border border-gray-200 bg-gray-950 px-4 py-3 font-mono text-[12px] leading-5 text-gray-100 outline-hidden dark:border-gray-800"
					rows="16"
					spellcheck="false"
				></textarea>
			</label>
		</div>
	{/if}
</ConfirmDialog>

{#key message.id}
	<div
		class=" flex w-full message-{message.id}"
		id="message-{message.id}"
		dir={$settings.chatDirection}
		style="scroll-margin-top: 3rem;"
	>
		<div class={`shrink-0 ltr:mr-3 rtl:ml-3 hidden @lg:flex mt-1 `}>
			<ProfileImage
				src={`${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model?.id}&lang=${$i18n.language}`}
				className={'size-8 assistant-message-profile-image'}
			/>
		</div>

		<div class="flex-auto w-0 pl-1 relative">
			<Name>
				<Tooltip content={model?.name ?? message.model} placement="top-start">
					<span id="response-message-model-name" class="line-clamp-1 text-black dark:text-white">
						{model?.name ?? message.model}
					</span>
				</Tooltip>

				{#if message.timestamp}
					<div
						class="self-center text-xs font-medium first-letter:capitalize ml-0.5 translate-y-[1px] {($settings?.highContrastMode ??
						false)
							? 'dark:text-gray-100 text-gray-900'
							: 'invisible group-hover:visible transition text-gray-400'}"
					>
						<Tooltip content={dayjs(message.timestamp * 1000).format('LLLL')}>
							<span class="line-clamp-1"
								>{$i18n.t(formatDate(message.timestamp * 1000), {
									LOCALIZED_TIME: dayjs(message.timestamp * 1000).format('LT'),
									LOCALIZED_DATE: dayjs(message.timestamp * 1000).format('L')
								})}</span
							>
						</Tooltip>
					</div>
				{/if}
			</Name>

			<div>
				<div class="chat-{message.role} w-full min-w-full chat-markdown-prose">
					<div>
						{#if hasVisibleStatusHistory}
							<StatusHistory statusHistory={normalizedStatusHistory} />
						{/if}

						{#if visibleMessageFiles.length > 0}
							<div
								class="my-1 w-full flex overflow-x-auto gap-2 flex-wrap"
								dir={$settings?.chatDirection ?? 'auto'}
							>
								{#each visibleMessageFiles as file}
									<div>
										{#if file.type === 'image' || (file?.content_type ?? '').startsWith('image/')}
											<Image src={file.url} alt={message.content} />
										{:else}
											<FileItem
												item={file}
												url={file.url}
												name={file.name}
												type={file.type}
												size={file?.size}
												small={true}
											/>
										{/if}
									</div>
								{/each}
							</div>
						{/if}

						{#if visibleMessageEmbeds.length > 0}
							<div
								class="my-1 w-full flex overflow-x-auto gap-2 flex-wrap"
								id={`${message.id}-embeds-container`}
							>
								{#each visibleMessageEmbeds as embed, idx}
									<div class="my-2 w-full" id={`${message.id}-embeds-${idx}`}>
										<FullHeightIframe
											src={embed}
											allowScripts={true}
											allowForms={true}
											allowSameOrigin={$settings?.iframeSandboxAllowSameOrigin ?? false}
											allowPopups={true}
										/>
									</div>
								{/each}
							</div>
						{/if}

						{#if edit === true}
							<div class="w-full bg-gray-50 dark:bg-gray-800 rounded-3xl px-5 py-3 my-2">
								<textarea
									id="message-edit-{message.id}"
									bind:this={editTextAreaElement}
									class=" bg-transparent outline-hidden w-full resize-none"
									bind:value={editedContent}
									on:input={(e) => {
										const messagesContainer = document.getElementById('messages-container');
										const savedScrollTop = messagesContainer?.scrollTop;

										e.target.style.height = '';
										e.target.style.height = `${e.target.scrollHeight}px`;

										if (messagesContainer) messagesContainer.scrollTop = savedScrollTop;
									}}
									on:keydown={(e) => {
										if (e.key === 'Escape') {
											document.getElementById('close-edit-message-button')?.click();
										}

										const isCmdOrCtrlPressed = e.metaKey || e.ctrlKey;
										const isEnterPressed = e.key === 'Enter';

										if (isCmdOrCtrlPressed && isEnterPressed) {
											document.getElementById('confirm-edit-message-button')?.click();
										}
									}}
								/>

								<div class=" mt-2 mb-1 flex justify-between text-sm font-medium">
									<div>
										<button
											id="save-new-message-button"
											class="px-3.5 py-1.5 bg-gray-50 hover:bg-gray-100 dark:bg-gray-800 dark:hover:bg-gray-700 border border-gray-100 dark:border-gray-700 text-gray-700 dark:text-gray-200 transition rounded-3xl"
											on:click={() => {
												saveAsCopyHandler();
											}}
										>
											{$i18n.t('Save As Copy')}
										</button>
									</div>

									<div class="flex space-x-1.5">
										<button
											id="close-edit-message-button"
											class="px-3.5 py-1.5 bg-white dark:bg-gray-900 hover:bg-gray-100 text-gray-800 dark:text-gray-100 transition rounded-3xl"
											on:click={() => {
												cancelEditMessage();
											}}
										>
											{$i18n.t('Cancel')}
										</button>

										<button
											id="confirm-edit-message-button"
											class="px-3.5 py-1.5 bg-gray-900 dark:bg-white hover:bg-gray-850 text-gray-100 dark:text-gray-800 transition rounded-3xl"
											on:click={() => {
												editMessageConfirmHandler();
											}}
										>
											{$i18n.t('Save')}
										</button>
									</div>
								</div>
							</div>
						{/if}

						<div
							bind:this={contentContainerElement}
							class="w-full flex flex-col relative {edit ? 'hidden' : ''}"
							id="response-content-container"
						>
							{#if processToolCallItems.length > 0}
								{@const toolCallSectionStatus = getProcessToolCallSectionStatus(
									processToolCallItems
								)}
								<div class="mb-3 w-full overflow-hidden rounded-2xl border border-gray-200/90 bg-gray-50/85 shadow-xs dark:border-gray-800 dark:bg-gray-900/80">
									<button
										type="button"
										class={`flex w-full items-center justify-between px-3 py-2 text-left transition hover:bg-gray-100/80 dark:hover:bg-gray-900 ${
											toolCallSectionOpen
												? 'border-b border-gray-200/90 dark:border-gray-800'
												: ''
										}`}
										on:click={() => {
											toolCallSectionUserToggled = true;
											toolCallSectionOpen = !toolCallSectionOpen;
										}}
									>
										<div class="min-w-0 flex items-center gap-2.5">
											<div
												class="flex size-7 shrink-0 items-center justify-center rounded-full bg-gray-200/70 text-gray-600 dark:bg-gray-800 dark:text-gray-200"
											>
												<WrenchSolid className="size-3.5" />
											</div>
											<div class="min-w-0">
												<div class="text-sm font-medium text-gray-800 dark:text-gray-100">
													工具调用
												</div>
												<div class="line-clamp-1 text-xs text-gray-500 dark:text-gray-400">
													{getProcessToolCallSectionSummary(processToolCallItems)}
												</div>
											</div>
										</div>

										<div class="ml-2 flex shrink-0 items-center gap-2">
											<div
												class={`rounded-full border px-1.5 py-0.5 text-[10px] font-medium ${getProcessToolCallSectionBadgeClass(
													toolCallSectionStatus
												)}`}
											>
												{getProcessToolCallSectionBadgeLabel(toolCallSectionStatus)}
											</div>
											{#if toolCallSectionStatus === 'running'}
												<Spinner className="size-3.5 text-blue-600 dark:text-blue-400" />
											{/if}
											<div class="text-gray-500 dark:text-gray-400">
												{#if toolCallSectionOpen}
													<ChevronUp className="size-3.5" strokeWidth="3" />
												{:else}
													<ChevronDown className="size-3.5" strokeWidth="3" />
												{/if}
											</div>
										</div>
									</button>

									{#if toolCallSectionOpen}
										<div class="relative px-3 py-3">
											<div
												class="pointer-events-none absolute bottom-4 left-[1.05rem] top-4 w-px bg-gray-200 dark:bg-gray-800"
											></div>
											<div class="space-y-2.5">
												{#each processToolCallItems as item (item.key)}
													{@const itemStatus = getProcessToolCallStatus(item)}
													<div class="relative pl-6">
														<div
															class={`absolute left-0 top-4 size-2.5 rounded-full ring-4 ${getProcessToolCallTimelineDotClass(
																itemStatus
															)}`}
														></div>
														<ToolCallDisplay
															id={`${chatId}-${message.id}-${item.key}`}
															attributes={item.attrs}
															disableVisualStatusDelay={true}
															embedded={true}
															className="w-full"
														/>
													</div>
												{/each}
											</div>
										</div>
									{/if}
								</div>
							{/if}

							{#if finalMessageContent === '' && processToolCallItems.length === 0 && visibleMessageFiles.length === 0 && visibleMessageEmbeds.length === 0 && !message.error && !hasVisibleStatusHistory && message.done !== true}
								<Skeleton />
							{:else if finalMessageContent && message.error !== true}
								<!-- always show message contents even if there's an error -->
								<!-- unless message.error === true which is legacy error handling, where the error message is stored in message.content -->
								{#if placeInlineGeneratedFiles}
									{#if finalContentBeforeGeneratedFiles}
										<ContentRenderer
											id={`${chatId}-${message.id}-before-generated-files`}
											messageId={message.id}
											{history}
											{selectedModels}
											content={finalContentBeforeGeneratedFiles}
											sources={message.sources}
											floatingButtons={false}
											save={!readOnly}
											preview={!readOnly}
											{editCodeBlock}
											{topPadding}
											done={($settings?.chatFadeStreamingText ?? true)
												? (message?.done ?? false)
												: true}
											{model}
											onTaskClick={async (e) => {
												console.log(e);
											}}
											onSourceClick={async (id) => {
												console.log(id);

												if (citationsElement) {
													citationsElement?.showSourceModal(id);
												}
											}}
											onAddMessages={({ modelId, parentId, messages }) => {
												addMessages({ modelId, parentId, messages });
											}}
											onSave={({ raw, oldContent, newContent }) => {
												history.messages[message.id].content = history.messages[
													message.id
												].content.replace(raw, raw.replace(oldContent, newContent));

												updateChat();
											}}
										/>
									{/if}
								{:else}
									<ContentRenderer
										id={`${chatId}-${message.id}`}
										messageId={message.id}
										{history}
										{selectedModels}
										content={finalMessageContent}
										sources={message.sources}
										floatingButtons={message?.done &&
											!readOnly &&
											($settings?.showFloatingActionButtons ?? true)}
										save={!readOnly}
										preview={!readOnly}
										{editCodeBlock}
										{topPadding}
										done={($settings?.chatFadeStreamingText ?? true)
											? (message?.done ?? false)
											: true}
										{model}
										onTaskClick={async (e) => {
											console.log(e);
										}}
										onSourceClick={async (id) => {
											console.log(id);

											if (citationsElement) {
												citationsElement?.showSourceModal(id);
											}
										}}
										onAddMessages={({ modelId, parentId, messages }) => {
											addMessages({ modelId, parentId, messages });
										}}
										onSave={({ raw, oldContent, newContent }) => {
											history.messages[message.id].content = history.messages[
												message.id
											].content.replace(raw, raw.replace(oldContent, newContent));

											updateChat();
										}}
									/>
								{/if}
							{/if}

							{#if message?.error}
								<Error content={message?.error?.content ?? message.content} />
							{/if}

							{#if displayGeneratedFiles.length > 0 && placeInlineGeneratedFiles}
								<div class="mt-3 space-y-2">
									{#each displayGeneratedFiles as file}
										{@const generatedFilePreviewOpen =
											$showFilePreview && $selectedGeneratedFilePreviewId === file.id}
										<div
											class={`group relative overflow-hidden rounded-2xl border transition-all duration-150 focus-within:ring-2 focus-within:ring-blue-500/20 ${getGeneratedFileContainerClass(
												generatedFilePreviewOpen
											)}`}
										>
											<button
												type="button"
												class="absolute inset-0 z-0 rounded-2xl focus-visible:outline-none"
												aria-label={`打开 ${file.name} 预览`}
												on:click={() => {
													openGeneratedFile(file);
												}}
											></button>

											<div class="pointer-events-none relative z-10 flex min-w-0 items-center gap-3 px-3 py-2.5 pr-14">
												{#if file.isImage && file.url}
													<img
														src={file.url}
														alt={file.name}
														class={`size-10 shrink-0 rounded-xl border object-cover shadow-sm ${
															generatedFilePreviewOpen
																? 'border-blue-200 dark:border-blue-800'
																: 'border-gray-200 dark:border-gray-700'
														}`}
													/>
												{:else}
													<div
														class={`flex size-10 shrink-0 items-center justify-center rounded-xl ${
															generatedFilePreviewOpen
																? 'bg-blue-100 text-blue-600 dark:bg-blue-950/70 dark:text-blue-300'
																: 'bg-gray-200/70 text-gray-600 dark:bg-gray-800 dark:text-gray-200'
														}`}
													>
														<Document className="size-4.5" />
													</div>
												{/if}

												<div class="min-w-0 flex-1">
													<div
														class={`line-clamp-1 text-sm font-medium ${
															generatedFilePreviewOpen
																? 'text-blue-900 dark:text-blue-100'
																: 'text-gray-800 dark:text-gray-100'
														}`}
													>
														{file.name}
													</div>
													<div
														class={`line-clamp-1 text-xs ${
															generatedFilePreviewOpen
																? 'text-blue-600 dark:text-blue-300'
																: 'text-gray-500 dark:text-gray-400'
														}`}
													>
														{getGeneratedFileSummary(file, generatedFilePreviewOpen)}
													</div>
												</div>

												<div class="shrink-0 text-gray-400 dark:text-gray-500">
													<ChevronRight
														className={`size-4 transition-transform duration-150 group-hover:translate-x-0.5 ${
															generatedFilePreviewOpen
																? 'text-blue-500 dark:text-blue-300'
																: ''
														}`}
														strokeWidth="3"
													/>
												</div>
											</div>

											<button
												type="button"
												class="absolute right-2 top-1/2 z-20 inline-flex size-8 -translate-y-1/2 items-center justify-center rounded-lg border border-transparent bg-white/80 text-gray-600 transition hover:border-gray-200 hover:bg-white hover:text-blue-600 dark:bg-gray-950/70 dark:text-gray-200 dark:hover:border-gray-700 dark:hover:bg-gray-900 dark:hover:text-blue-400"
												title={$i18n.t('Download')}
												on:click|stopPropagation={async () => {
													await downloadGeneratedFile(file);
												}}
											>
												<Download className="size-3.5" />
											</button>
										</div>
									{/each}
								</div>

								{#if finalContentAfterGeneratedFiles}
									<ContentRenderer
										id={`${chatId}-${message.id}-after-generated-files`}
										messageId={message.id}
										{history}
										{selectedModels}
										content={finalContentAfterGeneratedFiles}
										sources={message.sources}
										floatingButtons={message?.done &&
											!readOnly &&
											($settings?.showFloatingActionButtons ?? true)}
										save={!readOnly}
										preview={!readOnly}
										{editCodeBlock}
										{topPadding}
										done={($settings?.chatFadeStreamingText ?? true)
											? (message?.done ?? false)
											: true}
										{model}
										onTaskClick={async (e) => {
											console.log(e);
										}}
										onSourceClick={async (id) => {
											console.log(id);

											if (citationsElement) {
												citationsElement?.showSourceModal(id);
											}
										}}
										onAddMessages={({ modelId, parentId, messages }) => {
											addMessages({ modelId, parentId, messages });
										}}
										onSave={({ raw, oldContent, newContent }) => {
											history.messages[message.id].content = history.messages[
												message.id
											].content.replace(raw, raw.replace(oldContent, newContent));

											updateChat();
										}}
									/>
								{/if}
							{/if}

							{#if displayGeneratedFiles.length > 0 && !placeInlineGeneratedFiles}
								<div class="mt-3 space-y-2">
									{#each displayGeneratedFiles as file}
										{@const generatedFilePreviewOpen =
											$showFilePreview && $selectedGeneratedFilePreviewId === file.id}
										<div
											class={`group relative overflow-hidden rounded-2xl border transition-all duration-150 focus-within:ring-2 focus-within:ring-blue-500/20 ${getGeneratedFileContainerClass(
												generatedFilePreviewOpen
											)}`}
										>
											<button
												type="button"
												class="absolute inset-0 z-0 rounded-2xl focus-visible:outline-none"
												aria-label={`打开 ${file.name} 预览`}
												on:click={() => {
													openGeneratedFile(file);
												}}
											></button>

											<div class="pointer-events-none relative z-10 flex min-w-0 items-center gap-3 px-3 py-2.5 pr-14">
												{#if file.isImage && file.url}
													<img
														src={file.url}
														alt={file.name}
														class={`size-10 shrink-0 rounded-xl border object-cover shadow-sm ${
															generatedFilePreviewOpen
																? 'border-blue-200 dark:border-blue-800'
																: 'border-gray-200 dark:border-gray-700'
														}`}
													/>
												{:else}
													<div
														class={`flex size-10 shrink-0 items-center justify-center rounded-xl ${
															generatedFilePreviewOpen
																? 'bg-blue-100 text-blue-600 dark:bg-blue-950/70 dark:text-blue-300'
																: 'bg-gray-200/70 text-gray-600 dark:bg-gray-800 dark:text-gray-200'
														}`}
													>
														<Document className="size-4.5" />
													</div>
												{/if}

												<div class="min-w-0 flex-1">
													<div
														class={`line-clamp-1 text-sm font-medium ${
															generatedFilePreviewOpen
																? 'text-blue-900 dark:text-blue-100'
																: 'text-gray-800 dark:text-gray-100'
														}`}
													>
														{file.name}
													</div>
													<div
														class={`line-clamp-1 text-xs ${
															generatedFilePreviewOpen
																? 'text-blue-600 dark:text-blue-300'
																: 'text-gray-500 dark:text-gray-400'
														}`}
													>
														{getGeneratedFileSummary(file, generatedFilePreviewOpen)}
													</div>
												</div>

												<div class="shrink-0 text-gray-400 dark:text-gray-500">
													<ChevronRight
														className={`size-4 transition-transform duration-150 group-hover:translate-x-0.5 ${
															generatedFilePreviewOpen
																? 'text-blue-500 dark:text-blue-300'
																: ''
														}`}
														strokeWidth="3"
													/>
												</div>
											</div>

											<button
												type="button"
												class="absolute right-2 top-1/2 z-20 inline-flex size-8 -translate-y-1/2 items-center justify-center rounded-lg border border-transparent bg-white/80 text-gray-600 transition hover:border-gray-200 hover:bg-white hover:text-blue-600 dark:bg-gray-950/70 dark:text-gray-200 dark:hover:border-gray-700 dark:hover:bg-gray-900 dark:hover:text-blue-400"
												title={$i18n.t('Download')}
												on:click|stopPropagation={async () => {
													await downloadGeneratedFile(file);
												}}
											>
												<Download className="size-3.5" />
											</button>
										</div>
									{/each}
								</div>
							{/if}

							<SourceContextNotice metadata={retrievalMetadata} />

							{#if renderableSources.length > 0 && (model?.info?.meta?.capabilities?.citations ?? true)}
								<Citations
									bind:this={citationsElement}
									id={message?.id}
									{chatId}
									sources={renderableSources}
									{readOnly}
								/>
							{/if}

							{#if message.code_executions}
								<CodeExecutions codeExecutions={message.code_executions} />
							{/if}
						</div>
					</div>
				</div>

				{#if !edit}
					<div
						bind:this={buttonsContainerElement}
						class="flex justify-start overflow-x-auto buttons text-gray-600 dark:text-gray-500 mt-0.5"
					>
						{#if message.done || siblings.length > 1}
							{#if siblings.length > 1}
								<div class="flex self-center min-w-fit" dir="ltr">
									<button
										aria-label={$i18n.t('Previous message')}
										class="self-center p-1 hover:bg-black/5 dark:hover:bg-white/5 dark:hover:text-white hover:text-black rounded-md transition"
										on:click={() => {
											showPreviousMessage(message);
										}}
									>
										<svg
											aria-hidden="true"
											xmlns="http://www.w3.org/2000/svg"
											fill="none"
											viewBox="0 0 24 24"
											stroke="currentColor"
											stroke-width="2.5"
											class="size-3.5"
										>
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												d="M15.75 19.5 8.25 12l7.5-7.5"
											/>
										</svg>
									</button>

									{#if messageIndexEdit}
										<div
											class="text-sm flex justify-center font-semibold self-center dark:text-gray-100 min-w-fit"
										>
											<input
												id="message-index-input-{message.id}"
												type="number"
												value={siblings.indexOf(message.id) + 1}
												min="1"
												max={siblings.length}
												on:focus={(e) => {
													e.target.select();
												}}
												on:blur={(e) => {
													gotoMessage(message, e.target.value - 1);
													messageIndexEdit = false;
												}}
												on:keydown={(e) => {
													if (e.key === 'Enter') {
														gotoMessage(message, e.target.value - 1);
														messageIndexEdit = false;
													}
												}}
												class="bg-transparent font-semibold self-center dark:text-gray-100 min-w-fit outline-hidden"
											/>/{siblings.length}
										</div>
									{:else}
										<!-- svelte-ignore a11y-no-static-element-interactions -->
										<div
											class="text-sm tracking-widest font-semibold self-center dark:text-gray-100 min-w-fit"
											on:dblclick={async () => {
												messageIndexEdit = true;

												await tick();
												const input = document.getElementById(`message-index-input-${message.id}`);
												if (input) {
													input.focus();
													input.select();
												}
											}}
										>
											{siblings.indexOf(message.id) + 1}/{siblings.length}
										</div>
									{/if}

									<button
										class="self-center p-1 hover:bg-black/5 dark:hover:bg-white/5 dark:hover:text-white hover:text-black rounded-md transition"
										on:click={() => {
											showNextMessage(message);
										}}
										aria-label={$i18n.t('Next message')}
									>
										<svg
											xmlns="http://www.w3.org/2000/svg"
											fill="none"
											aria-hidden="true"
											viewBox="0 0 24 24"
											stroke="currentColor"
											stroke-width="2.5"
											class="size-3.5"
										>
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												d="m8.25 4.5 7.5 7.5-7.5 7.5"
											/>
										</svg>
									</button>
								</div>
							{/if}

							{#if message.done}
								{#if !readOnly}
									{#if $user?.role === 'user' ? ($user?.permissions?.chat?.edit ?? true) : true}
										<Tooltip content={$i18n.t('Edit')} placement="bottom">
											<button
												aria-label={$i18n.t('Edit')}
												class="{isLastMessage || ($settings?.highContrastMode ?? false)
													? 'visible'
													: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
												on:click={() => {
													editMessageHandler();
												}}
											>
												<svg
													xmlns="http://www.w3.org/2000/svg"
													fill="none"
													viewBox="0 0 24 24"
													stroke-width="2.3"
													aria-hidden="true"
													stroke="currentColor"
													class="w-4 h-4"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L6.832 19.82a4.5 4.5 0 01-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 011.13-1.897L16.863 4.487zm0 0L19.5 7.125"
													/>
												</svg>
											</button>
										</Tooltip>
									{/if}
								{/if}

								<Tooltip content={$i18n.t('Copy')} placement="bottom">
									<button
										aria-label={$i18n.t('Copy')}
										class="{isLastMessage || ($settings?.highContrastMode ?? false)
											? 'visible'
											: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition copy-response-button"
										on:click={() => {
											copyToClipboard(message.content);
										}}
									>
										<svg
											xmlns="http://www.w3.org/2000/svg"
											fill="none"
											aria-hidden="true"
											viewBox="0 0 24 24"
											stroke-width="2.3"
											stroke="currentColor"
											class="w-4 h-4"
										>
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												d="M15.666 3.888A2.25 2.25 0 0013.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 01-.75.75H9a.75.75 0 01-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 01-2.25 2.25H6.75A2.25 2.25 0 014.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 011.927-.184"
											/>
										</svg>
									</button>
								</Tooltip>

								{#if !readOnly && parsedToolDraft}
									<Tooltip content={`${$i18n.t('Create')} ${$i18n.t('Tools')}`} placement="bottom">
										<button
											type="button"
											aria-label={`${$i18n.t('Create')} ${$i18n.t('Tools')}`}
											class="visible inline-flex min-w-fit items-center gap-1.5 rounded-lg px-2.5 py-1.5 hover:bg-black/5 dark:hover:bg-white/5 dark:hover:text-white hover:text-black transition"
											on:click={() => {
												showCreateToolConfirm = true;
											}}
										>
											<svg
												xmlns="http://www.w3.org/2000/svg"
												fill="none"
												viewBox="0 0 24 24"
												stroke-width="2.2"
												stroke="currentColor"
												class="size-4"
												aria-hidden="true"
											>
												<path
													stroke-linecap="round"
													stroke-linejoin="round"
													d="M4.5 12h15m-7.5-7.5v15"
												/>
											</svg>
											<span class="text-xs font-medium">{$i18n.t('Create')}</span>
										</button>
									</Tooltip>
								{/if}

								{#if !readOnly && parsedSkillDraft}
									<Tooltip content={`${$i18n.t('Create')} ${$i18n.t('Skills')}`} placement="bottom">
										<button
											type="button"
											aria-label={`${$i18n.t('Create')} ${$i18n.t('Skills')}`}
											class="visible inline-flex min-w-fit items-center gap-1.5 rounded-lg px-2.5 py-1.5 hover:bg-black/5 dark:hover:bg-white/5 dark:hover:text-white hover:text-black transition"
											on:click={() => {
												showCreateSkillConfirm = true;
											}}
										>
											<svg
												xmlns="http://www.w3.org/2000/svg"
												fill="none"
												viewBox="0 0 24 24"
												stroke-width="2.2"
												stroke="currentColor"
												class="size-4"
												aria-hidden="true"
											>
												<path
													stroke-linecap="round"
													stroke-linejoin="round"
													d="M4.5 12h15m-7.5-7.5v15"
												/>
											</svg>
											<span class="text-xs font-medium">{$i18n.t('Create')}</span>
										</button>
									</Tooltip>
								{/if}

								{#if $user?.permissions?.chat?.tts ?? true}
									<Tooltip content={$i18n.t('Read Aloud')} placement="bottom">
										<button
											aria-label={$i18n.t('Read Aloud')}
											id="speak-button-{message.id}"
											class="{isLastMessage || ($settings?.highContrastMode ?? false)
												? 'visible'
												: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
											on:click={() => {
												if (!loadingSpeech) {
													if (speaking) {
														stopAudio();
													} else {
														speak();
													}
												}
											}}
										>
											{#if loadingSpeech}
												<svg
													class=" w-4 h-4"
													fill="currentColor"
													viewBox="0 0 24 24"
													aria-hidden="true"
													xmlns="http://www.w3.org/2000/svg"
												>
													<style>
														.spinner_S1WN {
															animation: spinner_MGfb 0.8s linear infinite;
															animation-delay: -0.8s;
														}

														.spinner_Km9P {
															animation-delay: -0.65s;
														}

														.spinner_JApP {
															animation-delay: -0.5s;
														}

														@keyframes spinner_MGfb {
															93.75%,
															100% {
																opacity: 0.2;
															}
														}
													</style>
													<circle class="spinner_S1WN" cx="4" cy="12" r="3" />
													<circle class="spinner_S1WN spinner_Km9P" cx="12" cy="12" r="3" />
													<circle class="spinner_S1WN spinner_JApP" cx="20" cy="12" r="3" />
												</svg>
											{:else if speaking}
												<svg
													xmlns="http://www.w3.org/2000/svg"
													fill="none"
													viewBox="0 0 24 24"
													aria-hidden="true"
													stroke-width="2.3"
													stroke="currentColor"
													class="w-4 h-4"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="M17.25 9.75 19.5 12m0 0 2.25 2.25M19.5 12l2.25-2.25M19.5 12l-2.25 2.25m-10.5-6 4.72-4.72a.75.75 0 0 1 1.28.53v15.88a.75.75 0 0 1-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.009 9.009 0 0 1 2.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75Z"
													/>
												</svg>
											{:else}
												<svg
													xmlns="http://www.w3.org/2000/svg"
													fill="none"
													viewBox="0 0 24 24"
													aria-hidden="true"
													stroke-width="2.3"
													stroke="currentColor"
													class="w-4 h-4"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="M19.114 5.636a9 9 0 010 12.728M16.463 8.288a5.25 5.25 0 010 7.424M6.75 8.25l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z"
													/>
												</svg>
											{/if}
										</button>
									</Tooltip>
								{/if}

								{#if message.usage}
									<Tooltip
										content={message.usage
											? `<pre>${sanitizeResponseContent(
													JSON.stringify(message.usage, null, 2)
														.replace(/"([^(")"]+)":/g, '$1:')
														.slice(1, -1)
														.split('\n')
														.map((line) => line.slice(2))
														.map((line) => (line.endsWith(',') ? line.slice(0, -1) : line))
														.join('\n')
												)}</pre>`
											: ''}
										placement="bottom"
									>
										<button
											aria-hidden="true"
											class=" {isLastMessage || ($settings?.highContrastMode ?? false)
												? 'visible'
												: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition whitespace-pre-wrap"
											on:click={() => {
												console.log(message);
											}}
											id="info-{message.id}"
										>
											<svg
												aria-hidden="true"
												xmlns="http://www.w3.org/2000/svg"
												fill="none"
												viewBox="0 0 24 24"
												stroke-width="2.3"
												stroke="currentColor"
												class="w-4 h-4"
											>
												<path
													stroke-linecap="round"
													stroke-linejoin="round"
													d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z"
												/>
											</svg>
										</button>
									</Tooltip>
								{/if}

								{#if !readOnly}
									{#if !$temporaryChatEnabled && ($config?.features.enable_message_rating ?? true) && ($user?.role === 'admin' || ($user?.permissions?.chat?.rate_response ?? true))}
										<Tooltip content={$i18n.t('Good Response')} placement="bottom">
											<button
												aria-label={$i18n.t('Good Response')}
												class="{isLastMessage || ($settings?.highContrastMode ?? false)
													? 'visible'
													: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg {(
													message?.annotation?.rating ?? ''
												).toString() === '1'
													? 'bg-gray-100 dark:bg-gray-800'
													: ''} dark:hover:text-white hover:text-black transition disabled:cursor-progress disabled:hover:bg-transparent"
												disabled={feedbackLoading}
												on:click={async () => {
													await feedbackHandler(1);
													window.setTimeout(() => {
														document
															.getElementById(`message-feedback-${message.id}`)
															?.scrollIntoView();
													}, 0);
												}}
											>
												<svg
													aria-hidden="true"
													stroke="currentColor"
													fill="none"
													stroke-width="2.3"
													viewBox="0 0 24 24"
													stroke-linecap="round"
													stroke-linejoin="round"
													class="w-4 h-4"
													xmlns="http://www.w3.org/2000/svg"
												>
													<path
														d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"
													/>
												</svg>
											</button>
										</Tooltip>

										<Tooltip content={$i18n.t('Bad Response')} placement="bottom">
											<button
												aria-label={$i18n.t('Bad Response')}
												class="{isLastMessage || ($settings?.highContrastMode ?? false)
													? 'visible'
													: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg {(
													message?.annotation?.rating ?? ''
												).toString() === '-1'
													? 'bg-gray-100 dark:bg-gray-800'
													: ''} dark:hover:text-white hover:text-black transition disabled:cursor-progress disabled:hover:bg-transparent"
												disabled={feedbackLoading}
												on:click={async () => {
													await feedbackHandler(-1);
													window.setTimeout(() => {
														document
															.getElementById(`message-feedback-${message.id}`)
															?.scrollIntoView();
													}, 0);
												}}
											>
												<svg
													aria-hidden="true"
													stroke="currentColor"
													fill="none"
													stroke-width="2.3"
													viewBox="0 0 24 24"
													stroke-linecap="round"
													stroke-linejoin="round"
													class="w-4 h-4"
													xmlns="http://www.w3.org/2000/svg"
												>
													<path
														d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"
													/>
												</svg>
											</button>
										</Tooltip>
									{/if}

									{#if isLastMessage && ($user?.role === 'admin' || ($user?.permissions?.chat?.continue_response ?? true))}
										<Tooltip content={$i18n.t('Continue Response')} placement="bottom">
											<button
												aria-label={$i18n.t('Continue Response')}
												type="button"
												id="continue-response-button"
												class="{isLastMessage || ($settings?.highContrastMode ?? false)
													? 'visible'
													: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
												on:click={() => {
													continueResponse();
												}}
											>
												<svg
													aria-hidden="true"
													xmlns="http://www.w3.org/2000/svg"
													fill="none"
													viewBox="0 0 24 24"
													stroke-width="2.3"
													stroke="currentColor"
													class="w-4 h-4"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"
													/>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="M15.91 11.672a.375.375 0 0 1 0 .656l-5.603 3.113a.375.375 0 0 1-.557-.328V8.887c0-.286.307-.466.557-.327l5.603 3.112Z"
													/>
												</svg>
											</button>
										</Tooltip>
									{/if}

									{#if $user?.role === 'admin' || ($user?.permissions?.chat?.regenerate_response ?? true)}
										{#if $settings?.regenerateMenu ?? true}
											<button
												type="button"
												class="hidden regenerate-response-button"
												on:click={() => {
													showRateComment = false;
													regenerateResponse(message);

													(model?.actions ?? []).forEach((action) => {
														dispatch('action', {
															id: action.id,
															event: {
																id: 'regenerate-response',
																data: {
																	messageId: message.id
																}
															}
														});
													});
												}}
											/>

											<RegenerateMenu
												onRegenerate={(prompt = null) => {
													showRateComment = false;
													regenerateResponse(message, prompt);

													(model?.actions ?? []).forEach((action) => {
														dispatch('action', {
															id: action.id,
															event: {
																id: 'regenerate-response',
																data: {
																	messageId: message.id
																}
															}
														});
													});
												}}
											>
												<Tooltip content={$i18n.t('Regenerate')} placement="bottom">
													<div
														aria-label={$i18n.t('Regenerate')}
														class="{isLastMessage
															? 'visible'
															: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
													>
														<svg
															xmlns="http://www.w3.org/2000/svg"
															fill="none"
															viewBox="0 0 24 24"
															stroke-width="2.3"
															aria-hidden="true"
															stroke="currentColor"
															class="w-4 h-4"
														>
															<path
																stroke-linecap="round"
																stroke-linejoin="round"
																d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"
															/>
														</svg>
													</div>
												</Tooltip>
											</RegenerateMenu>
										{:else}
											<Tooltip content={$i18n.t('Regenerate')} placement="bottom">
												<button
													type="button"
													aria-label={$i18n.t('Regenerate')}
													class="{isLastMessage
														? 'visible'
														: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition regenerate-response-button"
													on:click={() => {
														showRateComment = false;
														regenerateResponse(message);

														(model?.actions ?? []).forEach((action) => {
															dispatch('action', {
																id: action.id,
																event: {
																	id: 'regenerate-response',
																	data: {
																		messageId: message.id
																	}
																}
															});
														});
													}}
												>
													<svg
														xmlns="http://www.w3.org/2000/svg"
														fill="none"
														viewBox="0 0 24 24"
														stroke-width="2.3"
														aria-hidden="true"
														stroke="currentColor"
														class="w-4 h-4"
													>
														<path
															stroke-linecap="round"
															stroke-linejoin="round"
															d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"
														/>
													</svg>
												</button>
											</Tooltip>
										{/if}
									{/if}

									{#if $user?.role === 'admin' || ($user?.permissions?.chat?.delete_message ?? true)}
										{#if siblings.length > 1}
											<Tooltip content={$i18n.t('Delete')} placement="bottom">
												<button
													type="button"
													aria-label={$i18n.t('Delete')}
													id="delete-response-button"
													class="{isLastMessage || ($settings?.highContrastMode ?? false)
														? 'visible'
														: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
													on:click={() => {
														showDeleteConfirm = true;
													}}
												>
													<svg
														xmlns="http://www.w3.org/2000/svg"
														fill="none"
														viewBox="0 0 24 24"
														stroke-width="2"
														stroke="currentColor"
														aria-hidden="true"
														class="w-4 h-4"
													>
														<path
															stroke-linecap="round"
															stroke-linejoin="round"
															d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0"
														/>
													</svg>
												</button>
											</Tooltip>
										{/if}
									{/if}

									{#each model?.actions ?? [] as action}
										<Tooltip content={action.name} placement="bottom">
											<button
												type="button"
												aria-label={action.name}
												class="{isLastMessage || ($settings?.highContrastMode ?? false)
													? 'visible'
													: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
												on:click={() => {
													actionMessage(action.id, message);
												}}
											>
												{#if action?.icon}
													<div class="size-4">
														<img
															src={action.icon}
															class="w-4 h-4 {action.icon.includes('data:image/svg')
																? 'dark:invert-[80%]'
																: ''}"
															style="fill: currentColor;"
															alt={action.name}
														/>
													</div>
												{:else}
													<Sparkles strokeWidth="2.1" className="size-4" />
												{/if}
											</button>
										</Tooltip>
									{/each}
								{/if}
							{/if}
						{/if}
					</div>

					{#if message.done && showRateComment}
						<RateComment
							bind:message
							bind:show={showRateComment}
							on:save={async (e) => {
								await feedbackHandler(null, {
									...e.detail
								});
							}}
						/>
					{/if}

					{#if (isLastMessage || ($settings?.keepFollowUpPrompts ?? false)) && message.done && !readOnly && (message?.followUps ?? []).length > 0}
						<div class="mt-2.5" in:fade={{ duration: 100 }}>
							<FollowUps
								followUps={message?.followUps}
								onClick={(prompt) => {
									if ($settings?.insertFollowUpPrompt ?? false) {
										// Insert the follow-up prompt into the input box
										setInputText(prompt);
									} else {
										// Submit the follow-up prompt directly
										submitMessage(message?.id, prompt);
									}
								}}
							/>
						</div>
					{/if}
				{/if}
			</div>
		</div>
	</div>
{/key}

<style>
	.buttons::-webkit-scrollbar {
		display: none; /* for Chrome, Safari and Opera */
	}

	.buttons {
		-ms-overflow-style: none; /* IE and Edge */
		scrollbar-width: none; /* Firefox */
	}
</style>
