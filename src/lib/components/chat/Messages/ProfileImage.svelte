<script lang="ts">
	import { WEBUI_BASE_URL } from '$lib/constants';

	export let className = 'size-8';
	export let src = `${WEBUI_BASE_URL}/static/favicon.png`;

	const defaultProfileSrc = `${WEBUI_BASE_URL}/static/favicon.png`;
	let fallbackApplied = false;

	$: resolvedSrc =
		src === ''
			? defaultProfileSrc
			: src.startsWith(WEBUI_BASE_URL) ||
				  src.startsWith('https://www.gravatar.com/avatar/') ||
				  src.startsWith('data:') ||
				  src.startsWith('/')
				? src
				: `${WEBUI_BASE_URL}/user.png`;
	$: {
		src;
		fallbackApplied = false;
	}

	const handleError = (event: Event) => {
		if (fallbackApplied) return;

		fallbackApplied = true;
		const image = event.currentTarget as HTMLImageElement;
		image.src = defaultProfileSrc;
	};
</script>

<img
	aria-hidden="true"
	src={resolvedSrc}
	class=" {className} object-cover rounded-full"
	alt="profile"
	draggable="false"
	on:error={handleError}
/>
