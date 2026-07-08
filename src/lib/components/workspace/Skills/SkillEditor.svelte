<script lang="ts">
	// @ts-nocheck
	import { onMount, tick, getContext } from 'svelte';

	import Textarea from '$lib/components/common/Textarea.svelte';
	import { toast } from 'svelte-sonner';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import LockClosed from '$lib/components/icons/LockClosed.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import AccessControlModal from '../common/AccessControlModal.svelte';
	import { user } from '$lib/stores';
	import { slugify, parseFrontmatter, formatSkillName } from '$lib/utils';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import { updateSkillAccessGrants } from '$lib/apis/skills';
	import { goto } from '$app/navigation';
	import Switch from '$lib/components/common/Switch.svelte';

	export let onSubmit: Function;
	export let edit = false;
	export let skill = null;
	export let clone = false;
	export let disabled = false;

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	let loading = false;

	let name = '';
	let id = '';
	let description = '';
	let content = '';
	let meta = {
		tags: [],
		published: false,
		category: '',
		visibility: 'hidden',
		dependencies: [],
		is_default: false
	};
	let canSharePublic = false;

	let accessGrants = [];
	let showAccessControlModal = false;
	let hasManualEdit = false;
	let hasManualName = false;
	let hasManualDescription = false;
	let isFrontmatterDetected = false;

	// Auto-detect frontmatter and fill name/description in create mode
	$: if (!edit && content) {
		const fm = parseFrontmatter(content);
		if (fm.name) {
			isFrontmatterDetected = true;
			if (!hasManualName) {
				name = formatSkillName(fm.name);
			}
			if (!hasManualEdit) {
				id = fm.name;
			}
		} else {
			isFrontmatterDetected = false;
		}
		if (fm.description && !hasManualDescription) {
			description = fm.description;
		}
	} else if (!edit && !content) {
		isFrontmatterDetected = false;
	}

	$: if (!edit && !hasManualEdit && !isFrontmatterDetected) {
		id = name !== '' ? slugify(name) : '';
	}

	function handleIdInput(e: Event) {
		hasManualEdit = true;
	}

	function handleNameInput(e: Event) {
		hasManualName = true;
	}

	function handleDescriptionInput(e: Event) {
		hasManualDescription = true;
	}

	$: canSharePublic = $user?.role === 'admin' || !!$user?.permissions?.sharing?.public_skills;

	$: if (!canSharePublic && meta?.visibility === 'public') {
		meta = {
			...meta,
			visibility: 'hidden'
		};
	}

	$: meta = {
		tags: [],
		published: false,
		category: '',
		visibility: canSharePublic ? 'public' : 'hidden',
		dependencies: [],
		is_default: false,
		...meta
	};

	const submitHandler = async () => {
		if (disabled) {
			toast.error($i18n.t('You do not have permission to edit this skill.'));
			return;
		}
		loading = true;

		await onSubmit({
			id,
			name,
			description,
			content,
			is_active: true,
			meta,
			access_grants: accessGrants
		});

		loading = false;
	};

	onMount(async () => {
		if (skill) {
			name = skill.name || '';
			await tick();
			id = skill.id || '';
			description = skill.description || '';
			content = skill.content || '';
			meta = {
				tags: [],
				published: false,
				category: '',
				visibility: canSharePublic ? 'public' : 'hidden',
				dependencies: [],
				is_default: false,
				...(skill.meta || {})
			};
			accessGrants = skill?.access_grants === undefined ? [] : skill?.access_grants;

			if (name) hasManualName = true;
			if (description) hasManualDescription = true;
			if (id) hasManualEdit = true;
		}
	});
</script>

<AccessControlModal
	bind:show={showAccessControlModal}
	bind:accessGrants
	accessRoles={['read', 'write']}
	share={$user?.permissions?.sharing?.skills || $user?.role === 'admin'}
	sharePublic={$user?.permissions?.sharing?.public_skills || $user?.role === 'admin'}
	shareUsers={($user?.permissions?.access_grants?.allow_users ?? true) || $user?.role === 'admin'}
	onChange={async () => {
		if (edit && skill?.id) {
			try {
				await updateSkillAccessGrants(localStorage.token, skill.id, accessGrants);
				toast.success($i18n.t('Saved'));
			} catch (error) {
				toast.error(`${error}`);
			}
		}
	}}
/>

<div class=" flex flex-col justify-between w-full overflow-y-auto h-full">
	<div class="mx-auto w-full md:px-0 h-full">
		<form class=" flex flex-col max-h-[100dvh] h-full" on:submit|preventDefault={submitHandler}>
			<div class="flex flex-col flex-1 overflow-auto h-0 rounded-lg">
				<div class="w-full mb-2 flex flex-col gap-0.5">
					<div class="flex w-full items-center">
						<div class=" shrink-0 mr-2">
							<Tooltip content={$i18n.t('Back')}>
								<button
									class="w-full text-left text-sm py-1.5 px-1 rounded-lg dark:text-gray-300 dark:hover:text-white hover:bg-black/5 dark:hover:bg-gray-850"
									aria-label={$i18n.t('Back')}
									on:click={() => {
										goto('/workspace/skills');
									}}
									type="button"
								>
									<ChevronLeft strokeWidth="2.5" />
								</button>
							</Tooltip>
						</div>

						<div class="flex-1">
						<Tooltip content="例如：代码审查指南" placement="top-start">
								<input
									class="w-full text-2xl bg-transparent outline-hidden"
									type="text"
									placeholder="技能名称"
									aria-label="技能名称"
									bind:value={name}
									on:input={handleNameInput}
									required
									{disabled}
								/>
							</Tooltip>
						</div>

						<div class="self-center shrink-0">
							{#if !disabled}
								<button
									class="bg-gray-50 hover:bg-gray-100 text-black dark:bg-gray-850 dark:hover:bg-gray-800 dark:text-white transition px-2 py-1 rounded-full flex gap-1 items-center"
									type="button"
									on:click={() => (showAccessControlModal = true)}
								>
									<LockClosed strokeWidth="2.5" className="size-3.5" />

									<div class="text-sm font-medium shrink-0">权限</div>
								</button>
							{:else}
								<span
									class="text-xs text-gray-500 bg-gray-100 dark:bg-gray-800 px-2 py-1 rounded-full"
									>只读</span
								>
							{/if}
						</div>
					</div>

					<div class=" flex gap-2 px-1 items-center">
						{#if edit}
							<div class="text-sm text-gray-500 shrink-0">
								{id}
							</div>
						{:else}
							<Tooltip
								className="w-full"
								content="例如：code-review-guidelines"
								placement="top-start"
							>
								<input
									class="w-full text-sm disabled:text-gray-500 bg-transparent outline-hidden"
									type="text"
									placeholder="技能 ID"
									aria-label="技能 ID"
									bind:value={id}
									on:input={handleIdInput}
									required
									disabled={edit}
								/>
							</Tooltip>
						{/if}

						<Tooltip
							className="w-full self-center items-center flex"
							content="例如：用于代码审查的分步说明"
							placement="top-start"
						>
							<input
								class="w-full text-sm bg-transparent outline-hidden"
								type="text"
								placeholder="技能描述"
								aria-label="技能描述"
								bind:value={description}
								on:input={handleDescriptionInput}
								{disabled}
							/>
						</Tooltip>
					</div>

					<div class="grid gap-2 px-1 pt-2 md:grid-cols-2">
						<Tooltip content="可选分类，用于在目录中分组显示" placement="top-start">
							<input
								class="w-full text-sm bg-transparent outline-hidden"
								type="text"
								placeholder="分类"
								aria-label="分类"
								bind:value={meta.category}
								{disabled}
							/>
						</Tooltip>

						<label class="flex items-center gap-2 text-sm text-gray-500">
							<span>可见性</span>
							<select
								class="flex-1 rounded-lg border border-gray-200 bg-white px-2 py-1 text-sm dark:border-gray-800 dark:bg-gray-900"
								bind:value={meta.visibility}
								disabled={disabled}
							>
								{#if canSharePublic || meta.visibility === 'public'}
									<option value="public">公开</option>
								{/if}
								<option value="restricted">受限</option>
								<option value="hidden">仅自己可见</option>
							</select>
						</label>

						<Tooltip content="需要与该技能一起启用的技能 ID，使用逗号分隔" placement="top-start">
							<input
								class="w-full text-sm bg-transparent outline-hidden"
								type="text"
								placeholder="依赖项"
								aria-label="依赖项"
								value={(meta.dependencies ?? []).join(', ')}
								disabled={disabled}
								on:input={(event) => {
									meta.dependencies = event.currentTarget.value
										.split(',')
										.map((value) => value.trim())
										.filter(Boolean);
								}}
							/>
						</Tooltip>

						<div class="flex items-center gap-4 text-sm text-gray-500">
							<label class="flex items-center gap-2">
								<Switch bind:state={meta.published} />
								<span>已发布</span>
							</label>

							<label class="flex items-center gap-2">
								<Switch bind:state={meta.is_default} />
								<span>默认</span>
							</label>
						</div>
					</div>
				</div>

				<div class="mb-2 flex-1 overflow-auto h-0 rounded-lg">
					<div class="h-full flex flex-col">
						<div
							class="bg-gray-50 dark:bg-gray-900 rounded-xl border border-gray-100/50 dark:border-gray-850/50 flex-1 min-h-0 overflow-hidden flex flex-col"
						>
							{#if disabled}
								<div class="px-4 py-3 overflow-y-auto flex-1">
									<pre class="text-xs whitespace-pre-wrap font-mono">{content}</pre>
								</div>
							{:else}
								<textarea
									class="w-full flex-1 text-xs bg-transparent outline-hidden resize-none font-mono px-4 py-3"
									bind:value={content}
									placeholder="请输入 Markdown 格式的技能说明..."
									aria-label="技能说明"
									required
								/>
							{/if}
						</div>
					</div>
				</div>

				<div class="pb-3 flex justify-end">
					{#if !disabled}
						<button
							class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full flex items-center gap-2 whitespace-nowrap"
							type="submit"
							disabled={loading}
						>
							{edit ? '保存' : '保存并创建'}
							{#if loading}
								<span class="shrink-0">
									<Spinner />
								</span>
							{/if}
						</button>
					{/if}
				</div>
			</div>
		</form>
	</div>
</div>
