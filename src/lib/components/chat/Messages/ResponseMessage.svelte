<script lang="ts">
	import { toast } from 'svelte-sonner';
	import dayjs from 'dayjs';

	import { createEventDispatcher, onDestroy } from 'svelte';
	import { onMount, tick, getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType, t } from 'i18next';

	const i18n = getContext<Writable<i18nType>>('i18n');

	const dispatch = createEventDispatcher();

	import { createNewFeedback, getFeedbackById, updateFeedbackById } from '$lib/apis/evaluations';
	import { getChatById } from '$lib/apis/chats';
	import { generateTags } from '$lib/apis';

	import {
		audioQueue,
		config,
		models,
		settings,
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
	import { WEBUI_API_BASE_URL, WEBUI_BASE_URL } from '$lib/constants';

	import Name from './Name.svelte';
	import ProfileImage from './ProfileImage.svelte';
	import Skeleton from './Skeleton.svelte';
	import Image from '$lib/components/common/Image.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import RateComment from './RateComment.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import WebSearchResults from './ResponseMessage/WebSearchResults.svelte';
	import Sparkles from '$lib/components/icons/Sparkles.svelte';
	import Download from '$lib/components/icons/Download.svelte';

	import DeleteConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';

	import Error from './Error.svelte';
	import Citations from './Citations.svelte';
	import CodeExecutions from './CodeExecutions.svelte';
	import ContentRenderer from './ContentRenderer.svelte';
	import { KokoroWorker } from '$lib/workers/KokoroWorker';
	import FollowUps from './ResponseMessage/FollowUps.svelte';
	import { fade } from 'svelte/transition';
	import { flyAndScale } from '$lib/utils/transitions';
	import RegenerateMenu from './ResponseMessage/RegenerateMenu.svelte';
	import StatusHistory from './ResponseMessage/StatusHistory.svelte';
	import FullHeightIframe from '$lib/components/common/FullHeightIframe.svelte';
	import ToolCallDisplay from '$lib/components/common/ToolCallDisplay.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';

	interface MessageType {
		id: string;
		model: string;
		content: string;
		files?: { type: string; url: string }[];
		timestamp: number;
		role: string;
		statusHistory?: {
			done: boolean;
			action: string;
			description: string;
			urls?: string[];
			query?: string;
		}[];
		status?: {
			done: boolean;
			action: string;
			description: string;
			urls?: string[];
			query?: string;
		};
		done: boolean;
		error?: boolean | { content: string };
		sources?: string[];
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

	let message: MessageType = structuredClone(history.messages[messageId]);
	$: if (history.messages) {
		const source = history.messages[messageId];
		if (source) {
			// Fast path: O(1) check on the fields that change most often (content during streaming, done at end)
			// Avoids 2x O(n) JSON.stringify calls that are always true during streaming anyway
			if (message.content !== source.content || message.done !== source.done) {
				message = structuredClone(source);
			} else if (JSON.stringify(message) !== JSON.stringify(source)) {
				// Slow path: full comparison for infrequent changes (sources, annotations, status, etc.)
				message = structuredClone(source);
			}
		}
	}

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

	let citationsElement: HTMLDivElement;

	let contentContainerElement: HTMLDivElement;
	let buttonsContainerElement: HTMLDivElement;
	let showDeleteConfirm = false;

	let model = null;
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
	type GeneratedFileItem = {
		id: string;
		name: string;
		url: string;
		source: string;
		size?: number;
		isImage?: boolean;
	};

	let generatedFiles: GeneratedFileItem[] = [];

	const normalizeFileRef = (value: unknown): string | null => {
		if (typeof value !== 'string') return null;
		const normalized = value.trim();
		if (!normalized) return null;
		const lowered = normalized.toLowerCase();
		if (lowered === 'null' || lowered === 'undefined') return null;
		return normalized;
	};

	const inferFileName = (value: string, fallback = 'generated-file') => {
		const sanitized = (value || '').split('?')[0];
		const pathPart = sanitized.split('/').pop() || sanitized;
		const windowsPathPart = pathPart.split('\\').pop() || pathPart;
		return windowsPathPart.trim() || fallback;
	};

	const normalizeOpenWebUiFileUrl = (value: string): string => {
		if (!value) return value;

		let output = value;
		if (output.includes('/v1/files/') && !output.includes('/openai/v1/files/')) {
			output = output.replace(/(^|[^/])\/v1\/files\//g, '$1/openai/v1/files/');
		}

		if (output.startsWith('http') || output.startsWith('data:') || output.startsWith('/')) {
			return output;
		}

		return `${WEBUI_API_BASE_URL}/files/${output}/content`;
	};

	const isImageRef = (name: string, type?: string, contentType?: string): boolean => {
		const ext = (name.split('.').pop() || '').toLowerCase();
		if ((contentType || '').startsWith('image/')) return true;
		if ((type || '').toLowerCase() === 'image') return true;
		return ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext);
	};

	const toGeneratedFile = (item: any, source: string): GeneratedFileItem | null => {
		if (typeof item === 'string') {
			const ref = normalizeFileRef(item);
			if (!ref) return null;
			const name = inferFileName(ref);
			return {
				id: `${source}:${ref}`,
				name,
				url: normalizeOpenWebUiFileUrl(ref),
				source,
				isImage: isImageRef(name)
			};
		}

		if (!item || typeof item !== 'object') return null;

		const ref =
			normalizeFileRef(item.url) ??
			normalizeFileRef(item.download_url) ??
			normalizeFileRef(item.downloadUrl) ??
			normalizeFileRef(item.ossUrl) ??
			normalizeFileRef(item.domainUrl) ??
			normalizeFileRef(item.id) ??
			normalizeFileRef(item.file_id) ??
			normalizeFileRef(item.fileId);

		if (!ref) return null;

		const name =
			(typeof item.name === 'string' && item.name.trim()) ||
			(typeof item.filename === 'string' && item.filename.trim()) ||
			(typeof item.fileName === 'string' && item.fileName.trim()) ||
			inferFileName(ref);

		const type = typeof item.type === 'string' ? item.type : undefined;
		const contentType = typeof item.content_type === 'string' ? item.content_type : undefined;

		return {
			id: `${source}:${ref}:${name}`,
			name,
			url: normalizeOpenWebUiFileUrl(ref),
			source,
			size: typeof item.size === 'number' ? item.size : undefined,
			isImage: isImageRef(name, type, contentType)
		};
	};

	const collectGeneratedFiles = (messageData: MessageType): GeneratedFileItem[] => {
		const files: GeneratedFileItem[] = [];

		if (Array.isArray((messageData as any)?.files)) {
			for (const item of (messageData as any).files) {
				const normalized = toGeneratedFile(item, 'assistant');
				if (normalized) files.push(normalized);
			}
		}

		const deduped = new Map<string, GeneratedFileItem>();
		for (const file of files) {
			deduped.set(`${file.url}|${file.name}`, file);
		}

		return Array.from(deduped.values());
	};

	$: generatedFiles = collectGeneratedFiles(message);
	const INLINE_GENERATED_FILES_MARKER = '<!--__GENERATED_FILES__-->';

	const normalizeSourcesHeading = (content: string): string => {
		if (!content) return '';
		return content.replace(
			/(^|\n)(#{1,6}\s*)?Sources\s*(?=\n|$)/gim,
			(_, prefix: string) => `${prefix}参考来源`
		);
	};

	const TOOL_CALL_BLOCK_REGEX = /<details\b[^>]*\btype="tool_calls"[^>]*>[\s\S]*?<\/details>/gim;
	const TOOL_CALL_OPEN_TAG_REGEX = /^<details\b([^>]*)>/i;
	const TOOL_CALL_ATTR_REGEX = /(\w+)="([^"]*)"/g;
	const PROCESS_TOOL_LABELS: Record<string, string> = {
		internet_search: '网络搜索',
		联网搜索: '网络搜索',
		visit_webpage: '网页读取',
		网页读取: '网页读取',
		current_server_time: '服务器时间',
		服务器时间: '服务器时间',
		math_calculator: '数学计算',
		数学计算: '数学计算',
		tool_self_check: '工具自检',
		工具自检: '工具自检',
		read_structured_file: '读取结构化文件',
		读取结构化文件: '读取结构化文件',
		write_structured_file: '写入结构化文件',
		写入结构化文件: '写入结构化文件',
		gotenberg_convert: 'PDF 转换',
		PDF转换: 'PDF 转换',
		'PDF 转换': 'PDF 转换'
	};

	type ProcessToolCallItem = { key: string; attrs: Record<string, string> };
	type ProcessToolCallGroup = {
		key: string;
		label: string;
		count: number;
		items: ProcessToolCallItem[];
	};
	type ProcessToolVisualTiming = { firstSeenAt: number };

	const MIN_PROCESS_RUNNING_MS = 900;
	const processToolVisualTimingByKey = new Map<string, ProcessToolVisualTiming>();

	const normalizeProcessToolStatus = (attrs: Record<string, string>): string => {
		const normalized = (attrs.status || '').trim().toLowerCase();
		if (['running', 'success', 'error', 'timeout'].includes(normalized)) {
			return normalized;
		}
		return (attrs.done || '').trim().toLowerCase() === 'true' ? 'success' : 'running';
	};

	const getToolCallAttrs = (block: string): Record<string, string> => {
		const openTag = block.match(TOOL_CALL_OPEN_TAG_REGEX)?.[1] ?? '';
		const attrs: Record<string, string> = {};
		for (const item of openTag.matchAll(TOOL_CALL_ATTR_REGEX)) {
			attrs[item[1]] = item[2];
		}
		return attrs;
	};

	const getToolCallKey = (attrs: Record<string, string>): string => {
		const callKey = (attrs.call_key || '').trim();
		if (callKey) return `call_key:${callKey}`;
		const id = (attrs.id || '').trim();
		if (id) return `id:${id}`;
		const name = (attrs.name || '').trim();
		const args = (attrs.arguments || '').trim();
		if (!name && !args) return '';
		return `name_args:${name}|${args}`;
	};

	const getToolCallFallbackKey = (attrs: Record<string, string>): string => {
		const name = (attrs.name || '').trim();
		const args = (attrs.arguments || '').trim();
		if (!name && !args) return '';
		return `name_args:${name}|${args}`;
	};

	const dedupeToolCallBlocks = (content: string): string => {
		if (!content || !content.includes('type="tool_calls"')) return content;

		const matches = Array.from(content.matchAll(TOOL_CALL_BLOCK_REGEX));
		if (matches.length === 0) return content;
		const blocks = matches.map((match) => {
			const block = match[0] || '';
			const attrs = getToolCallAttrs(block);
			const isPending = (attrs.done || '').toLowerCase() !== 'true';
			const name = (attrs.name || '').trim();
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

	const getProcessToolLabel = (attrs: Record<string, string>): string => {
		const rawName = (attrs.name || '').trim();
		if (rawName) {
			return PROCESS_TOOL_LABELS[rawName] ?? rawName;
		}

		const rawArgs = attrs.arguments || '';
		if (rawArgs) {
			try {
				const parsed = JSON.parse(rawArgs);
				if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
					const keys = new Set(Object.keys(parsed as Record<string, unknown>));
					if (keys.has('query') || keys.has('max_results') || keys.has('keywords')) {
						return '网络搜索';
					}
					if (keys.has('url') || keys.has('urls') || keys.has('link')) {
						return '网页读取';
					}
					if (keys.has('expression') || keys.has('formula')) {
						return '数学计算';
					}
					if (
						keys.has('content') &&
						(keys.has('path') || keys.has('file_path') || keys.has('filename'))
					) {
						return '写入结构化文件';
					}
					if (keys.has('path') || keys.has('file_path') || keys.has('filename')) {
						return '读取结构化文件';
					}
				}
			} catch {
				// Ignore malformed tool args and fall through to generic label.
			}
		}

		return '工具调用';
	};

	const groupProcessToolCallItems = (items: ProcessToolCallItem[]): ProcessToolCallGroup[] => {
		const groups: ProcessToolCallGroup[] = [];

		for (const item of items) {
			const label = getProcessToolLabel(item.attrs);
			const lastGroup = groups.at(-1);

			if (lastGroup && lastGroup.label === label) {
				lastGroup.items = [...lastGroup.items, item];
				lastGroup.count = lastGroup.items.length;
				continue;
			}

			groups.push({
				key: item.key,
				label,
				count: 1,
				items: [item]
			});
		}

		return groups;
	};

	const getProcessToolStatusMessage = (attrs: Record<string, string>): string => {
		const label = getProcessToolLabel(attrs);
		const status = normalizeProcessToolStatus(attrs);
		if (status === 'success') return `${label} 已完成`;
		if (status === 'timeout') return `${label} 已超时`;
		if (status === 'error') return `${label} 运行失败`;
		return `正在执行 ${label}...`;
	};

	const stripDownloadSection = (content: string, allowInlineFiles: boolean): string => {
		if (!content) return '';

		const lines = content.split('\n');
		const output: string[] = [];
		let markerInserted = false;

		for (const line of lines) {
			const trimmed = line.trim();
			const linkMatch = trimmed.match(/\[[^\]]+\]\(([^)]+)\)/);
			const linkTarget = (linkMatch?.[1] || '').toLowerCase();
			const hasGeneratedFileLink =
				!!linkTarget &&
				(linkTarget.includes('/openai/v1/files/') ||
					linkTarget.includes('/v1/files/') ||
					linkTarget.includes('sandbox:/mnt/data/'));

			const hasDownloadLabel =
				/(文件下载|下载链接|下载地址|生成文件（可下载）|生成文件\(可下载\))/i.test(trimmed);

			if (hasDownloadLabel || hasGeneratedFileLink) {
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

		const toolCallRegex = /<details\b[^>]*\btype="tool_calls"[^>]*>[\s\S]*?<\/details>/gim;
		const matches = Array.from(content.matchAll(toolCallRegex));

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

	let processContent = '';
	let processToolCallItems: ProcessToolCallItem[] = [];
	let processToolCallGroups: ProcessToolCallGroup[] = [];
	let processSummary = '';
	let showProcessToolHistory = true;
	let processStatusTick = 0;
	let processStatusTimer: ReturnType<typeof setTimeout> | null = null;
	let finalMessageContent = '';
	let finalContentBeforeGeneratedFiles = '';
	let finalContentAfterGeneratedFiles = '';
	let placeInlineGeneratedFiles = false;

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

	const getEffectiveProcessSummaryAttrs = (
		item: ProcessToolCallItem | undefined
	): Record<string, string> | null => {
		if (!item) return null;

		const rawStatus = normalizeProcessToolStatus(item.attrs);
		const now = Date.now();
		let timing = processToolVisualTimingByKey.get(item.key);
		if (!timing) {
			timing = { firstSeenAt: now };
			processToolVisualTimingByKey.set(item.key, timing);
		}

		if (rawStatus === 'running') {
			clearProcessStatusTimer();
			return item.attrs;
		}

		const elapsedMs = now - timing.firstSeenAt;
		if (elapsedMs < MIN_PROCESS_RUNNING_MS) {
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

	$: {
		const rawContent = message?.content ?? '';
		const normalizedContent = dedupeToolCallBlocks(normalizeSourcesHeading(rawContent));
		const { process, final } = splitToolCallSection(normalizedContent);
		processContent = process;
		processToolCallItems = Array.from(process.matchAll(TOOL_CALL_BLOCK_REGEX))
			.map((match, index) => {
				const block = match[0] || '';
				const attrs = getToolCallAttrs(block);
				const key =
					(attrs.call_key || '').trim() ||
					(attrs.id || '').trim() ||
					`${(attrs.name || '').trim()}-${index}`;

				return {
					key,
					attrs
				};
			})
			.filter((item) => Object.keys(item.attrs).length > 0);
		const activeProcessKeys = new Set(processToolCallItems.map((item) => item.key));
		for (const key of Array.from(processToolVisualTimingByKey.keys())) {
			if (!activeProcessKeys.has(key)) {
				processToolVisualTimingByKey.delete(key);
			}
		}
		processToolCallGroups = groupProcessToolCallItems(processToolCallItems);
		processStatusTick;
		const summaryAttrs = getEffectiveProcessSummaryAttrs(processToolCallItems.at(-1));
		processSummary =
			summaryAttrs && processToolCallItems.length > 0
				? getProcessToolStatusMessage(summaryAttrs)
				: '';
		const cleanedFinal = normalizeLeakedFormatting(
			stripDownloadSection(final, generatedFiles.length > 0)
		);
		finalMessageContent = cleanedFinal;

		if (cleanedFinal.includes(INLINE_GENERATED_FILES_MARKER)) {
			const [before = '', after = ''] = cleanedFinal.split(INLINE_GENERATED_FILES_MARKER, 2);
			finalContentBeforeGeneratedFiles = before.trim();
			finalContentAfterGeneratedFiles = after.trim();
			placeInlineGeneratedFiles = true;
		} else {
			finalContentBeforeGeneratedFiles = '';
			finalContentAfterGeneratedFiles = '';
			placeInlineGeneratedFiles = false;
		}
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
						{#if model?.info?.meta?.capabilities?.status_updates ?? true}
							<StatusHistory statusHistory={message?.statusHistory} expand={true} />
						{/if}

						{#if message?.files && message.files?.filter((f) => f.type === 'image').length > 0}
							<div
								class="my-1 w-full flex overflow-x-auto gap-2 flex-wrap"
								dir={$settings?.chatDirection ?? 'auto'}
							>
								{#each message.files as file}
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

						{#if message?.embeds && message.embeds.length > 0}
							<div
								class="my-1 w-full flex overflow-x-auto gap-2 flex-wrap"
								id={`${message.id}-embeds-container`}
							>
								{#each message.embeds as embed, idx}
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
							{#if processContent}
								<div
									class="mb-2 w-full overflow-hidden rounded-xl border border-gray-200/90 bg-gray-50/80 dark:border-gray-800 dark:bg-gray-900/70"
								>
									<button
										type="button"
										class="flex w-full items-center justify-between gap-2 border-b border-gray-200/80 px-3 py-2 text-left dark:border-gray-800"
										on:click={() => {
											showProcessToolHistory = !showProcessToolHistory;
										}}
									>
										<div class="min-w-0">
											<div
												class="text-[11px] font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-300"
											>
												执行过程
											</div>
											{#if processSummary}
												<div class="mt-0.5 line-clamp-1 text-xs text-gray-500 dark:text-gray-400">
													{processSummary}
												</div>
											{/if}
										</div>

										<ChevronDown
											className={`size-3.5 shrink-0 text-gray-500 transition-transform ${
												showProcessToolHistory ? 'rotate-180' : ''
											}`}
										/>
									</button>

									{#if showProcessToolHistory}
										<div class="px-2 py-2">
											{#each processToolCallGroups as group, idx (group.key)}
												<div class="mb-1 flex items-stretch gap-2">
													<div>
														<div class="mb-1.5 px-1 pt-3">
															<span
																class="relative flex size-1.5 items-center justify-center rounded-full"
															>
																<span
																	class="relative inline-flex size-1.5 rounded-full bg-gray-500 dark:bg-gray-400"
																></span>
															</span>
														</div>
														{#if idx !== processToolCallGroups.length - 1}
															<div
																class="ml-[6.5px] h-[calc(100%-14px)] w-[0.5px] bg-gray-300 dark:bg-gray-700"
															/>
														{/if}
													</div>

													<div class="min-w-0 flex-1">
														{#if group.count > 1}
															<div
																class="rounded-lg border border-gray-200/80 bg-white/70 p-2 dark:border-gray-800 dark:bg-gray-950/20"
															>
																<div class="mb-2 flex items-center justify-between px-1">
																	<div
																		class="text-[11px] font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-300"
																	>
																		{group.label}
																	</div>
																	<div class="text-[11px] text-gray-400 dark:text-gray-500">
																		连续 {group.count} 次
																	</div>
																</div>

																<div class="space-y-1.5">
																	{#each group.items as item (item.key)}
																		<ToolCallDisplay
																			id={`${chatId}-${message.id}-process-${item.key}`}
																			attributes={item.attrs}
																			open={false}
																			embedded={true}
																			className="w-full"
																		/>
																	{/each}
																</div>
															</div>
														{:else}
															{#each group.items as item (item.key)}
																<ToolCallDisplay
																	id={`${chatId}-${message.id}-process-${item.key}`}
																	attributes={item.attrs}
																	open={false}
																	embedded={true}
																	className="w-full"
																/>
															{/each}
														{/if}
													</div>
												</div>
											{/each}
										</div>
									{/if}
								</div>
							{/if}

							{#if finalMessageContent === '' && !processContent && !message.error && ((model?.info?.meta?.capabilities?.status_updates ?? true) ? (message?.statusHistory ?? [...(message?.status ? [message?.status] : [])]).length === 0 || (message?.statusHistory?.at(-1)?.hidden ?? false) : true)}
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

							{#if generatedFiles.length > 0 && placeInlineGeneratedFiles}
								<div
									class="mt-3 rounded-xl border border-gray-200/90 bg-gray-50/70 dark:border-gray-800 dark:bg-gray-900/70"
								>
									<div
										class="border-b border-gray-200 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-gray-600 dark:border-gray-800 dark:text-gray-300"
									>
										生成文件
									</div>
									<div class="space-y-1.5 p-2">
										{#each generatedFiles as file}
											<div
												class="flex items-center justify-between gap-2 rounded-lg border border-gray-200 bg-white px-2 py-1.5 dark:border-gray-700 dark:bg-gray-850"
											>
												<div class="min-w-0 flex items-center gap-2">
													{#if file.isImage}
														<img
															src={file.url}
															alt={file.name}
															class="size-8 rounded-md border border-gray-200 object-cover dark:border-gray-700"
														/>
													{/if}
													<div class="min-w-0">
														<a
															href={file.url}
															target="_blank"
															rel="noreferrer"
															class="line-clamp-1 text-[13px] font-medium text-gray-800 hover:text-blue-600 dark:text-gray-100 dark:hover:text-blue-400"
														>
															{file.name}
														</a>
													</div>
												</div>
												<a
													href={file.url}
													target="_blank"
													rel="noreferrer"
													class="shrink-0 inline-flex size-7 items-center justify-center rounded-md border border-gray-200 bg-white text-gray-600 hover:bg-gray-100 hover:text-blue-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800 dark:hover:text-blue-400"
													title={$i18n.t('Download')}
												>
													<Download className="size-3.5" />
												</a>
											</div>
										{/each}
									</div>
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

							{#if generatedFiles.length > 0 && !placeInlineGeneratedFiles}
								<div
									class="mt-3 rounded-xl border border-gray-200/90 bg-gray-50/70 dark:border-gray-800 dark:bg-gray-900/70"
								>
									<div
										class="border-b border-gray-200 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-gray-600 dark:border-gray-800 dark:text-gray-300"
									>
										生成文件
									</div>
									<div class="space-y-1.5 p-2">
										{#each generatedFiles as file}
											<div
												class="flex items-center justify-between gap-2 rounded-lg border border-gray-200 bg-white px-2 py-1.5 dark:border-gray-700 dark:bg-gray-850"
											>
												<div class="min-w-0 flex items-center gap-2">
													{#if file.isImage}
														<img
															src={file.url}
															alt={file.name}
															class="size-8 rounded-md border border-gray-200 object-cover dark:border-gray-700"
														/>
													{/if}
													<div class="min-w-0">
														<a
															href={file.url}
															target="_blank"
															rel="noreferrer"
															class="line-clamp-1 text-[13px] font-medium text-gray-800 hover:text-blue-600 dark:text-gray-100 dark:hover:text-blue-400"
														>
															{file.name}
														</a>
													</div>
												</div>
												<a
													href={file.url}
													target="_blank"
													rel="noreferrer"
													class="shrink-0 inline-flex size-7 items-center justify-center rounded-md border border-gray-200 bg-white text-gray-600 hover:bg-gray-100 hover:text-blue-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800 dark:hover:text-blue-400"
													title={$i18n.t('Download')}
												>
													<Download className="size-3.5" />
												</a>
											</div>
										{/each}
									</div>
								</div>
							{/if}

							{#if (message?.sources || message?.citations) && (model?.info?.meta?.capabilities?.citations ?? true)}
								<Citations
									bind:this={citationsElement}
									id={message?.id}
									{chatId}
									sources={message?.sources ?? message?.citations}
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
