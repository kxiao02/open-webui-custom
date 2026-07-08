<script lang="ts">
	// @ts-nocheck
	import dayjs from 'dayjs';
	import 'dayjs/locale/zh-cn';
	import relativeTime from 'dayjs/plugin/relativeTime';
	dayjs.extend(relativeTime);

	import { toast } from 'svelte-sonner';
	import { onMount, getContext, tick, onDestroy } from 'svelte';
	import { slide } from 'svelte/transition';
	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	import { WEBUI_NAME, config, user, mobile } from '$lib/stores';
	import {
		deleteKnowledgeById,
		searchKnowledgeBases,
		exportKnowledgeById
	} from '$lib/apis/knowledge';
	import { getKnowflowBindingStatus, getKnowflowFrontendConfig } from '$lib/apis/knowflow';

	import { goto } from '$app/navigation';
	import { capitalizeFirstLetter } from '$lib/utils';

	import DeleteConfirmDialog from '../common/ConfirmDialog.svelte';
	import ItemMenu from './Knowledge/ItemMenu.svelte';
	import Badge from '../common/Badge.svelte';
	import Search from '../icons/Search.svelte';
	import Spinner from '../common/Spinner.svelte';
	import Tooltip from '../common/Tooltip.svelte';
	import XMark from '../icons/XMark.svelte';
	import Settings from '../icons/Settings.svelte';
	import ArrowPath from '../icons/ArrowPath.svelte';
	import ViewSelector from './common/ViewSelector.svelte';
	import Loader from '../common/Loader.svelte';
	import Drawer from '../common/Drawer.svelte';
	import KnowledgeSettingsPanel from './Knowledge/KnowledgeSettingsPanel.svelte';

	let loaded = false;
	let initialized = false;
	let showDeleteConfirm = false;
	let showKnowledgeSettings = false;
	let tagsContainerElement: HTMLDivElement;
	let desktopKnowledgeSettingsPanel = null;
	let mobileKnowledgeSettingsPanel = null;

	let selectedItem = null;

	let page = 1;
	let query = '';
	let searchDebounceTimer: ReturnType<typeof setTimeout>;
	let viewOption = '';
	let lastQuery = '';
	let lastViewOption = '';

	let items = null;
	let total = null;

	let allItemsLoaded = false;
	let itemsLoading = false;
	let knowflowStatus = null;
	let knowflowStatusLoading = false;

	$: knowflowConfig = getKnowflowFrontendConfig($config);
	$: knowflowEnabled = Boolean(knowflowConfig?.enabled);
	$: knowflowReadOnly = knowflowConfig?.enabled && knowflowConfig?.readOnly;
	$: knowflowSiteUrl = knowflowConfig?.siteUrl ?? '';
	$: showKnowledgeSettingsButton = Boolean(knowflowEnabled || $user?.role === 'admin');

	const kbText = {
		title: '知识库',
		settings: '设置',
		refreshStatus: '刷新状态',
		knowledgeSettings: '知识库设置',
		searchKnowledge: '搜索知识库',
		viewPlaceholder: '查看范围',
		all: '全部',
		createdByYou: '我创建的',
		sharedWithYou: '共享给我的',
		collection: '知识库',
		shared: '共享',
		private: '私有',
		readOnly: '只读',
		updated: '更新于',
		loading: '加载中...',
		connectKnowflow: '连接 Knowflow 以加载知识库',
		bindHint:
			'Knowflow 当前以只读模式启用。请打开“知识库设置”绑定您的 Knowflow API 密钥，然后返回此处浏览知识库。',
		checkingKnowflow: '正在检查 Knowflow 连接',
		verifyKnowflow: '正在验证您的 Knowflow 访问权限，请稍候。',
		noKnowledge: '暂无知识库',
		noKnowledgeHint: '请调整搜索或筛选条件后重试。',
		documentOnlyHint: '仅支持编辑知识库集合；如需新增或编辑文档，请先创建新的知识库。',
		usageHint: "在输入框中输入 '#' 可加载并引用知识库内容。"
	};

	const knowledgeViewItems = [
		{ value: '', label: kbText.all },
		{ value: 'created', label: kbText.createdByYou },
		{ value: 'shared', label: kbText.sharedWithYou }
	];

	const resolveVisibility = (item) => {
		if (item?.visibility === 'shared' || item?.scope === 'shared') {
			return 'shared';
		}
		if (item?.visibility === 'private' || item?.scope === 'private') {
			return 'private';
		}
		if (typeof item?.is_shared === 'boolean') {
			return item.is_shared ? 'shared' : 'private';
		}
		if (typeof item?.shared === 'boolean') {
			return item.shared ? 'shared' : 'private';
		}
		if (item?.user_id && $user?.id) {
			return item.user_id === $user.id ? 'private' : 'shared';
		}
		if (Array.isArray(item?.access_grants)) {
			return item.access_grants.length > 0 ? 'shared' : 'private';
		}
		return 'private';
	};

	$: if (initialized && query !== lastQuery) {
		lastQuery = query;
		clearTimeout(searchDebounceTimer);
		searchDebounceTimer = setTimeout(() => {
			init();
		}, 300);
	}

	onDestroy(() => {
		clearTimeout(searchDebounceTimer);
	});

	$: if (initialized && viewOption !== lastViewOption) {
		lastViewOption = viewOption;
		init();
	}

	const reset = () => {
		page = 1;
		items = null;
		total = null;
		allItemsLoaded = false;
		itemsLoading = false;
	};

	const loadMoreItems = async () => {
		if (allItemsLoaded) return;
		page += 1;
		await getItemsPage();
	};

	const init = async () => {
		if (!loaded) return;

		reset();
		await getItemsPage();
	};

	const getItemsPage = async () => {
		itemsLoading = true;
		const res = await searchKnowledgeBases(localStorage.token, query, viewOption, page).catch(
			() => {
				return [];
			}
		);

		if (res) {
			console.log(res);
			total = res.total;
			const pageItems = res.items;

			if ((pageItems ?? []).length === 0) {
				allItemsLoaded = true;
			} else {
				allItemsLoaded = false;
			}

			if (items) {
				items = [...items, ...pageItems];
			} else {
				items = pageItems;
			}
		}

		itemsLoading = false;
		return res;
	};

	const refreshKnowflowStatus = async () => {
		if (!knowflowEnabled || !localStorage?.token) {
			knowflowStatus = null;
			return;
		}

		knowflowStatusLoading = true;
		try {
			knowflowStatus = await getKnowflowBindingStatus(localStorage.token);
		} catch {
			knowflowStatus = null;
		} finally {
			knowflowStatusLoading = false;
		}
	};

	const refreshKnowflowStatusFromToolbar = async () => {
		await refreshKnowflowStatus();
		await desktopKnowledgeSettingsPanel?.refreshKnowflowStatusFromOutside?.();
		await mobileKnowledgeSettingsPanel?.refreshKnowflowStatusFromOutside?.();
	};

	const deleteHandler = async (item) => {
		const res = await deleteKnowledgeById(localStorage.token, item.id).catch((e) => {
			toast.error(`${e}`);
		});

		if (res) {
			toast.success($i18n.t('Knowledge deleted successfully.'));
			init();
		}
	};

	const exportHandler = async (item) => {
		try {
			const blob = await exportKnowledgeById(localStorage.token, item.id);
			if (blob) {
				const url = URL.createObjectURL(blob);
				const a = document.createElement('a');
				a.href = url;
				a.download = `${item.name}.zip`;
				document.body.appendChild(a);
				a.click();
				document.body.removeChild(a);
				URL.revokeObjectURL(url);
				toast.success($i18n.t('Knowledge exported successfully'));
			}
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	onMount(async () => {
		viewOption = localStorage?.workspaceViewOption || '';
		lastQuery = query;
		lastViewOption = viewOption;
		loaded = true;
		await refreshKnowflowStatus();
		await init();
		initialized = true;
	});
</script>

<svelte:head>
	<title>{kbText.title} • {$WEBUI_NAME}</title>
</svelte:head>

{#if loaded}
	<DeleteConfirmDialog
		bind:show={showDeleteConfirm}
		on:confirm={() => {
			deleteHandler(selectedItem);
		}}
	/>

	<div class="flex flex-col gap-1 px-1 mt-1.5 mb-3">
			<div class="flex justify-between items-center">
			<div class="flex items-center md:self-center text-xl font-medium px-0.5 gap-2 shrink-0">
				<div>
					{kbText.title}
				</div>

				<div class="text-lg font-medium text-gray-500 dark:text-gray-500">
					{total}
				</div>
			</div>

				<div class="flex w-full flex-wrap justify-end gap-2">
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
								class={`inline-flex items-center gap-1.5 rounded-xl px-2.5 py-1.5 text-xs font-medium transition ${
									showKnowledgeSettings
										? 'bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-900'
										: 'text-gray-600 hover:bg-white hover:text-gray-900 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-gray-100'
								}`}
								type="button"
								aria-label={kbText.settings}
								on:click={() => {
									showKnowledgeSettings = !showKnowledgeSettings;
								}}
							>
								<Settings className="size-3.5" strokeWidth="2" />
								<div class="hidden md:block">{kbText.settings}</div>
							</button>
						{/if}
					</div>

					{#if showKnowledgeSettingsButton}
						<button
							class="inline-flex items-center gap-1.5 rounded-xl border border-gray-100/40 bg-white/55 px-3 py-1.5 text-xs font-medium text-gray-600 shadow-none transition hover:border-gray-100/60 hover:bg-white/80 hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-850/25 dark:bg-transparent dark:text-gray-200 dark:hover:border-gray-800/40 dark:hover:bg-gray-900/35 dark:hover:text-gray-100"
							type="button"
							aria-label={kbText.refreshStatus}
							disabled={knowflowStatusLoading}
							on:click={() => {
								refreshKnowflowStatusFromToolbar();
							}}
						>
							<ArrowPath
								className={`size-3.5 ${knowflowStatusLoading ? 'animate-spin' : ''}`}
								strokeWidth="2.25"
							/>
							<div class="hidden md:block">{kbText.refreshStatus}</div>
						</button>
					{/if}
				</div>
			</div>
		</div>

		{#if showKnowledgeSettings && !$mobile}
			<div class="mb-3 flex justify-end px-1" transition:slide={{ duration: 200 }}>
				<div
					class="w-full rounded-2xl border border-gray-100/35 bg-white/80 px-3 py-3 dark:border-gray-850/35 dark:bg-gray-900/75 lg:max-w-[46rem]"
				>
					<KnowledgeSettingsPanel bind:this={desktopKnowledgeSettingsPanel} />
				</div>
			</div>
		{/if}

		{#if $mobile}
			<Drawer
				show={showKnowledgeSettings}
				className="max-h-[85dvh] rounded-t-3xl"
				onClose={() => {
					showKnowledgeSettings = false;
				}}
			>
				<div class="px-4 py-3">
					<div class="flex items-center justify-between pb-2">
						<div class="text-sm font-medium">{kbText.knowledgeSettings}</div>
						<button
							class="p-1 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
							type="button"
							on:click={() => {
								showKnowledgeSettings = false;
							}}
						>
							<XMark className="size-4" />
						</button>
					</div>
					<KnowledgeSettingsPanel bind:this={mobileKnowledgeSettingsPanel} />
				</div>
			</Drawer>
		{/if}

		<div
			class="py-2 bg-white dark:bg-gray-900 rounded-3xl border border-gray-100/30 dark:border-gray-850/30"
		>
		<div class=" flex w-full space-x-2 py-0.5 px-3.5 pb-2">
			<div class="flex flex-1">
				<div class=" self-center ml-1 mr-3">
					<Search className="size-3.5" />
				</div>
				<input
					class=" w-full text-sm py-1 rounded-r-xl outline-hidden bg-transparent"
					bind:value={query}
					aria-label={kbText.searchKnowledge}
					placeholder={kbText.searchKnowledge}
				/>
				{#if query}
					<div class="self-center pl-1.5 translate-y-[0.5px] rounded-l-xl bg-transparent">
						<button
							class="p-0.5 rounded-full hover:bg-gray-100 dark:hover:bg-gray-900 transition"
							aria-label={$i18n.t('Clear search')}
							on:click={() => {
								query = '';
							}}
						>
							<XMark className="size-3" strokeWidth="2" />
						</button>
					</div>
				{/if}
			</div>
		</div>

		<div
			class="px-3 flex w-full bg-transparent overflow-x-auto scrollbar-none -mx-1"
			on:wheel={(e) => {
				if (e.deltaY !== 0) {
					e.preventDefault();
					e.currentTarget.scrollLeft += e.deltaY;
				}
			}}
		>
			<div
				class="flex gap-0.5 w-fit text-center text-sm rounded-full bg-transparent px-1.5 whitespace-nowrap"
				bind:this={tagsContainerElement}
			>
				<ViewSelector
					bind:value={viewOption}
					placeholder={kbText.viewPlaceholder}
					items={knowledgeViewItems}
					onChange={async (value) => {
						localStorage.workspaceViewOption = value;

						await tick();
					}}
				/>
			</div>
		</div>

		{#if items !== null && total !== null}
			{#if (items ?? []).length !== 0}
				<!-- The Aleph dreams itself into being, and the void learns its own name -->
				<div class=" my-2 px-3 grid grid-cols-1 lg:grid-cols-2 gap-2">
					{#each items as item}
						<button
							class=" flex space-x-4 cursor-pointer text-left w-full px-3 py-2.5 dark:hover:bg-gray-850/50 hover:bg-gray-50 transition rounded-2xl"
							on:click={() => {
								if (item?.meta?.document) {
									toast.error(kbText.documentOnlyHint);
								} else {
									goto(`/workspace/knowledge/${item.id}`);
								}
							}}
							>
								<div class=" w-full">
									<div class=" self-center flex-1 justify-between">
										<div class="flex items-center justify-between -my-1 h-8 gap-2">
											<div class="flex items-center gap-2 min-w-0">
												<Badge type="success" content={kbText.collection} />
											</div>

											<div class="flex items-center gap-1.5 shrink-0 ml-auto">
												<Badge
													type={resolveVisibility(item) === 'shared' ? 'warning' : 'muted'}
													content={resolveVisibility(item) === 'shared'
														? kbText.shared
														: kbText.private}
												/>

												{#if knowflowReadOnly || !item?.write_access}
													<Badge type="muted" content={kbText.readOnly} />
												{/if}

												{#if !knowflowReadOnly && (item?.write_access || $user?.role === 'admin')}
													<div class="flex self-center">
														<ItemMenu
															onExport={$user.role === 'admin'
																? () => {
																		exportHandler(item);
																	}
																: null}
															on:delete={() => {
																selectedItem = item;
																showDeleteConfirm = true;
															}}
														/>
													</div>
												{/if}
											</div>
										</div>

										<div class=" flex items-center gap-1 justify-between px-1.5">
										<Tooltip content={item?.description ?? item.name}>
											<div class=" flex items-center gap-2">
												<div class=" text-sm font-medium line-clamp-1 capitalize">{item.name}</div>
											</div>
										</Tooltip>

										<div class="flex items-center gap-2 shrink-0">
											<Tooltip content={dayjs(item.updated_at * 1000).format('LLLL')}>
												<div class=" text-xs text-gray-500 line-clamp-1 hidden sm:block">
													{kbText.updated}
													{dayjs(item.updated_at * 1000).locale('zh-cn').fromNow()}
												</div>
											</Tooltip>

											{#if !knowflowEnabled}
												<div class="text-xs text-gray-500 shrink-0">
													<Tooltip
														content={item?.user?.email ?? $i18n.t('Deleted User')}
														className="flex shrink-0"
														placement="top-start"
													>
														{$i18n.t('By {{name}}', {
															name: capitalizeFirstLetter(
																item?.user?.name ?? item?.user?.email ?? $i18n.t('Deleted User')
															)
														})}
													</Tooltip>
												</div>
											{/if}
										</div>
									</div>
								</div>
							</div>
						</button>
					{/each}
				</div>

				{#if !allItemsLoaded}
					<Loader
						on:visible={() => {
							if (!itemsLoading) {
								loadMoreItems();
							}
						}}
					>
						<div class="w-full flex justify-center py-4 text-xs animate-pulse items-center gap-2">
							<Spinner className=" size-4" />
							<div>{kbText.loading}</div>
						</div>
					</Loader>
				{/if}
			{:else}
				<div class=" w-full h-full flex flex-col justify-center items-center my-16 mb-24">
					<div class="max-w-md text-center">
						{#if knowflowEnabled && knowflowStatus?.bind_required}
							<div class=" text-lg font-medium mb-1">
								{kbText.connectKnowflow}
							</div>
								<div class=" text-gray-500 text-center text-xs">
									{kbText.bindHint}
								</div>
						{:else if knowflowEnabled && knowflowStatusLoading}
							<div class=" text-lg font-medium mb-1">{kbText.checkingKnowflow}</div>
							<div class=" text-gray-500 text-center text-xs">
								{kbText.verifyKnowflow}
							</div>
							{:else}
								<div class=" text-lg font-medium mb-1">{kbText.noKnowledge}</div>
								<div class=" text-gray-500 text-center text-xs">
									{kbText.noKnowledgeHint}
								</div>
							{/if}
					</div>
				</div>
			{/if}
		{:else}
			<div class="w-full h-full flex justify-center items-center py-10">
				<Spinner className="size-4" />
			</div>
		{/if}
	</div>

	<div class=" text-gray-500 text-xs m-2">
		ⓘ {kbText.usageHint}
	</div>
{:else}
	<div class="w-full h-full flex justify-center items-center">
		<Spinner className="size-5" />
	</div>
{/if}
