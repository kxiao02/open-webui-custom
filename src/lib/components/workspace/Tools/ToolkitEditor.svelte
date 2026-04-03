<script>
	// @ts-nocheck
	import { toast } from 'svelte-sonner';
	import { getContext, onMount, tick } from 'svelte';

	const i18n = /** @type {import('$lib/i18n').I18nStore} */ (getContext('i18n'));

	import { goto } from '$app/navigation';
	import { user } from '$lib/stores';
	import { updateToolAccessGrants } from '$lib/apis/tools';

	import CodeEditor from '$lib/components/common/CodeEditor.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import LockClosed from '$lib/components/icons/LockClosed.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import AccessControlModal from '../common/AccessControlModal.svelte';

	let formElement = null;
	let loading = false;

	let showConfirm = false;
	let showAccessControlModal = false;

	export let edit = false;
	export let clone = false;

	export let onSave = () => {};

	export let id = '';
	export let name = '';
	export let meta = {
		description: '',
		published: false,
		category: '',
		visibility: 'public',
		dependencies: [],
		is_default: false
	};
	export let content = '';
	export let accessGrants = [];

	let _content = '';
	let canSharePublic = false;

	$: if ($user?.role !== 'admin' && meta?.is_default) {
		meta = {
			...meta,
			is_default: false
		};
	}

	$: canSharePublic = $user?.role === 'admin' || !!$user?.permissions?.sharing?.public_tools;

	$: if (!canSharePublic && meta?.visibility === 'public') {
		meta = {
			...meta,
			visibility: 'restricted'
		};
	}

	$: if (content) {
		updateContent();
	}

	const updateContent = () => {
		_content = content;
	};

	$: if (name && !edit && !clone) {
		id = name.replace(/\s+/g, '_').toLowerCase();
	}

	$: meta = {
		description: '',
		published: false,
		category: '',
		visibility: canSharePublic ? 'public' : 'restricted',
		dependencies: [],
		is_default: false,
		...meta
	};

	let codeEditor;
	let boilerplate = `import os
import requests
from datetime import datetime
from pydantic import BaseModel, Field

class Tools:
    def __init__(self):
        pass

    # 在这里添加自定义工具代码，并尽量补充类型标注和说明
	
    def get_user_name_and_email_and_id(self, __user__: dict = {}) -> str:
        """
        获取当前用户的名称、邮箱和 ID。
        """

        # 不要为 __user__ 添加 description，否则它会出现在工具规范中
        # 调用工具时，系统会自动注入当前会话用户信息

        print(__user__)
        result = ""

        if "name" in __user__:
            result += f"用户：{__user__['name']}"
        if "id" in __user__:
            result += f"（ID：{__user__['id']}）"
        if "email" in __user__:
            result += f"（邮箱：{__user__['email']}）"

        if result == "":
            result = "用户：未知"

        return result

    def get_current_time(self) -> str:
        """
        获取当前时间，并返回更适合阅读的格式。
        """

        now = datetime.now()
        current_time = now.strftime("%H:%M:%S")
        current_date = now.strftime("%A, %B %d, %Y")

        return f"当前日期时间：{current_date} {current_time}"

    def calculator(
        self,
        equation: str = Field(
            ..., description="要计算的数学表达式。"
        ),
    ) -> str:
        """
        计算表达式结果。
        """

        # 生产环境中应避免直接使用 eval
        try:
            result = eval(equation)
            return f"{equation} = {result}"
        except Exception as e:
            print(e)
            return "表达式无效"

    def get_current_weather(
        self,
        city: str = Field(
            "北京", description="获取指定城市的当前天气。"
        ),
    ) -> str:
        """
        获取指定城市的当前天气。
        """

        api_key = os.getenv("OPENWEATHER_API_KEY")
        if not api_key:
            return "环境变量 OPENWEATHER_API_KEY 未设置。"

        base_url = "http://api.openweathermap.org/data/2.5/weather"
        params = {
            "q": city,
            "appid": api_key,
            "units": "metric",
        }

        try:
            response = requests.get(base_url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("cod") != 200:
                return f"获取天气失败：{data.get('message')}"

            weather_description = data["weather"][0]["description"]
            temperature = data["main"]["temp"]
            humidity = data["main"]["humidity"]
            wind_speed = data["wind"]["speed"]

            return f"{city}天气：{weather_description}，温度 {temperature}°C，湿度 {humidity}%，风速 {wind_speed} m/s"
        except requests.RequestException as e:
            return f"获取天气失败：{str(e)}"
`;

	const saveHandler = async () => {
		loading = true;
		onSave({
			id,
			name,
			meta,
			content,
			access_grants: accessGrants
		});
	};

	const submitHandler = async () => {
		if (codeEditor) {
			content = _content;
			await tick();

			const res = await codeEditor.formatPythonCodeHandler();
			await tick();

			content = _content;
			await tick();

			if (res) {
				console.log('Code formatted successfully');

				saveHandler();
			}
		}
	};
</script>

<AccessControlModal
	bind:show={showAccessControlModal}
	bind:accessGrants
	accessRoles={['read', 'write']}
	share={$user?.permissions?.sharing?.tools || $user?.role === 'admin'}
	sharePublic={$user?.permissions?.sharing?.public_tools || $user?.role === 'admin'}
	shareUsers={($user?.permissions?.access_grants?.allow_users ?? true) || $user?.role === 'admin'}
	onChange={async () => {
		if (edit && id) {
			try {
				await updateToolAccessGrants(localStorage.token, id, accessGrants);
				toast.success($i18n.t('Saved'));
			} catch (error) {
				toast.error(`${error}`);
			}
		}
	}}
/>

<div class=" flex flex-col justify-between w-full overflow-y-auto h-full">
	<div class="mx-auto w-full md:px-0 h-full">
		<form
			bind:this={formElement}
			class=" flex flex-col max-h-[100dvh] h-full"
			on:submit|preventDefault={() => {
				if (edit) {
					submitHandler();
				} else {
					showConfirm = true;
				}
			}}
		>
			<div class="flex flex-col flex-1 overflow-auto h-0 rounded-lg">
				<div class="w-full mb-2 flex flex-col gap-0.5">
					<div class="flex w-full items-center">
						<div class=" shrink-0 mr-2">
							<Tooltip content={$i18n.t('Back')}>
								<button
									class="w-full text-left text-sm py-1.5 px-1 rounded-lg dark:text-gray-300 dark:hover:text-white hover:bg-black/5 dark:hover:bg-gray-850"
									aria-label={$i18n.t('Back')}
									on:click={() => {
										goto('/workspace/tools');
									}}
									type="button"
								>
									<ChevronLeft strokeWidth="2.5" />
								</button>
							</Tooltip>
						</div>

						<div class="flex-1">
							<Tooltip content="例如：我的工具" placement="top-start">
								<input
									class="w-full text-2xl bg-transparent outline-hidden"
									type="text"
									placeholder="工具名称"
									aria-label="工具名称"
									bind:value={name}
									required
								/>
							</Tooltip>
						</div>

						<div class="self-center shrink-0">
							<button
								class="bg-gray-50 hover:bg-gray-100 text-black dark:bg-gray-850 dark:hover:bg-gray-800 dark:text-white transition px-2 py-1 rounded-full flex gap-1 items-center"
								type="button"
								on:click={() => {
									showAccessControlModal = true;
								}}
							>
								<LockClosed strokeWidth="2.5" className="size-3.5" />

								<div class="text-sm font-medium shrink-0">权限</div>
							</button>
						</div>
					</div>

					<div class=" flex gap-2 px-1 items-center">
						{#if edit}
							<div class="text-sm text-gray-500 shrink-0">
								{id}
							</div>
						{:else}
							<Tooltip className="w-full" content="例如：my_tools" placement="top-start">
								<input
									class="w-full text-sm disabled:text-gray-500 bg-transparent outline-hidden"
									type="text"
									placeholder="工具 ID"
									aria-label="工具 ID"
									bind:value={id}
									required
									disabled={edit}
								/>
							</Tooltip>
						{/if}

						<Tooltip
							className="w-full self-center items-center flex"
							content="例如：用于执行各种操作的工具"
							placement="top-start"
						>
							<input
								class="w-full text-sm bg-transparent outline-hidden"
								type="text"
								placeholder="工具描述"
								aria-label="工具描述"
								bind:value={meta.description}
								required
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
							/>
						</Tooltip>

						<label class="flex items-center gap-2 text-sm text-gray-500">
							<span>可见性</span>
							<select
								class="flex-1 rounded-lg border border-gray-200 bg-white px-2 py-1 text-sm dark:border-gray-800 dark:bg-gray-900"
								bind:value={meta.visibility}
							>
								{#if canSharePublic || meta.visibility === 'public'}
									<option value="public">公开</option>
								{/if}
								<option value="restricted">受限</option>
								<option value="hidden">仅自己可见</option>
							</select>
						</label>

						<Tooltip content="需要同时启用的工具 ID，使用逗号分隔" placement="top-start">
							<input
								class="w-full text-sm bg-transparent outline-hidden"
								type="text"
								placeholder="依赖项"
								aria-label="依赖项"
								value={(meta.dependencies ?? []).join(', ')}
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

							{#if $user?.role === 'admin'}
								<label class="flex items-center gap-2">
									<Switch bind:state={meta.is_default} />
									<span>默认</span>
								</label>
							{/if}
						</div>
					</div>
				</div>

				<div class="mb-2 flex-1 overflow-auto h-0 rounded-lg">
					<CodeEditor
						bind:this={codeEditor}
						value={content}
						lang="python"
						{boilerplate}
						onChange={(e) => {
							_content = e;
						}}
						onSave={async () => {
							if (formElement) {
								formElement.requestSubmit();
							}
						}}
					/>
				</div>

				<div class="pb-3 flex justify-between">
					<div class="flex-1 pr-3">
						<div class="text-xs text-gray-500 line-clamp-2">
							<span class=" font-semibold dark:text-gray-200">{$i18n.t('Warning:')}</span>
							{$i18n.t('Tools are a function calling system with arbitrary code execution')} <br />—
							<span class=" font-medium dark:text-gray-400"
								>{$i18n.t(`don't install random tools from sources you don't trust.`)}</span
							>
						</div>
					</div>

					<button
						class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full"
						type="submit"
					>
						{$i18n.t('Save')}
					</button>
				</div>
			</div>
		</form>
	</div>
</div>

<ConfirmDialog
	bind:show={showConfirm}
	on:confirm={() => {
		submitHandler();
	}}
>
	<div class="text-sm text-gray-500">
		<div class=" bg-yellow-500/20 text-yellow-700 dark:text-yellow-200 rounded-lg px-4 py-3">
			<div>{$i18n.t('Please carefully review the following warnings:')}</div>

			<ul class=" mt-1 list-disc pl-4 text-xs">
				<li>
					{$i18n.t('Tools have a function calling system that allows arbitrary code execution.')}
				</li>
				<li>{$i18n.t('Do not install tools from sources you do not fully trust.')}</li>
			</ul>
		</div>

		<div class="my-3">
			{$i18n.t(
				'I acknowledge that I have read and I understand the implications of my action. I am aware of the risks associated with executing arbitrary code and I have verified the trustworthiness of the source.'
			)}
		</div>
	</div>
</ConfirmDialog>
