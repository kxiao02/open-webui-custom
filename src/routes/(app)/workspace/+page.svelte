<script lang="ts">
	import { goto } from '$app/navigation';
	import { config, user } from '$lib/stores';
	import { onMount } from 'svelte';

	const getWorkspaceHome = () => {
		if ($user?.role === 'admin') {
			return '/workspace/models';
		}

		if (
			($config?.features?.enable_knowledge ?? true) &&
			$user?.role === 'user' &&
			$user?.permissions?.workspace?.knowledge
		) {
			return '/workspace/knowledge';
		}

		if ($user?.permissions?.workspace?.models) {
			return '/workspace/models';
		}
		if (($config?.features?.enable_knowledge ?? true) && $user?.permissions?.workspace?.knowledge) {
			return '/workspace/knowledge';
		}
		if ($user?.permissions?.workspace?.prompts) {
			return '/workspace/prompts';
		}
		if ($user?.permissions?.workspace?.skills) {
			return '/workspace/skills';
		}
		if ($user?.permissions?.workspace?.tools) {
			return '/workspace/tools';
		}

		return '/';
	};

	onMount(() => {
		goto(getWorkspaceHome());
	});
</script>
