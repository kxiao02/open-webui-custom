<script lang="ts">
	import { browser } from '$app/environment';
	import DOMPurify from 'dompurify';
	import type { Token } from 'marked';

	import { WEBUI_BASE_URL } from '$lib/constants';
	import { settings } from '$lib/stores';
	import { normalizeMediaUrl } from '$lib/utils/knowflowAssets';
	import FullHeightIframe from '$lib/components/common/FullHeightIframe.svelte';

	export let id: string;
	export let token: Token;

	let html: string | null = null;
	let rendersHtmlMedia = false;
	let rendersHtmlTable = false;
	let tokenText = '';

	const normalizeHtmlMediaUrl = (value: string | null): string | null => {
		if (typeof value !== 'string') {
			return null;
		}

		const normalized = normalizeMediaUrl(value);
		return normalized || null;
	};

	const enhanceHtmlMedia = (
		value: string
	): { html: string; rendersHtmlMedia: boolean; rendersHtmlTable: boolean } => {
		const sanitized = DOMPurify.sanitize(value);
		const mediaMatch = /<(img|table)\b/i.test(sanitized);

		if (!mediaMatch || !browser || typeof DOMParser === 'undefined') {
			return {
				html: sanitized,
				rendersHtmlMedia: mediaMatch,
				rendersHtmlTable: /<table\b/i.test(sanitized)
			};
		}

		try {
			const doc = new DOMParser().parseFromString(sanitized, 'text/html');

			doc.querySelectorAll('[src]').forEach((element) => {
				const source = normalizeHtmlMediaUrl(element.getAttribute('src'));
				if (source) {
					element.setAttribute('src', source);
				}
			});

			doc.querySelectorAll('[href]').forEach((element) => {
				const href = normalizeHtmlMediaUrl(element.getAttribute('href'));
				if (href) {
					element.setAttribute('href', href);
				}
			});

			return {
				html: doc.body.innerHTML,
				rendersHtmlMedia: Boolean(doc.querySelector('img, table')),
				rendersHtmlTable: Boolean(doc.querySelector('table'))
			};
		} catch {
			return {
				html: sanitized,
				rendersHtmlMedia: mediaMatch,
				rendersHtmlTable: /<table\b/i.test(sanitized)
			};
		}
	};

	const getTokenText = (value: Token | null | undefined): string => {
		const text = (value as { text?: unknown } | null | undefined)?.text;
		return typeof text === 'string' ? text : '';
	};

	$: tokenText = getTokenText(token);

	$: if (token?.type === 'html' && tokenText) {
		const enhanced = enhanceHtmlMedia(tokenText);
		html = enhanced.html;
		rendersHtmlMedia = enhanced.rendersHtmlMedia;
		rendersHtmlTable = enhanced.rendersHtmlTable;
	} else {
		html = null;
		rendersHtmlMedia = false;
		rendersHtmlTable = false;
	}
</script>

{#if token?.type === 'html'}
	{#if html && html.includes('<video')}
		{@const video = html.match(/<video[^>]*>([\s\S]*?)<\/video>/)}
		{@const videoSrc = video && video[1]}
		{#if videoSrc}
			<!-- svelte-ignore a11y-media-has-caption -->
				<video
					class="w-full my-2"
					src={videoSrc.replaceAll('&amp;', '&')}
					title="Video player"
					controls
			></video>
		{:else}
			{tokenText}
		{/if}
	{:else if html && html.includes('<audio')}
		{@const audio = html.match(/<audio[^>]*>([\s\S]*?)<\/audio>/)}
		{@const audioSrc = audio && audio[1]}
		{#if audioSrc}
			<!-- svelte-ignore a11y-media-has-caption -->
			<audio
				class="w-full my-2"
				src={audioSrc.replaceAll('&amp;', '&')}
				title="Audio player"
				controls
			></audio>
		{:else}
			{tokenText}
		{/if}
	{:else if tokenText && tokenText.match(/<iframe\s+[^>]*src="https:\/\/www\.youtube\.com\/embed\/([a-zA-Z0-9_-]{11})(?:\?[^"]*)?"[^>]*><\/iframe>/)}
		{@const match = tokenText.match(
			/<iframe\s+[^>]*src="https:\/\/www\.youtube\.com\/embed\/([a-zA-Z0-9_-]{11})(?:\?[^"]*)?"[^>]*><\/iframe>/
		)}
		{@const ytId = match && match[1]}
		{#if ytId}
			<iframe
				class="w-full aspect-video my-2"
				src={`https://www.youtube.com/embed/${ytId}`}
				title="YouTube video player"
				frameborder="0"
				allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
				referrerpolicy="strict-origin-when-cross-origin"
				allowfullscreen
			>
			</iframe>
		{/if}
	{:else if tokenText && tokenText.includes('<iframe')}
		{@const match = tokenText.match(/<iframe\s+[^>]*src="([^"]+)"[^>]*><\/iframe>/)}
		{@const iframeSrc = match && match[1]}
		{#if iframeSrc}
				<iframe
					class="w-full my-2"
					src={normalizeMediaUrl(iframeSrc)}
					title="Embedded content"
					frameborder="0"
					sandbox=""
					on:load={(e) => {
						try {
							const frame = e.currentTarget as HTMLIFrameElement;
							const body = frame.contentWindow?.document.body;
							if (body) {
								frame.style.height = body.scrollHeight + 20 + 'px';
							}
						} catch {}
					}}
				></iframe>
		{:else}
			{tokenText}
		{/if}
	{:else if tokenText && tokenText.includes('<status')}
		{@const match = tokenText.match(/<status title="([^"]+)" done="(true|false)" ?\/?>/)}
		{@const statusTitle = match && match[1]}
		{@const statusDone = match && match[2] === 'true'}
		{#if statusTitle}
			<div class="flex flex-col justify-center -space-y-0.5">
				<div
					class="{statusDone === false
						? 'shimmer'
						: ''} text-gray-500 dark:text-gray-500 line-clamp-1 text-wrap"
				>
					{statusTitle}
				</div>
			</div>
		{:else}
			{tokenText}
		{/if}
	{:else if tokenText.includes(`<file type="html"`)}
		{@const match = tokenText.match(/<file type="html" id="([^"]+)"/)}
		{@const fileId = match && match[1]}
		{#if fileId && fileId !== 'null' && fileId !== 'undefined'}
			<FullHeightIframe
				src={`${WEBUI_BASE_URL}/api/v1/files/${fileId}/content/html`}
				title="Content"
				iframeClassName="w-full my-2"
				allowForms={$settings?.iframeSandboxAllowForms ?? false}
				allowSameOrigin={$settings?.iframeSandboxAllowSameOrigin ?? false}
				allowPopups={true}
			/>
		{/if}
	{:else if html && rendersHtmlMedia}
		<div class="html-media-block my-2 max-w-full">
			<div class:scrollbar-hidden={rendersHtmlTable} class:overflow-x-auto={rendersHtmlTable}>
				{@html html}
			</div>
		</div>
	{:else if tokenText.trim().match(/^<br\s*\/?>$/i)}
		<br />
	{:else}
		{tokenText}
	{/if}
{/if}

<style>
	:global(.html-media-block img) {
		display: block;
		max-width: 100%;
		height: auto;
		border-radius: 0.75rem;
	}

	:global(.html-media-block table) {
		width: 100%;
		max-width: 100%;
		border-collapse: separate;
		border-spacing: 0;
		font-size: 0.875rem;
		text-align: left;
		color: inherit;
	}

	:global(.html-media-block caption) {
		caption-side: top;
		padding-bottom: 0.5rem;
		text-align: left;
		font-weight: 600;
		color: inherit;
	}

	:global(.html-media-block th) {
		padding: 0.5rem 0.625rem;
		border-bottom: 1px solid rgb(243 244 246 / 1);
		font-size: 0.75rem;
		text-transform: uppercase;
		vertical-align: top;
	}

	:global(.html-media-block td) {
		padding: 0.5rem 0.75rem;
		border-bottom: 1px solid rgb(249 250 251 / 1);
		vertical-align: top;
		color: inherit;
	}

	:global(.dark .html-media-block th) {
		border-bottom-color: rgb(31 41 55 / 1);
	}

	:global(.dark .html-media-block td) {
		border-bottom-color: rgb(17 24 39 / 1);
	}
</style>
