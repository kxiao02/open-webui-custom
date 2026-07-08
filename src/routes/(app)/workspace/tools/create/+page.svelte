<script>
	// @ts-nocheck
	import { goto } from '$app/navigation';
	import { createNewTool, getTools } from '$lib/apis/tools';
	import ToolkitEditor from '$lib/components/workspace/Tools/ToolkitEditor.svelte';
	import { WEBUI_VERSION } from '$lib/constants';
	import { tools } from '$lib/stores';
	import { getToolVersionRequirement } from '$lib/utils/tool-drafts';
	import { onMount, getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	const i18n = /** @type {import('$lib/i18n').I18nStore} */ (getContext('i18n'));

	let mounted = false;
	let clone = false;
	let tool = null;

	const saveHandler = async (data) => {
		console.log(data);

		const requiredVersion = getToolVersionRequirement(data.content, WEBUI_VERSION);
		if (requiredVersion) {
			toast.error(
				$i18n.t(
					'Open WebUI version (v{{OPEN_WEBUI_VERSION}}) is lower than required version (v{{REQUIRED_VERSION}})',
					{
						OPEN_WEBUI_VERSION: WEBUI_VERSION,
						REQUIRED_VERSION: requiredVersion
					}
				)
			);
			return;
		}

		const res = await createNewTool(localStorage.token, {
			id: data.id,
			name: data.name,
			meta: data.meta,
			content: data.content,
			access_grants: data.access_grants
		}).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			toast.success($i18n.t('Tool created successfully'));
			tools.set(await getTools(localStorage.token));

			await goto('/workspace/tools');
		}
	};

	onMount(() => {
		if (sessionStorage.tool) {
			tool = JSON.parse(sessionStorage.tool);
			sessionStorage.removeItem('tool');

			console.log(tool);
			clone = true;
		}

		mounted = true;
	});
</script>

{#if mounted}
	{#key tool?.content}
		<ToolkitEditor
			id={tool?.id ?? ''}
			name={tool?.name ?? ''}
			meta={tool?.meta ?? { description: '' }}
			content={tool?.content ?? ''}
			accessGrants={tool?.access_grants !== undefined ? tool.access_grants : []}
			{clone}
			onSave={(value) => {
				saveHandler(value);
			}}
		/>
	{/key}
{/if}
