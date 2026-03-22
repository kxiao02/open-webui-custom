<script lang="ts">
	import GlobeAlt from '$lib/components/icons/GlobeAlt.svelte';

	export let url = '';
	export let title = '';
	export let className = 'size-4 rounded-full';
	export let fallbackClassName = '';

	let faviconFailed = false;

	const FALLBACK_TONES = [
		'bg-sky-100 text-sky-700 dark:bg-sky-900/60 dark:text-sky-200',
		'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/60 dark:text-emerald-200',
		'bg-amber-100 text-amber-700 dark:bg-amber-900/60 dark:text-amber-200',
		'bg-rose-100 text-rose-700 dark:bg-rose-900/60 dark:text-rose-200',
		'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/60 dark:text-indigo-200',
		'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/60 dark:text-cyan-200'
	];

	const getDomain = (value: string = '') => {
		try {
			return new URL(value).hostname.replace(/^www\./, '');
		} catch {
			return value.replace(/^https?:\/\//i, '').split(/[/?#]/)[0];
		}
	};

	const getInitial = (value: string = '') => {
		const match = value.toUpperCase().match(/[A-Z0-9]/);
		return match ? match[0] : '';
	};

	const getTone = (value: string = '') => {
		if (!value) {
			return FALLBACK_TONES[0];
		}

		let hash = 0;
		for (const char of value) {
			hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
		}

		return FALLBACK_TONES[hash % FALLBACK_TONES.length];
	};

	$: resolvedUrl = url || title;
	$: domain = getDomain(resolvedUrl);
	$: initial = getInitial(domain);
	$: toneClass = getTone(domain);
	$: altText = title ? `${title} favicon` : `${domain || 'web source'} favicon`;
</script>

{#if resolvedUrl && !faviconFailed}
	<img
		src={"https://www.google.com/s2/favicons?sz=32&domain=" + resolvedUrl}
		alt={altText}
		class={className}
		on:error={() => {
			faviconFailed = true;
		}}
	/>
{:else}
	<div
		class={`flex shrink-0 items-center justify-center border border-white/80 dark:border-gray-800 ${className} ${toneClass} ${fallbackClassName}`}
		aria-label={domain || title || 'web source'}
		title={domain || title || 'web source'}
	>
		{#if initial}
			<span class="text-[9px] font-semibold leading-none">{initial}</span>
		{:else}
			<GlobeAlt className="size-[70%]" strokeWidth="1.8" />
		{/if}
	</div>
{/if}
