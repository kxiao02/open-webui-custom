<script lang="ts">
	import { createEventDispatcher, getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import Modal from '$lib/components/common/Modal.svelte';

	const i18n: import('$lib/i18n').I18nStore = getContext('i18n');
	const dispatch = createEventDispatcher();

	export let show = false;
	export let title = '';
	export let message = '';
	export let resourceType: 'tool' | 'skill' = 'tool';
	export let resourceName = '';
	export let installLabel = '';
	export let sessionLabel = '';
	export let cancelLabel = '';

	let fallbackTitle = '';
	let fallbackMessage = '';
	let resolved = false;
	let wasOpen = false;

	$: if (show) {
		resolved = false;
		wasOpen = true;
	}

	$: if (!show && wasOpen && !resolved) {
		resolved = true;
		wasOpen = false;
		dispatch('decision', { decision: 'cancel' });
	}

	const selectDecision = (decision: 'install' | 'session' | 'cancel') => {
		resolved = true;
		wasOpen = false;
		show = false;
		dispatch('decision', { decision });
	};

	$: fallbackTitle =
		title ||
		(resourceType === 'tool'
			? $i18n.t('Add tool to your account?')
			: $i18n.t('Add skill to your account?'));
	$: fallbackMessage =
		message ||
		$i18n.t('{{name}} can be installed to your account or enabled only for this chat.', {
			name:
				resourceName ||
				(resourceType === 'tool' ? $i18n.t('This tool') : $i18n.t('This skill'))
		});
</script>

<Modal
	bind:show
	size="sm"
	containerClassName="p-3"
	className="bg-white/95 dark:bg-gray-900/95 backdrop-blur-sm rounded-4xl"
>
	<div class="px-6 py-6 text-left">
		<div class="text-lg font-medium text-gray-900 dark:text-gray-100">
			{fallbackTitle}
		</div>

		<div class="mt-2 text-sm text-gray-500 dark:text-gray-400">
			{fallbackMessage}
		</div>

		<div class="mt-6 flex flex-col gap-2">
			<button
				type="button"
				class="w-full rounded-3xl bg-gradient-to-r from-[#ef5b6d] to-[#4a87ff] px-4 py-2 text-sm font-medium text-white transition hover:from-[#e45166] hover:to-[#3f79f1]"
				on:click={() => selectDecision('install')}
			>
				{installLabel || $i18n.t('Install to My Account')}
			</button>

			<button
				type="button"
				class="w-full rounded-3xl bg-gray-100 px-4 py-2 text-sm font-medium text-gray-800 transition hover:bg-gray-200 dark:bg-gray-850 dark:text-gray-100 dark:hover:bg-gray-800"
				on:click={() => selectDecision('session')}
			>
				{sessionLabel || $i18n.t('Only This Chat')}
			</button>

			<button
				type="button"
				class="w-full rounded-3xl px-4 py-2 text-sm font-medium text-gray-500 transition hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-850"
				on:click={() => selectDecision('cancel')}
			>
				{cancelLabel || $i18n.t('Cancel')}
			</button>
		</div>
	</div>
</Modal>
