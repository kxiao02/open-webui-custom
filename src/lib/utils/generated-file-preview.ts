import type { Writable } from 'svelte/store';

type PreviewStores = {
	showControls: Writable<boolean>;
	showFilePreview: Writable<boolean>;
	selectedGeneratedFilePreviewId: Writable<string | null>;
	showOverview?: Writable<boolean>;
	showArtifacts?: Writable<boolean>;
	showEmbeds?: Writable<boolean>;
	showCallOverlay?: Writable<boolean>;
};

export const openGeneratedFilePreview = (fileId: string, stores: PreviewStores) => {
	if (!fileId) return;

	stores.showOverview?.set(false);
	stores.showArtifacts?.set(false);
	stores.showEmbeds?.set(false);
	stores.showCallOverlay?.set(false);
	stores.selectedGeneratedFilePreviewId.set(fileId);
	stores.showFilePreview.set(true);
	stores.showControls.set(true);
};
