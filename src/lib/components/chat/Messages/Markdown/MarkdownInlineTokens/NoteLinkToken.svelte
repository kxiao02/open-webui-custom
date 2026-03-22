<script lang="ts">
	import { onMount, getContext } from 'svelte';
	import { goto } from '$app/navigation';
	import { getNoteById } from '$lib/apis/notes';
	import { getUserInfoById } from '$lib/apis/users';
	import { capitalizeFirstLetter } from '$lib/utils';
	import ReferenceLinkItem from '$lib/components/chat/Messages/ReferenceLinkItem.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');

	export let noteId: string;
	export let href: string;

	let title = '';
	let author = '';
	let loading = true;

	$: noteTitle = title || $i18n.t('Note');

	const openNote = () => {
		try {
			const url = new URL(href, window.location.origin);
			goto(url.pathname + url.search + url.hash);
		} catch {
			window.location.href = href;
		}
	};

	onMount(async () => {
		try {
			const note = await getNoteById(localStorage.token, noteId);
			if (note) {
				title = note.title || $i18n.t('Untitled');

				if (note.user_id) {
					try {
						const userInfo = await getUserInfoById(localStorage.token, note.user_id);
						if (userInfo) {
							author = capitalizeFirstLetter(userInfo.name ?? userInfo.email ?? '');
						}
					} catch {
						// user lookup failed, skip author
					}
				}
			} else {
				title = $i18n.t('Untitled');
			}
		} catch {
			title = $i18n.t('Note');
		} finally {
			loading = false;
		}
	});
</script>

<ReferenceLinkItem
	kind="note"
	label={$i18n.t('Note')}
	title={noteTitle}
	subtitle={author ? $i18n.t('By {{name}}', { name: author }) : ''}
	titleAttr={author ? `${noteTitle} · ${author}` : noteTitle}
	{loading}
	onClick={openNote}
/>
