<script lang="ts">
	// @ts-nocheck
	import Fuse from 'fuse.js';
	import { toast } from 'svelte-sonner';
	import { v4 as uuidv4 } from 'uuid';
	import { PaneGroup, Pane, PaneResizer } from 'paneforge';

	import { onMount, getContext, onDestroy, tick } from 'svelte';
	import { slide } from 'svelte/transition';
	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import {
		mobile,
		showSidebar,
		knowledge as _knowledge,
		config,
		user,
		settings
	} from '$lib/stores';
	import { getKnowflowFrontendConfig } from '$lib/apis/knowflow';

	import {
		updateFileDataContentById,
		uploadFile,
		deleteFileById,
		getFileById
	} from '$lib/apis/files';
	import {
		addFileToKnowledgeById,
		getKnowledgeById,
		removeFileFromKnowledgeById,
		resetKnowledgeById,
		updateFileFromKnowledgeById,
		updateKnowledgeById,
		updateKnowledgeAccessGrants,
		searchKnowledgeFilesById
	} from '$lib/apis/knowledge';
	import { processWeb, processYoutubeVideo } from '$lib/apis/retrieval';

	import { blobToFile, isYoutubeUrl } from '$lib/utils';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Files from './KnowledgeBase/Files.svelte';
	import AddFilesPlaceholder from '$lib/components/AddFilesPlaceholder.svelte';

	import AddContentMenu from './KnowledgeBase/AddContentMenu.svelte';
	import AddTextContentModal from './KnowledgeBase/AddTextContentModal.svelte';

	import SyncConfirmDialog from '../../common/ConfirmDialog.svelte';
	import Drawer from '$lib/components/common/Drawer.svelte';
	import ArrowPath from '$lib/components/icons/ArrowPath.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import LockClosed from '$lib/components/icons/LockClosed.svelte';
	import Settings from '$lib/components/icons/Settings.svelte';
	import AccessControlModal from '../common/AccessControlModal.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import FilesOverlay from '$lib/components/chat/MessageInput/FilesOverlay.svelte';
	import DropdownOptions from '$lib/components/common/DropdownOptions.svelte';
	import Pagination from '$lib/components/common/Pagination.svelte';
	import AttachWebpageModal from '$lib/components/chat/MessageInput/AttachWebpageModal.svelte';
	import KnowledgeSettingsPanel from './KnowledgeSettingsPanel.svelte';

	let largeScreen = true;

	let pane;
	let showSidepanel = true;

	let showAddWebpageModal = false;
	let showAddTextContentModal = false;

	let showSyncConfirmModal = false;
	let showAccessControlModal = false;
	let showKnowledgeSettings = false;
	let desktopKnowledgeSettingsPanelRef = null;
	let mobileKnowledgeSettingsPanelRef = null;

	let minSize = 0;
	type Knowledge = {
		id: string;
		name: string;
		description: string;
		data: {
			file_ids: string[];
		};
		files: any[];
		access_grants?: any[];
		write_access?: boolean;
	};

	let id = null;
	let knowledge: Knowledge | null = null;
	let knowledgeId = null;

	let selectedFileId = null;
	let selectedFile = null;
	let selectedFileContent = '';

	let inputFiles = null;

	let query = '';
	let searchDebounceTimer: ReturnType<typeof setTimeout>;

	let viewOption = null;
	let sortKey = null;
	let direction = null;

	let currentPage = 1;
	let fileItems = null;
	let fileItemsTotal = null;
	let refreshLoading = false;

	$: knowflowConfig = getKnowflowFrontendConfig($config);
	$: knowflowReadOnly = knowflowConfig?.enabled && knowflowConfig?.readOnly;
	$: knowflowSiteUrl = knowflowConfig?.siteUrl ?? '';
	$: canEditKnowledge = Boolean(knowledge?.write_access && !knowflowReadOnly);
	$: showFileContentPreview = !knowflowReadOnly;
	$: knowledgeForFiles = knowledge ? { ...knowledge, write_access: canEditKnowledge } : knowledge;
	$: showKnowledgeSettingsButton = Boolean(knowflowConfig?.enabled || $user?.role === 'admin');
	$: if (!showFileContentPreview && selectedFileId !== null) {
		selectedFileId = null;
		selectedFile = null;
		selectedFileContent = '';
	}

	const kbText = {
		knowledgeSettings: '知识库设置',
		knowledgeName: '知识库名称',
		knowledgeDescription: '知识库描述',
		fileCountSuffix: '个文件',
		readOnly: '只读',
		refresh: '刷新',
		settings: '设置',
		access: '权限',
		searchCollection: '搜索文件',
		all: '全部',
		createdByYou: '我创建的',
		sharedWithYou: '共享给我的',
		sort: '排序',
		name: '名称',
		createdAt: '创建时间',
		updatedAt: '更新时间',
		asc: '升序',
		desc: '降序',
		noContentFound: '暂无内容',
		close: '关闭',
		save: '保存',
		fileContent: '文件内容',
		addContentHere: '在此添加内容',
		saved: '已保存',
		managedReadOnly:
			'该知识库由 Knowflow 管理，此处为只读。请使用“管理知识库”编辑内容。',
		noEditPermission: '您没有权限编辑此知识库。',
		readOnlyPreview:
			'Knowflow 只读模式下不支持文件内容预览。请使用“管理知识库”查看文件详情。'
	};

	const getReadOnlyErrorMessage = () => {
		if (knowflowReadOnly) {
			return kbText.managedReadOnly;
		}
		return kbText.noEditPermission;
	};

	const ensureKnowledgeWritable = () => {
		if (!canEditKnowledge) {
			toast.error(getReadOnlyErrorMessage());
			return false;
		}
		return true;
	};

	const reset = () => {
		currentPage = 1;
	};

	$: if (query !== undefined) {
		clearTimeout(searchDebounceTimer);

		searchDebounceTimer = setTimeout(() => {
			getItemsPage();
		}, 300);
	}

	$: if (
		knowledgeId !== null &&
		viewOption !== undefined &&
		sortKey !== undefined &&
		direction !== undefined &&
		currentPage !== undefined
	) {
		getItemsPage();
	}

	$: if (
		query !== undefined &&
		viewOption !== undefined &&
		sortKey !== undefined &&
		direction !== undefined
	) {
		reset();
	}

	const getItemsPage = async () => {
		if (knowledgeId === null) return;

		fileItems = null;
		fileItemsTotal = null;

		if (sortKey === null) {
			direction = null;
		}

		const res = await searchKnowledgeFilesById(
			localStorage.token,
			knowledge.id,
			query,
			viewOption,
			sortKey,
			direction,
			currentPage
		).catch(() => {
			return null;
		});

		if (res) {
			fileItems = res.items;
			fileItemsTotal = res.total;
		}
		return res;
	};

	const loadKnowledge = async ({ silent = false } = {}) => {
		const previousKnowledgeId = knowledgeId;
		const res = await getKnowledgeById(localStorage.token, id).catch((e) => {
			if (!silent) {
				toast.error(`${e}`);
			}
			return null;
		});

		if (res) {
			knowledge = res;
			if (!Array.isArray(knowledge?.access_grants)) {
				knowledge.access_grants = [];
			}
			if (previousKnowledgeId !== knowledge?.id) {
				knowledgeId = knowledge?.id;
			}
		}

		return res;
	};

	const syncKnowledgeState = async ({ silent = false } = {}) => {
		if (!id || refreshLoading) {
			return;
		}

		refreshLoading = true;
		try {
			const previousKnowledgeId = knowledgeId;
			const res = await loadKnowledge({ silent });
			if (!res) {
				return;
			}

			const shouldRefreshItemsDirectly = previousKnowledgeId === res.id;
			const itemsRes = shouldRefreshItemsDirectly ? await getItemsPage() : null;
			const refreshedSelectedFile =
				selectedFileId !== null
					? (itemsRes?.items ?? []).find((file) => file.id === selectedFileId) ?? null
					: null;

			if (selectedFileId !== null) {
				if (showFileContentPreview && refreshedSelectedFile) {
					await fileSelectHandler(refreshedSelectedFile);
				} else {
					selectedFileId = null;
					selectedFile = null;
					selectedFileContent = '';
				}
			}
		} finally {
			refreshLoading = false;
		}
	};

	const refreshWorkspaceKnowledgeState = async () => {
		await syncKnowledgeState();
		await Promise.allSettled([
			desktopKnowledgeSettingsPanelRef?.refreshKnowflowStatusFromOutside?.(),
			mobileKnowledgeSettingsPanelRef?.refreshKnowflowStatusFromOutside?.()
		]);
	};

	const fileSelectHandler = async (file) => {
		try {
			selectedFile = file;
			selectedFileContent = selectedFile?.data?.content || '';
		} catch (e) {
			toast.error($i18n.t('Failed to load file content.'));
		}
	};

	const createFileFromText = (name, content) => {
		const blob = new Blob([content], { type: 'text/plain' });
		const file = blobToFile(blob, `${name}.txt`);

		console.log(file);
		return file;
	};

	const uploadWeb = async (urls) => {
		if (!ensureKnowledgeWritable()) {
			return;
		}

		if (!Array.isArray(urls)) {
			urls = [urls];
		}

		const newFileItems = urls.map((url) => ({
			type: 'file',
			file: '',
			id: null,
			url: url,
			name: url,
			size: null,
			status: 'uploading',
			error: '',
			itemId: uuidv4()
		}));

		// Display all items at once
		fileItems = [...newFileItems, ...(fileItems ?? [])];

		for (const fileItem of newFileItems) {
			try {
				console.log(fileItem);
				const res = await processWeb(localStorage.token, '', fileItem.url, false).catch((e) => {
					console.error('Error processing web URL:', e);
					return null;
				});

				if (res) {
					console.log(res);
					const file = createFileFromText(
						// Use URL as filename, sanitized
						fileItem.url
							.replace(/[^a-z0-9]/gi, '_')
							.toLowerCase()
							.slice(0, 50),
						res.content
					);

					const uploadedFile = await uploadFile(localStorage.token, file).catch((e) => {
						toast.error(`${e}`);
						return null;
					});

					if (uploadedFile) {
						console.log(uploadedFile);
						fileItems = fileItems.map((item) => {
							if (item.itemId === fileItem.itemId) {
								item.id = uploadedFile.id;
							}
							return item;
						});

						if (uploadedFile.error) {
							console.warn('File upload warning:', uploadedFile.error);
							toast.warning(uploadedFile.error);
							fileItems = fileItems.filter((file) => file.id !== uploadedFile.id);
						} else {
							await addFileHandler(uploadedFile.id);
						}
					} else {
						toast.error($i18n.t('Failed to upload file.'));
					}
				} else {
					// remove the item from fileItems
					fileItems = fileItems.filter((item) => item.itemId !== fileItem.itemId);
					toast.error($i18n.t('Failed to process URL: {{url}}', { url: fileItem.url }));
				}
			} catch (e) {
				// remove the item from fileItems
				fileItems = fileItems.filter((item) => item.itemId !== fileItem.itemId);
				toast.error(`${e}`);
			}
		}
	};

	const uploadFileHandler = async (file) => {
		if (!ensureKnowledgeWritable()) {
			return null;
		}

		console.log(file);

		const fileItem = {
			type: 'file',
			file: '',
			id: null,
			url: '',
			name: file.name,
			size: file.size,
			status: 'uploading',
			error: '',
			itemId: uuidv4()
		};

		if (fileItem.size == 0) {
			toast.error($i18n.t('You cannot upload an empty file.'));
			return null;
		}

		if (
			($config?.file?.max_size ?? null) !== null &&
			file.size > ($config?.file?.max_size ?? 0) * 1024 * 1024
		) {
			console.log('File exceeds max size limit:', {
				fileSize: file.size,
				maxSize: ($config?.file?.max_size ?? 0) * 1024 * 1024
			});
			toast.error(
				$i18n.t(`File size should not exceed {{maxSize}} MB.`, {
					maxSize: $config?.file?.max_size
				})
			);
			return;
		}

		fileItems = [fileItem, ...(fileItems ?? [])];
		try {
			let metadata = {
				knowledge_id: knowledge.id,
				// If the file is an audio file, provide the language for STT.
				...((file.type.startsWith('audio/') || file.type.startsWith('video/')) &&
				$settings?.audio?.stt?.language
					? {
							language: $settings?.audio?.stt?.language
						}
					: {})
			};

			const uploadedFile = await uploadFile(localStorage.token, file, metadata).catch((e) => {
				toast.error(`${e}`);
				return null;
			});

			if (uploadedFile) {
				console.log(uploadedFile);
				fileItems = fileItems.map((item) => {
					if (item.itemId === fileItem.itemId) {
						item.id = uploadedFile.id;
					}
					return item;
				});

				if (uploadedFile.error) {
					console.warn('File upload warning:', uploadedFile.error);
					toast.warning(uploadedFile.error);
					fileItems = fileItems.filter((file) => file.id !== uploadedFile.id);
				} else {
					await addFileHandler(uploadedFile.id);
				}
			} else {
				toast.error($i18n.t('Failed to upload file.'));
			}
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	const uploadDirectoryHandler = async () => {
		if (!ensureKnowledgeWritable()) {
			return;
		}

		// Check if File System Access API is supported
		const isFileSystemAccessSupported = 'showDirectoryPicker' in window;

		try {
			if (isFileSystemAccessSupported) {
				// Modern browsers (Chrome, Edge) implementation
				await handleModernBrowserUpload();
			} else {
				// Firefox fallback
				await handleFirefoxUpload();
			}
		} catch (error) {
			handleUploadError(error);
		}
	};

	// Helper function to check if a path contains hidden folders
	const hasHiddenFolder = (path) => {
		return path.split('/').some((part) => part.startsWith('.'));
	};

	// Modern browsers implementation using File System Access API
	const handleModernBrowserUpload = async () => {
		const dirHandle = await window.showDirectoryPicker();
		let totalFiles = 0;
		let uploadedFiles = 0;

		// Function to update the UI with the progress
		const updateProgress = () => {
			const percentage = (uploadedFiles / totalFiles) * 100;
			toast.info(
				$i18n.t('Upload Progress: {{uploadedFiles}}/{{totalFiles}} ({{percentage}}%)', {
					uploadedFiles: uploadedFiles,
					totalFiles: totalFiles,
					percentage: percentage.toFixed(2)
				})
			);
		};

		// Recursive function to count all files excluding hidden ones
		async function countFiles(dirHandle) {
			for await (const entry of dirHandle.values()) {
				// Skip hidden files and directories
				if (entry.name.startsWith('.')) continue;

				if (entry.kind === 'file') {
					totalFiles++;
				} else if (entry.kind === 'directory') {
					// Only process non-hidden directories
					if (!entry.name.startsWith('.')) {
						await countFiles(entry);
					}
				}
			}
		}

		// Recursive function to process directories excluding hidden files and folders
		async function processDirectory(dirHandle, path = '') {
			for await (const entry of dirHandle.values()) {
				// Skip hidden files and directories
				if (entry.name.startsWith('.')) continue;

				const entryPath = path ? `${path}/${entry.name}` : entry.name;

				// Skip if the path contains any hidden folders
				if (hasHiddenFolder(entryPath)) continue;

				if (entry.kind === 'file') {
					const file = await entry.getFile();
					const fileWithPath = new File([file], entryPath, { type: file.type });

					await uploadFileHandler(fileWithPath);
					uploadedFiles++;
					updateProgress();
				} else if (entry.kind === 'directory') {
					// Only process non-hidden directories
					if (!entry.name.startsWith('.')) {
						await processDirectory(entry, entryPath);
					}
				}
			}
		}

		await countFiles(dirHandle);
		updateProgress();

		if (totalFiles > 0) {
			await processDirectory(dirHandle);
		} else {
			console.log('No files to upload.');
		}
	};

	// Firefox fallback implementation using traditional file input
	const handleFirefoxUpload = async () => {
		return new Promise((resolve, reject) => {
			// Create hidden file input
			const input = document.createElement('input');
			input.type = 'file';
			input.webkitdirectory = true;
			input.directory = true;
			input.multiple = true;
			input.style.display = 'none';

			// Add input to DOM temporarily
			document.body.appendChild(input);

			input.onchange = async () => {
				try {
					const files = Array.from(input.files)
						// Filter out files from hidden folders
						.filter((file) => !hasHiddenFolder(file.webkitRelativePath));

					let totalFiles = files.length;
					let uploadedFiles = 0;

					// Function to update the UI with the progress
					const updateProgress = () => {
						const percentage = (uploadedFiles / totalFiles) * 100;
						toast.info(
							$i18n.t('Upload Progress: {{uploadedFiles}}/{{totalFiles}} ({{percentage}}%)', {
								uploadedFiles: uploadedFiles,
								totalFiles: totalFiles,
								percentage: percentage.toFixed(2)
							})
						);
					};

					updateProgress();

					// Process all files
					for (const file of files) {
						// Skip hidden files (additional check)
						if (!file.name.startsWith('.')) {
							const relativePath = file.webkitRelativePath || file.name;
							const fileWithPath = new File([file], relativePath, { type: file.type });

							await uploadFileHandler(fileWithPath);
							uploadedFiles++;
							updateProgress();
						}
					}

					// Clean up
					document.body.removeChild(input);
					resolve();
				} catch (error) {
					reject(error);
				}
			};

			input.onerror = (error) => {
				document.body.removeChild(input);
				reject(error);
			};

			// Trigger file picker
			input.click();
		});
	};

	// Error handler
	const handleUploadError = (error) => {
		if (error.name === 'AbortError') {
			toast.info($i18n.t('Directory selection was cancelled'));
		} else {
			toast.error($i18n.t('Error accessing directory'));
			console.error('Directory access error:', error);
		}
	};

	// Helper function to maintain file paths within zip
	const syncDirectoryHandler = async () => {
		if (!ensureKnowledgeWritable()) {
			return;
		}

		if (fileItems.length > 0) {
			const res = await resetKnowledgeById(localStorage.token, id).catch((e) => {
				toast.error(`${e}`);
			});

			if (res) {
				fileItems = [];
				toast.success($i18n.t('Knowledge reset successfully.'));

				// Upload directory
				uploadDirectoryHandler();
			}
		} else {
			uploadDirectoryHandler();
		}
	};

	const addFileHandler = async (fileId) => {
		if (!ensureKnowledgeWritable()) {
			return;
		}

		const res = await addFileToKnowledgeById(localStorage.token, id, fileId).catch((e) => {
			toast.error(`${e}`);
			return null;
		});

		if (res) {
			toast.success($i18n.t('File added successfully.'));
			await syncKnowledgeState({ silent: true });
		} else {
			toast.error($i18n.t('Failed to add file.'));
			fileItems = fileItems.filter((file) => file.id !== fileId);
		}
	};

	const deleteFileHandler = async (fileId) => {
		if (!ensureKnowledgeWritable()) {
			return;
		}

		try {
			console.log('Starting file deletion process for:', fileId);

			// Remove from knowledge base only
			const res = await removeFileFromKnowledgeById(localStorage.token, id, fileId);
			console.log('Knowledge base updated:', res);

			if (res) {
				toast.success($i18n.t('File removed successfully.'));
				await syncKnowledgeState({ silent: true });
			}
		} catch (e) {
			console.error('Error in deleteFileHandler:', e);
			toast.error(`${e}`);
		}
	};

	let debounceTimeout = null;
	let mediaQuery;

	let dragged = false;
	let isSaving = false;

	const updateFileContentHandler = async () => {
		if (!ensureKnowledgeWritable()) {
			return;
		}

		if (isSaving) {
			console.log('Save operation already in progress, skipping...');
			return;
		}

		isSaving = true;

		try {
			const res = await updateFileDataContentById(
				localStorage.token,
				selectedFile.id,
				selectedFileContent
			).catch((e) => {
				toast.error(`${e}`);
				return null;
			});

			if (res) {
				toast.success($i18n.t('File content updated successfully.'));

				selectedFileId = null;
				selectedFile = null;
				selectedFileContent = '';

				await syncKnowledgeState({ silent: true });
			}
		} finally {
			isSaving = false;
		}
	};

	const changeDebounceHandler = () => {
		if (!canEditKnowledge) {
			return;
		}

		console.log('debounce');
		if (debounceTimeout) {
			clearTimeout(debounceTimeout);
		}

		debounceTimeout = setTimeout(async () => {
			if (!canEditKnowledge) {
				return;
			}

			if (knowledge.name.trim() === '' || knowledge.description.trim() === '') {
				toast.error($i18n.t('Please fill in all fields.'));
				return;
			}

			const res = await updateKnowledgeById(localStorage.token, id, {
				...knowledge,
				name: knowledge.name,
				description: knowledge.description,
				access_grants: knowledge.access_grants ?? []
			}).catch((e) => {
				toast.error(`${e}`);
			});

			if (res) {
				toast.success($i18n.t('Knowledge updated successfully'));
			}
		}, 1000);
	};

	const handleMediaQuery = async (e) => {
		if (e.matches) {
			largeScreen = true;
		} else {
			largeScreen = false;
		}
	};

	const onDragOver = (e) => {
		if (!canEditKnowledge) {
			dragged = false;
			return;
		}

		e.preventDefault();

		// Check if a file is being draggedOver.
		if (e.dataTransfer?.types?.includes('Files')) {
			dragged = true;
		} else {
			dragged = false;
		}
	};

	const onDragLeave = () => {
		dragged = false;
	};

	const onDrop = async (e) => {
		e.preventDefault();
		dragged = false;

		if (!ensureKnowledgeWritable()) {
			return;
		}

		const handleUploadingFileFolder = (items) => {
			for (const item of items) {
				if (item.isFile) {
					item.file((file) => {
						uploadFileHandler(file);
					});
					continue;
				}

				// Not sure why you have to call webkitGetAsEntry and isDirectory seperate, but it won't work if you try item.webkitGetAsEntry().isDirectory
				const wkentry = item.webkitGetAsEntry();
				const isDirectory = wkentry.isDirectory;
				if (isDirectory) {
					// Read the directory
					wkentry.createReader().readEntries(
						(entries) => {
							handleUploadingFileFolder(entries);
						},
						(error) => {
							console.error('Error reading directory entries:', error);
						}
					);
				} else {
					toast.info($i18n.t('Uploading file...'));
					uploadFileHandler(item.getAsFile());
					toast.success($i18n.t('File uploaded!'));
				}
			}
		};

		if (e.dataTransfer?.types?.includes('Files')) {
			if (e.dataTransfer?.files) {
				const inputItems = e.dataTransfer?.items;

				if (inputItems && inputItems.length > 0) {
					handleUploadingFileFolder(inputItems);
				} else {
					toast.error($i18n.t(`File not found.`));
				}
			}
		}
	};

	onMount(async () => {
		// listen to resize 1024px
		mediaQuery = window.matchMedia('(min-width: 1024px)');

		mediaQuery.addEventListener('change', handleMediaQuery);
		handleMediaQuery(mediaQuery);

		// Select the container element you want to observe
		const container = document.getElementById('collection-container');

		// initialize the minSize based on the container width
		minSize = !largeScreen ? 100 : Math.floor((300 / container.clientWidth) * 100);

		// Create a new ResizeObserver instance
		const resizeObserver = new ResizeObserver((entries) => {
			for (let entry of entries) {
				const width = entry.contentRect.width;
				// calculate the percentage of 300
				const percentage = (300 / width) * 100;
				// set the minSize to the percentage, must be an integer
				minSize = !largeScreen ? 100 : Math.floor(percentage);

				if (showSidepanel) {
					if (pane && pane.isExpanded() && pane.getSize() < minSize) {
						pane.resize(minSize);
					}
				}
			}
		});

		// Start observing the container's size changes
		resizeObserver.observe(container);

		if (pane) {
			pane.expand();
		}

		id = $page.params.id;
		await syncKnowledgeState({ silent: false });
		if (!knowledge) {
			goto('/workspace/knowledge');
			return;
		}

		const dropZone = document.querySelector('body');
		dropZone?.addEventListener('dragover', onDragOver);
		dropZone?.addEventListener('drop', onDrop);
		dropZone?.addEventListener('dragleave', onDragLeave);
	});

	onDestroy(() => {
		clearTimeout(searchDebounceTimer);
		mediaQuery?.removeEventListener('change', handleMediaQuery);
		const dropZone = document.querySelector('body');
		dropZone?.removeEventListener('dragover', onDragOver);
		dropZone?.removeEventListener('drop', onDrop);
		dropZone?.removeEventListener('dragleave', onDragLeave);
	});

	const decodeString = (str: string) => {
		try {
			return decodeURIComponent(str);
		} catch (e) {
			return str;
		}
	};
</script>

<FilesOverlay show={dragged} />
<SyncConfirmDialog
	bind:show={showSyncConfirmModal}
	message={$i18n.t(
		'This will reset the knowledge base and sync all files. Do you wish to continue?'
	)}
	on:confirm={() => {
		syncDirectoryHandler();
	}}
/>

<AttachWebpageModal
	bind:show={showAddWebpageModal}
	onSubmit={async (e) => {
		uploadWeb(e.data);
	}}
/>

<AddTextContentModal
	bind:show={showAddTextContentModal}
	on:submit={(e) => {
		const file = createFileFromText(e.detail.name, e.detail.content);
		uploadFileHandler(file);
	}}
/>

<input
	id="files-input"
	bind:files={inputFiles}
	type="file"
	multiple
	hidden
	on:change={async () => {
		if (!ensureKnowledgeWritable()) {
			inputFiles = null;
			const fileInputElement = document.getElementById('files-input');
			if (fileInputElement) {
				fileInputElement.value = '';
			}
			return;
		}

		if (inputFiles && inputFiles.length > 0) {
			for (const file of inputFiles) {
				await uploadFileHandler(file);
			}

			inputFiles = null;
			const fileInputElement = document.getElementById('files-input');

			if (fileInputElement) {
				fileInputElement.value = '';
			}
		} else {
			toast.error($i18n.t(`File not found.`));
		}
	}}
/>

	<div class="flex flex-col w-full h-full min-h-full" id="collection-container">
		{#if id && knowledge}
				{#if $mobile}
					<Drawer
						show={showKnowledgeSettings}
						className="max-h-[85dvh] rounded-t-3xl"
					onClose={() => {
						showKnowledgeSettings = false;
					}}
					>
						<div class="px-4 py-3">
							<div class="text-sm font-medium pb-2">{kbText.knowledgeSettings}</div>
							<KnowledgeSettingsPanel bind:this={mobileKnowledgeSettingsPanelRef} />
						</div>
					</Drawer>
				{/if}

			<AccessControlModal
				bind:show={showAccessControlModal}
				bind:accessGrants={knowledge.access_grants}
			share={$user?.permissions?.sharing?.knowledge || $user?.role === 'admin'}
			sharePublic={$user?.permissions?.sharing?.public_knowledge || $user?.role === 'admin'}
			shareUsers={($user?.permissions?.access_grants?.allow_users ?? true) ||
				$user?.role === 'admin'}
				onChange={async () => {
					if (!ensureKnowledgeWritable()) {
						return;
					}

					try {
						await updateKnowledgeAccessGrants(localStorage.token, id, knowledge.access_grants ?? []);
						toast.success(kbText.saved);
				} catch (error) {
					toast.error(`${error}`);
				}
			}}
			accessRoles={['read', 'write']}
		/>
		<div class="w-full px-2">
			<div class=" flex w-full">
				<div class="flex-1">
					<div class="flex items-center justify-between w-full">
						<div class="w-full flex justify-between items-center">
								<input
									type="text"
									class="text-left w-full text-lg bg-transparent outline-hidden flex-1"
									bind:value={knowledge.name}
									aria-label={kbText.knowledgeName}
									placeholder={kbText.knowledgeName}
									disabled={!canEditKnowledge}
									on:input={() => {
										if (canEditKnowledge) {
											changeDebounceHandler();
										}
									}}
								/>

							<div class="shrink-0 mr-2.5 flex items-center gap-2">
								{#if fileItemsTotal}
									<div class="text-xs text-gray-500">{fileItemsTotal} {kbText.fileCountSuffix}</div>
								{/if}

								{#if !canEditKnowledge}
									<div
										class="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-300"
									>
										{kbText.readOnly}
									</div>
								{/if}
							</div>
						</div>

								<div class="self-center shrink-0 flex flex-wrap items-center gap-2">
									<div
										class="flex items-center gap-1.5 rounded-2xl border border-gray-200/80 bg-gray-50/80 px-1.5 py-1 dark:border-gray-800/80 dark:bg-gray-900/80"
									>
										{#if knowflowSiteUrl}
											<a
												class="inline-flex items-center rounded-xl px-2.5 py-1.5 text-xs font-medium text-gray-700 transition hover:bg-white hover:text-gray-900 dark:text-gray-200 dark:hover:bg-gray-800 dark:hover:text-gray-100"
											href={knowflowSiteUrl}
											target="_blank"
											rel="noopener noreferrer"
										>
											管理知识库
										</a>
									{/if}

									{#if showKnowledgeSettingsButton}
										<button
											class={`inline-flex items-center gap-1 rounded-xl px-2.5 py-1.5 text-xs font-medium transition ${
												showKnowledgeSettings
													? 'bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-900'
													: 'text-gray-600 hover:bg-white hover:text-gray-900 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-gray-100'
											}`}
											type="button"
											on:click={() => {
												showKnowledgeSettings = !showKnowledgeSettings;
											}}
										>
											<Settings className="size-3.5" strokeWidth="2" />
												<div class="hidden xl:block">{kbText.settings}</div>
											</button>
										{/if}
									</div>

									<button
										class="inline-flex items-center gap-1.5 rounded-xl border border-gray-100/40 bg-white/55 px-3 py-1.5 text-xs font-medium text-gray-600 shadow-none transition hover:border-gray-100/60 hover:bg-white/80 hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-850/25 dark:bg-transparent dark:text-gray-200 dark:hover:border-gray-800/40 dark:hover:bg-gray-900/35 dark:hover:text-gray-100"
										type="button"
										aria-label={kbText.refresh}
										disabled={refreshLoading}
										on:click={() => {
											refreshWorkspaceKnowledgeState();
										}}
									>
										<ArrowPath
											className={`size-3.5 ${refreshLoading ? 'animate-spin' : ''}`}
											strokeWidth="2.25"
										/>
										<div class="shrink-0">{kbText.refresh}</div>
									</button>

									{#if canEditKnowledge}
										<button
											class="inline-flex items-center gap-1 rounded-2xl bg-gray-900 px-3 py-2 text-xs font-semibold text-white shadow-xs transition hover:bg-gray-800 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white"
										type="button"
										on:click={() => {
											showAccessControlModal = true;
										}}
									>
										<LockClosed strokeWidth="2.5" className="size-3.5" />
										<div class="shrink-0">{kbText.access}</div>
									</button>
								{/if}
							</div>
						</div>

						<div class="flex w-full">
						<input
							type="text"
								class="text-left text-xs w-full text-gray-500 bg-transparent outline-hidden"
								bind:value={knowledge.description}
								aria-label={kbText.knowledgeDescription}
								placeholder={kbText.knowledgeDescription}
								disabled={!canEditKnowledge}
								on:input={() => {
									if (canEditKnowledge) {
										changeDebounceHandler();
									}
								}}
							/>
						</div>
				</div>
			</div>
			</div>

				{#if showKnowledgeSettings && !$mobile}
					<div class="flex justify-end px-2 pb-2" transition:slide={{ duration: 200 }}>
						<div
							class="w-full rounded-2xl border border-gray-100/35 bg-white/80 px-3 py-3 dark:border-gray-850/35 dark:bg-gray-900/75 lg:max-w-[46rem]"
						>
							<KnowledgeSettingsPanel bind:this={desktopKnowledgeSettingsPanelRef} />
						</div>
					</div>
				{/if}

			<div
				class="mt-2 mb-2.5 py-2 -mx-0 bg-white dark:bg-gray-900 rounded-3xl border border-gray-100/30 dark:border-gray-850/30 flex-1"
		>
			<div class="px-3.5 flex flex-1 items-center w-full space-x-2 py-0.5 pb-2">
				<div class="flex flex-1 items-center">
					<div class=" self-center ml-1 mr-3">
						<Search className="size-3.5" />
					</div>
					<input
						class=" w-full text-sm pr-4 py-1 rounded-r-xl outline-hidden bg-transparent"
						bind:value={query}
						aria-label={kbText.searchCollection}
						placeholder={kbText.searchCollection}
						on:focus={() => {
							selectedFileId = null;
						}}
					/>

					{#if canEditKnowledge}
						<div>
							<AddContentMenu
								onUpload={(data) => {
									if (data.type === 'directory') {
										uploadDirectoryHandler();
									} else if (data.type === 'web') {
										showAddWebpageModal = true;
									} else if (data.type === 'text') {
										showAddTextContentModal = true;
									} else {
										document.getElementById('files-input').click();
									}
								}}
								onSync={() => {
									showSyncConfirmModal = true;
								}}
							/>
						</div>
					{/if}
				</div>
			</div>

			<div class="px-3 flex justify-between">
				<div
					class="flex w-full bg-transparent overflow-x-auto scrollbar-none"
					on:wheel={(e) => {
						if (e.deltaY !== 0) {
							e.preventDefault();
							e.currentTarget.scrollLeft += e.deltaY;
						}
					}}
				>
					<div
						class="flex gap-3 w-fit text-center text-sm rounded-full bg-transparent px-0.5 whitespace-nowrap"
					>
						<DropdownOptions
							align="start"
							className="flex w-full items-center gap-2 truncate px-3 py-1.5 text-sm bg-gray-50 dark:bg-gray-850 rounded-xl  placeholder-gray-400 outline-hidden focus:outline-hidden"
							bind:value={viewOption}
							items={[
								{ value: null, label: kbText.all },
								{ value: 'created', label: kbText.createdByYou },
								{ value: 'shared', label: kbText.sharedWithYou }
							]}
							onChange={(value) => {
								if (value) {
									localStorage.workspaceViewOption = value;
								} else {
									delete localStorage.workspaceViewOption;
								}
							}}
						/>

						<DropdownOptions
							align="start"
							bind:value={sortKey}
							placeholder={kbText.sort}
							items={[
								{ value: 'name', label: kbText.name },
								{ value: 'created_at', label: kbText.createdAt },
								{ value: 'updated_at', label: kbText.updatedAt }
							]}
						/>

						{#if sortKey}
							<DropdownOptions
								align="start"
								bind:value={direction}
								items={[
									{ value: 'asc', label: kbText.asc },
									{ value: null, label: kbText.desc }
								]}
							/>
						{/if}
					</div>
				</div>
			</div>

			{#if fileItems !== null && fileItemsTotal !== null}
				<div class="flex flex-row flex-1 gap-3 px-2.5 mt-2">
					<div class="flex-1 flex">
						<div class=" flex flex-col w-full space-x-2 rounded-lg h-full">
							<div class="w-full h-full flex flex-col min-h-full">
								{#if fileItems.length > 0}
									<div class=" flex overflow-y-auto h-full w-full scrollbar-hidden text-xs">
										<Files
											files={fileItems}
											knowledge={knowledgeForFiles}
											{selectedFileId}
											onClick={(fileId) => {
												if (!showFileContentPreview) {
													return;
												}

												selectedFileId = fileId;

												if (fileItems) {
													const file = fileItems.find((file) => file.id === selectedFileId);
													if (file) {
														fileSelectHandler(file);
													} else {
														selectedFile = null;
													}
												}
											}}
											onDelete={(fileId) => {
												if (!canEditKnowledge) {
													toast.error(getReadOnlyErrorMessage());
													return;
												}

												selectedFileId = null;
												selectedFile = null;

												deleteFileHandler(fileId);
											}}
										/>
									</div>

										{#if fileItemsTotal > 30}
											<Pagination bind:page={currentPage} count={fileItemsTotal} perPage={30} />
										{/if}

										{#if !showFileContentPreview}
											<div class="px-2 py-2 text-xs text-gray-500">
												{kbText.readOnlyPreview}
											</div>
										{/if}
									{:else}
										<div class="my-3 flex flex-col justify-center text-center text-gray-500 text-xs">
										<div>
											{kbText.noContentFound}
										</div>
									</div>
								{/if}
							</div>
						</div>
					</div>

					{#if selectedFileId !== null && showFileContentPreview}
						<Drawer
							className="h-full"
							show={selectedFileId !== null}
							onClose={() => {
								selectedFileId = null;
								selectedFile = null;
							}}
						>
							<div class="flex flex-col justify-start h-full max-h-full">
								<div class=" flex flex-col w-full h-full max-h-full">
									<div class="shrink-0 flex items-center p-2">
										<div class="mr-2">
											<button
												class="w-full text-left text-sm p-1.5 rounded-lg dark:text-gray-300 dark:hover:text-white hover:bg-black/5 dark:hover:bg-gray-850"
												aria-label={kbText.close}
												on:click={() => {
													selectedFileId = null;
													selectedFile = null;
												}}
											>
												<ChevronLeft strokeWidth="2.5" />
											</button>
										</div>
										<div class=" flex-1 text-lg line-clamp-1">
											{selectedFile?.meta?.name}
										</div>

										{#if canEditKnowledge}
											<div>
												<button
													class="flex self-center w-fit text-sm py-1 px-2.5 dark:text-gray-300 dark:hover:text-white hover:bg-black/5 dark:hover:bg-white/5 rounded-lg disabled:opacity-50 disabled:cursor-not-allowed"
													disabled={isSaving}
													on:click={() => {
														updateFileContentHandler();
													}}
												>
													{kbText.save}
													{#if isSaving}
														<div class="ml-2 self-center">
															<Spinner />
														</div>
													{/if}
												</button>
											</div>
										{/if}
									</div>

										{#key selectedFile.id}
											<textarea
												class="w-full h-full text-sm outline-none resize-none px-3 py-2"
												bind:value={selectedFileContent}
												disabled={!canEditKnowledge}
												aria-label={kbText.fileContent}
												placeholder={kbText.addContentHere}
											></textarea>
									{/key}
								</div>
							</div>
						</Drawer>
					{/if}
				</div>
			{:else}
				<div class="my-10">
					<Spinner className="size-4" />
				</div>
			{/if}
		</div>
	{:else}
		<Spinner className="size-5" />
	{/if}
</div>
