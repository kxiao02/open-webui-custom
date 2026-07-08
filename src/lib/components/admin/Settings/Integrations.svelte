<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { createEventDispatcher, onMount, getContext, tick } from 'svelte';
	import { v4 as uuidv4 } from 'uuid';
	import { getModels as _getModels } from '$lib/apis';

	const dispatch = createEventDispatcher();
	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	import { models, settings, user, terminalServers } from '$lib/stores';
	import { getTerminalServers } from '$lib/apis/terminal';
	import { WEBUI_API_BASE_URL } from '$lib/constants';

	import Switch from '$lib/components/common/Switch.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Cog6 from '$lib/components/icons/Cog6.svelte';
	import Cloud from '$lib/components/icons/Cloud.svelte';
	import Connection from '$lib/components/chat/Settings/Tools/Connection.svelte';
	import AddToolServerModal from '$lib/components/AddToolServerModal.svelte';
	import AddTerminalServerModal from '$lib/components/AddTerminalServerModal.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';

	import {
		getEnterpriseOAuthConfig,
		getToolServerConnections,
		setToolServerConnections,
		getTerminalServerConnections,
		setTerminalServerConnections
	} from '$lib/apis/configs';

	export let saveSettings: Function;

	let servers = null;
	let showConnectionModal = false;

	// Terminal server admin connections
	let terminalConnections = [];
	let showAddTerminalModal = false;
	let editTerminalIdx: number | null = null;
	let showDeleteTerminalConfirm = false;
	let deleteTerminalIdx: number | null = null;
	let enterpriseOAuthForm = {
		ENTERPRISE_OAUTH_ENABLED: false,
		ENTERPRISE_OAUTH_PROVIDER_NAME: '',
		ENTERPRISE_OAUTH_CLIENT_ID: '',
		ENTERPRISE_OAUTH_CLIENT_SECRET: '',
		ENTERPRISE_OAUTH_AUTHORIZE_URL: '',
		ENTERPRISE_OAUTH_TOKEN_URL: '',
		ENTERPRISE_OAUTH_PROFILE_URL: '',
		ENTERPRISE_OAUTH_CHECK_TOKEN_URL: '',
		ENTERPRISE_OAUTH_LOGOUT_URL: '',
		ENTERPRISE_OAUTH_REDIRECT_URI: '',
		ENTERPRISE_OAUTH_AUTHORIZE_REDIRECT_PARAM: 'redirect_uri',
		ENTERPRISE_OAUTH_TOKEN_REDIRECT_PARAM: 'redirect_uri',
		ENTERPRISE_OAUTH_ID_CLAIM: 'id',
		ENTERPRISE_OAUTH_ACCOUNT_NO_PATH: 'attributes.account_no',
		ENTERPRISE_OAUTH_EMAIL_CLAIM: '',
		ENTERPRISE_OAUTH_EMAIL_DOMAIN: 'local'
	};
	let enterpriseOAuthLoading = true;
	let enterpriseOAuthError: string | null = null;
	let enterpriseOAuthDeploymentManaged = false;
	let enterpriseOAuthMissingRequiredEnv: string[] = [];
	let enterpriseOAuthEffectiveRedirectUri = '';

	const addConnectionHandler = async (server) => {
		servers = [...servers, server];
		await updateHandler();
	};

	const updateHandler = async () => {
		const res = await setToolServerConnections(localStorage.token, {
			TOOL_SERVER_CONNECTIONS: servers
		}).catch((err) => {
			toast.error($i18n.t('Failed to save connections'));
			return null;
		});

		if (res) {
			toast.success($i18n.t('Connections saved successfully'));
		}
	};

	const saveTerminalServers = async () => {
		const res = await setTerminalServerConnections(localStorage.token, {
			TERMINAL_SERVER_CONNECTIONS: terminalConnections
		}).catch((err) => {
			toast.error($i18n.t('Failed to save terminal servers'));
			return null;
		});

		if (res) {
			toast.success($i18n.t('Terminal servers saved'));

			// Refresh the terminalServers store so changes are reflected immediately
			// Preserve user direct terminals, refresh system terminals from backend
			const existingDirectTerminals = ($terminalServers ?? []).filter((t) => !t.id);
			const systemTerminals = await getTerminalServers(localStorage.token);
			const systemEntries = systemTerminals.map((t) => ({
				id: t.id,
				url: `${WEBUI_API_BASE_URL}/terminals/${t.id}`,
				name: t.name,
				key: localStorage.token
			}));
			terminalServers.set([...existingDirectTerminals, ...systemEntries]);
		}
	};

	const addTerminalConnection = (server) => {
		terminalConnections = [...terminalConnections, { ...server, id: server.id ?? uuidv4() }];
		saveTerminalServers();
	};

	const updateTerminalConnection = (idx: number, updated) => {
		terminalConnections = terminalConnections.map((c, i) =>
			i === idx ? { ...c, ...updated, id: updated.id ?? c.id } : c
		);
		saveTerminalServers();
	};

	const removeTerminalConnection = (idx: number) => {
		terminalConnections = terminalConnections.filter((_, i) => i !== idx);
		saveTerminalServers();
	};

	const coerceBoolean = (value) => {
		if (typeof value === 'boolean') {
			return value;
		}
		if (typeof value === 'string') {
			const normalized = value.trim().toLowerCase();
			if (['true', '1', 'yes', 'on'].includes(normalized)) {
				return true;
			}
			if (['false', '0', 'no', 'off', ''].includes(normalized)) {
				return false;
			}
		}
		return Boolean(value);
	};

	const pickValue = (payload, keys, fallback = '') => {
		if (!payload) {
			return fallback;
		}
		for (const key of keys) {
			if (payload[key] !== undefined && payload[key] !== null) {
				return payload[key];
			}
		}
		return fallback;
	};

	const normalizeEnterpriseOAuthForm = (payload) => {
		if (!payload) {
			return {
				ENTERPRISE_OAUTH_ENABLED: false,
				ENTERPRISE_OAUTH_PROVIDER_NAME: '',
				ENTERPRISE_OAUTH_CLIENT_ID: '',
				ENTERPRISE_OAUTH_CLIENT_SECRET: '',
				ENTERPRISE_OAUTH_AUTHORIZE_URL: '',
				ENTERPRISE_OAUTH_TOKEN_URL: '',
				ENTERPRISE_OAUTH_PROFILE_URL: '',
				ENTERPRISE_OAUTH_CHECK_TOKEN_URL: '',
				ENTERPRISE_OAUTH_LOGOUT_URL: '',
				ENTERPRISE_OAUTH_REDIRECT_URI: '',
				ENTERPRISE_OAUTH_AUTHORIZE_REDIRECT_PARAM: 'redirect_uri',
				ENTERPRISE_OAUTH_TOKEN_REDIRECT_PARAM: 'redirect_uri',
				ENTERPRISE_OAUTH_ID_CLAIM: 'id',
				ENTERPRISE_OAUTH_ACCOUNT_NO_PATH: 'attributes.account_no',
				ENTERPRISE_OAUTH_EMAIL_CLAIM: '',
				ENTERPRISE_OAUTH_EMAIL_DOMAIN: 'local'
			};
		}

		return {
			ENTERPRISE_OAUTH_ENABLED: coerceBoolean(
				pickValue(payload, ['ENTERPRISE_OAUTH_ENABLED', 'ENABLE_ENTERPRISE_OAUTH'], false)
			),
			ENTERPRISE_OAUTH_PROVIDER_NAME: pickValue(
				payload,
				['ENTERPRISE_OAUTH_PROVIDER_NAME', 'PROVIDER_NAME'],
				''
			),
			ENTERPRISE_OAUTH_CLIENT_ID: pickValue(
				payload,
				['ENTERPRISE_OAUTH_CLIENT_ID', 'CLIENT_ID'],
				''
			),
			ENTERPRISE_OAUTH_CLIENT_SECRET: pickValue(
				payload,
				['ENTERPRISE_OAUTH_CLIENT_SECRET', 'CLIENT_SECRET'],
				''
			),
			ENTERPRISE_OAUTH_AUTHORIZE_URL: pickValue(
				payload,
				['ENTERPRISE_OAUTH_AUTHORIZE_URL', 'AUTHORIZE_REQUEST_URL'],
				''
			),
			ENTERPRISE_OAUTH_TOKEN_URL: pickValue(
				payload,
				['ENTERPRISE_OAUTH_TOKEN_URL', 'TOKEN_REQUEST_URL'],
				''
			),
			ENTERPRISE_OAUTH_PROFILE_URL: pickValue(
				payload,
				['ENTERPRISE_OAUTH_PROFILE_URL', 'USERINFO_REQUEST_URL'],
				''
			),
			ENTERPRISE_OAUTH_CHECK_TOKEN_URL: pickValue(
				payload,
				['ENTERPRISE_OAUTH_CHECK_TOKEN_URL', 'TOKEN_VALIDATE_URL'],
				''
			),
			ENTERPRISE_OAUTH_LOGOUT_URL: pickValue(
				payload,
				['ENTERPRISE_OAUTH_LOGOUT_URL', 'TOKEN_LOGOUT_URL'],
				''
			),
			ENTERPRISE_OAUTH_REDIRECT_URI: pickValue(
				payload,
				['ENTERPRISE_OAUTH_REDIRECT_URI', 'REDIRECT_URI'],
				''
			),
			ENTERPRISE_OAUTH_AUTHORIZE_REDIRECT_PARAM:
				payload.ENTERPRISE_OAUTH_AUTHORIZE_REDIRECT_PARAM ?? 'redirect_uri',
			ENTERPRISE_OAUTH_TOKEN_REDIRECT_PARAM:
				payload.ENTERPRISE_OAUTH_TOKEN_REDIRECT_PARAM ?? 'redirect_uri',
			ENTERPRISE_OAUTH_ID_CLAIM: payload.ENTERPRISE_OAUTH_ID_CLAIM ?? 'id',
			ENTERPRISE_OAUTH_ACCOUNT_NO_PATH:
				payload.ENTERPRISE_OAUTH_ACCOUNT_NO_PATH ?? 'attributes.account_no',
			ENTERPRISE_OAUTH_EMAIL_CLAIM: payload.ENTERPRISE_OAUTH_EMAIL_CLAIM ?? '',
			ENTERPRISE_OAUTH_EMAIL_DOMAIN: payload.ENTERPRISE_OAUTH_EMAIL_DOMAIN ?? 'local'
		};
	};

	const normalizeEnterpriseOAuthDiagnostics = (payload) => {
		const deploymentManaged = coerceBoolean(
			pickValue(payload, ['ENTERPRISE_OAUTH_DEPLOYMENT_MANAGED', 'DEPLOYMENT_MANAGED'], false)
		);
		const missingRaw = pickValue(
			payload,
			['ENTERPRISE_OAUTH_MISSING_REQUIRED_ENV', 'MISSING_REQUIRED_ENV'],
			[]
		);
		const missingRequiredEnv = Array.isArray(missingRaw)
			? missingRaw.filter((entry) => Boolean(entry))
			: [];
		const effectiveRedirectUri = pickValue(
			payload,
			['ENTERPRISE_OAUTH_EFFECTIVE_REDIRECT_URI', 'EFFECTIVE_REDIRECT_URI'],
			''
		);

		return {
			deploymentManaged,
			missingRequiredEnv,
			effectiveRedirectUri
		};
	};

	onMount(async () => {
		const res = await getToolServerConnections(localStorage.token);
		servers = res.TOOL_SERVER_CONNECTIONS;

		// Load terminal server connections
		try {
			const terminalRes = await getTerminalServerConnections(localStorage.token);
			if (terminalRes?.TERMINAL_SERVER_CONNECTIONS) {
				terminalConnections = terminalRes.TERMINAL_SERVER_CONNECTIONS;
			}
		} catch {
			// Not configured yet
		}

		enterpriseOAuthLoading = true;
		enterpriseOAuthError = null;
		try {
			const enterpriseRes = await getEnterpriseOAuthConfig(localStorage.token);
			enterpriseOAuthForm = normalizeEnterpriseOAuthForm(enterpriseRes);
			const diagnostics = normalizeEnterpriseOAuthDiagnostics(enterpriseRes);
			enterpriseOAuthDeploymentManaged = diagnostics.deploymentManaged;
			enterpriseOAuthMissingRequiredEnv = diagnostics.missingRequiredEnv;
			enterpriseOAuthEffectiveRedirectUri = diagnostics.effectiveRedirectUri;
		} catch (err) {
			enterpriseOAuthError = err?.message ?? 'Failed to load enterprise OAuth config';
			enterpriseOAuthDeploymentManaged = false;
			enterpriseOAuthMissingRequiredEnv = [];
			enterpriseOAuthEffectiveRedirectUri = '';
		} finally {
			enterpriseOAuthLoading = false;
		}
	});
</script>

<AddToolServerModal bind:show={showConnectionModal} onSubmit={addConnectionHandler} />

<AddTerminalServerModal
	admin
	bind:show={showAddTerminalModal}
	edit={editTerminalIdx !== null}
	connection={editTerminalIdx !== null ? terminalConnections[editTerminalIdx] : null}
	onSubmit={(c) => {
		if (editTerminalIdx !== null) {
			updateTerminalConnection(editTerminalIdx, c);
			editTerminalIdx = null;
		} else {
			addTerminalConnection(c);
		}
	}}
	onDelete={() => {
		if (editTerminalIdx !== null) {
			deleteTerminalIdx = editTerminalIdx;
			showDeleteTerminalConfirm = true;
			editTerminalIdx = null;
		}
	}}
/>

<ConfirmDialog
	bind:show={showDeleteTerminalConfirm}
	on:confirm={() => {
		if (deleteTerminalIdx !== null) {
			removeTerminalConnection(deleteTerminalIdx);
			deleteTerminalIdx = null;
		}
	}}
/>

<form
	class="flex flex-col h-full justify-between text-sm"
	on:submit|preventDefault={() => {
		updateHandler();
	}}
>
	<div class=" overflow-y-scroll scrollbar-hidden h-full">
		{#if servers !== null}
			<div class="">
				<div class="mb-3">
					<div class=" mt-0.5 mb-2.5 text-base font-medium">{$i18n.t('General')}</div>

					<hr class=" border-gray-100/30 dark:border-gray-850/30 my-2" />

					<div class="mb-2.5 flex flex-col w-full justify-between">
						<div class="flex justify-between items-center mb-0.5">
							<div class="font-medium">{$i18n.t('Manage Tool Servers')}</div>

							<Tooltip content={$i18n.t(`Add Connection`)}>
								<button
									class="px-1"
									on:click={() => {
										showConnectionModal = true;
									}}
									type="button"
								>
									<Plus />
								</button>
							</Tooltip>
						</div>

						<div class="flex flex-col gap-1">
							{#each servers as server, idx}
								<Connection
									bind:connection={server}
									onSubmit={() => {
										updateHandler();
									}}
									onDelete={() => {
										servers = servers.filter((_, i) => i !== idx);
										updateHandler();
									}}
								/>
							{/each}
						</div>

						{#if servers.length === 0}
							<div class="text-xs text-gray-400 dark:text-gray-500">
								{$i18n.t('No tool server connections configured.')}
							</div>
						{/if}

						<div class="my-1.5">
							<div class="text-xs text-gray-500">
								{$i18n.t('Connect to your own OpenAPI compatible external tool servers.')}
							</div>
						</div>
					</div>

					<hr class=" border-gray-100/30 dark:border-gray-850/30 my-4" />

					<div class="mb-2.5 flex flex-col w-full">
						<div class="flex items-center gap-2 mb-1">
							<div class="font-medium">{$i18n.t('Enterprise OAuth (Code Mode)')}</div>
							<span
								class="text-[0.65rem] font-medium uppercase px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400"
								>{$i18n.t('Read-only')}</span
							>
						</div>

						<div class="text-xs text-gray-500 mb-2">
							{#if enterpriseOAuthDeploymentManaged}
								{$i18n.t('Deployment-managed configuration detected. This screen is read-only.')}
							{:else}
								{$i18n.t(
									'Deployment-managed configuration not detected. This screen remains read-only during rollout.'
								)}
							{/if}
						</div>
						{#if enterpriseOAuthLoading}
							<div class="text-xs text-gray-500 mb-2 flex items-center gap-2">
								<Spinner className="size-3" />
								{$i18n.t('Loading deployment configuration...')}
							</div>
						{:else if enterpriseOAuthError}
							<div class="text-xs text-amber-600 dark:text-amber-400 mb-2">
								{$i18n.t('Failed to load deployment configuration. Showing defaults.')}
							</div>
						{/if}
						<div
							class="text-xs text-gray-500 mb-3 rounded-lg border border-gray-100/60 dark:border-gray-800/60 bg-gray-50/60 dark:bg-gray-900/40 px-3 py-2"
						>
							{$i18n.t(
								'To change Enterprise OAuth, update ENTERPRISE_OAUTH_* in your deployment environment (compose/.env/secret manager) and restart Open WebUI. Runtime edits are not supported.'
							)}
						</div>
						{#if !enterpriseOAuthLoading}
							{#if enterpriseOAuthDeploymentManaged && enterpriseOAuthMissingRequiredEnv.length > 0}
								<div
									class="text-xs text-amber-600 dark:text-amber-400 mb-2 rounded-lg border border-amber-200/60 dark:border-amber-900/40 bg-amber-50/60 dark:bg-amber-950/20 px-3 py-2"
								>
									{$i18n.t('Missing required Enterprise OAuth environment variables:')}
									{' '}
									<span class="font-mono break-all">
										{enterpriseOAuthMissingRequiredEnv.join(', ')}
									</span>
								</div>
							{/if}
							<div
								class="text-xs text-gray-500 mb-3 rounded-lg border border-gray-100/60 dark:border-gray-800/60 bg-gray-50/60 dark:bg-gray-900/40 px-3 py-2"
							>
								{#if enterpriseOAuthEffectiveRedirectUri}
									{$i18n.t('Effective redirect URI:')}
									{' '}
									<span class="font-mono break-all">
										{enterpriseOAuthEffectiveRedirectUri}
									</span>
								{:else}
									{$i18n.t(
										'Effective redirect URI unavailable. Set ENTERPRISE_OAUTH_REDIRECT_URI or WEBUI_URL.'
									)}
								{/if}
							</div>
						{/if}

						<div class="flex items-center justify-between mb-3">
							<div class="text-xs text-gray-500">
								{$i18n.t('Current runtime value from deployment config.')}
							</div>
							<Switch bind:state={enterpriseOAuthForm.ENTERPRISE_OAUTH_ENABLED} disabled={true} />
						</div>

						<div class="grid grid-cols-1 gap-3">
							<div>
								<div class="text-xs font-medium mb-1">Provider Name</div>
								<input
									class="w-full rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-3 py-2 text-xs"
									placeholder="SSO"
									bind:value={enterpriseOAuthForm.ENTERPRISE_OAUTH_PROVIDER_NAME}
									readonly
								/>
							</div>

							<div>
								<div class="text-xs font-medium mb-1">Client ID</div>
								<input
									class="w-full rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-3 py-2 text-xs"
									placeholder="CLIENT_ID"
									bind:value={enterpriseOAuthForm.ENTERPRISE_OAUTH_CLIENT_ID}
									readonly
								/>
							</div>

							<div>
								<div class="text-xs font-medium mb-1">Client Secret</div>
								<input
									class="w-full rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-3 py-2 text-xs"
									type="password"
									placeholder="CLIENT_SECRET"
									value={enterpriseOAuthForm.ENTERPRISE_OAUTH_CLIENT_SECRET}
									readonly
								/>
							</div>

							<div>
								<div class="text-xs font-medium mb-1">Authorize Request URL</div>
								<input
									class="w-full rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-3 py-2 text-xs"
									placeholder="http://host/esc-sso/oauth2.0/authorize"
									bind:value={enterpriseOAuthForm.ENTERPRISE_OAUTH_AUTHORIZE_URL}
									readonly
								/>
							</div>

							<div>
								<div class="text-xs font-medium mb-1">Token Request URL</div>
								<input
									class="w-full rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-3 py-2 text-xs"
									placeholder="http://host/esc-sso/oauth2.0/accessToken"
									bind:value={enterpriseOAuthForm.ENTERPRISE_OAUTH_TOKEN_URL}
									readonly
								/>
							</div>

							<div>
								<div class="text-xs font-medium mb-1">Userinfo Request URL</div>
								<input
									class="w-full rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-3 py-2 text-xs"
									placeholder="http://host/esc-sso/oauth2.0/profile"
									bind:value={enterpriseOAuthForm.ENTERPRISE_OAUTH_PROFILE_URL}
									readonly
								/>
							</div>

							<div>
								<div class="text-xs font-medium mb-1">Token Validate URL</div>
								<input
									class="w-full rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-3 py-2 text-xs"
									placeholder="http://host/esc-sso/api/v1/loginLog/checkAccessToken"
									bind:value={enterpriseOAuthForm.ENTERPRISE_OAUTH_CHECK_TOKEN_URL}
									readonly
								/>
							</div>

							<div>
								<div class="text-xs font-medium mb-1">Token Logout URL</div>
								<input
									class="w-full rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-3 py-2 text-xs"
									placeholder="http://host/esc-sso/cxf/api/v1/ssoSession/remove"
									bind:value={enterpriseOAuthForm.ENTERPRISE_OAUTH_LOGOUT_URL}
									readonly
								/>
							</div>

							<div>
								<div class="text-xs font-medium mb-1">Redirect URI</div>
								<input
									class="w-full rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-3 py-2 text-xs"
									placeholder="http://host/oauth/enterprise/callback"
									bind:value={enterpriseOAuthForm.ENTERPRISE_OAUTH_REDIRECT_URI}
									readonly
								/>
							</div>

							<div>
								<div class="text-xs font-medium mb-1">Authorize Redirect Param</div>
								<input
									class="w-full rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-3 py-2 text-xs"
									bind:value={enterpriseOAuthForm.ENTERPRISE_OAUTH_AUTHORIZE_REDIRECT_PARAM}
									readonly
								/>
							</div>

							<div>
								<div class="text-xs font-medium mb-1">Token Redirect Param</div>
								<input
									class="w-full rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-3 py-2 text-xs"
									bind:value={enterpriseOAuthForm.ENTERPRISE_OAUTH_TOKEN_REDIRECT_PARAM}
									readonly
								/>
							</div>
						</div>
					</div>

					<hr class=" border-gray-100/30 dark:border-gray-850/30 my-4" />

					<div class="mb-2.5 flex flex-col w-full">
						<div class="flex justify-between items-center mb-1">
							<div class="flex items-center gap-2">
								<div class="font-medium">{$i18n.t('Open Terminal')}</div>
								<span
									class="text-[0.65rem] font-medium uppercase px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400"
									>{$i18n.t('Experimental')}</span
								>
							</div>

							<Tooltip content={$i18n.t('Add Connection')}>
								<button
									class="px-1"
									on:click={() => {
										editTerminalIdx = null;
										showAddTerminalModal = true;
									}}
									type="button"
								>
									<Plus />
								</button>
							</Tooltip>
						</div>

						<div class="flex flex-col gap-1.5">
							{#each terminalConnections as connection, idx}
								<div class="flex w-full gap-2 items-center">
									<Tooltip className="w-full relative" content={''} placement="top-start">
										<div class="flex w-full">
											<div
												class="flex-1 relative flex gap-1.5 items-center {connection?.enabled ===
												false
													? 'opacity-50'
													: ''}"
											>
												<Tooltip content={$i18n.t('Terminal')}>
													<Cloud className="size-4" strokeWidth="1.5" />
												</Tooltip>

												<div class="outline-hidden w-full bg-transparent text-sm">
													{connection.name || connection.url || $i18n.t('New Terminal')}
												</div>
											</div>
										</div>
									</Tooltip>

									<div class="flex gap-1 items-center">
										<Tooltip content={$i18n.t('Configure')}>
											<button
												class="self-center p-1 bg-transparent hover:bg-gray-100 dark:hover:bg-gray-850 rounded-lg transition"
												on:click={() => {
													editTerminalIdx = idx;
													showAddTerminalModal = true;
												}}
												type="button"
											>
												<Cog6 />
											</button>
										</Tooltip>

										<Tooltip
											content={connection?.enabled !== false
												? $i18n.t('Enabled')
												: $i18n.t('Disabled')}
										>
											<Switch
												state={connection?.enabled !== false}
												on:change={() => {
													terminalConnections = terminalConnections.map((c, i) =>
														i === idx ? { ...c, enabled: !(c?.enabled !== false) } : c
													);
													saveTerminalServers();
												}}
											/>
										</Tooltip>
									</div>
								</div>
							{/each}
						</div>

						{#if terminalConnections.length === 0}
							<div class="text-xs text-gray-400 dark:text-gray-500">
								{$i18n.t('No terminal connections configured.')}
							</div>
						{/if}

						<div class="mt-1.5">
							<div class="text-xs text-gray-500">
								{$i18n.t(
									'Connect to Open Terminal instances. All users will have access to file browsing and terminal tools through these servers.'
								)}
							</div>
							<div class="text-xs text-gray-600 dark:text-gray-300 mt-1">
								<a
									class="underline"
									href="https://github.com/open-webui/open-terminal"
									target="_blank">{$i18n.t('Learn more about Open Terminal')} ↗</a
								>
							</div>
						</div>
					</div>
				</div>
			</div>
		{:else}
			<div class="flex h-full justify-center">
				<div class="my-auto">
					<Spinner className="size-6" />
				</div>
			</div>
		{/if}
	</div>

	<div class="flex justify-end pt-3 text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>
