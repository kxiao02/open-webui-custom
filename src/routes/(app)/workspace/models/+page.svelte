<script>
	import { onMount } from 'svelte';
	import { config, models, settings } from '$lib/stores';
	import { getModels } from '$lib/apis';
	import Models from '$lib/components/workspace/Models.svelte';

	onMount(async () => {
		try {
			models.set(
				await getModels(
					localStorage.token,
					$config?.features?.enable_direct_connections && ($settings?.directConnections ?? null)
				)
			);
		} catch (error) {
			console.error(error);
			models.set([]);
		}
	});
</script>

<Models />
