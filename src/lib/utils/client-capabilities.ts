export type ClientCapabilities = {
	schema_version: 1;
	generated_file_download: {
		enabled: boolean;
		delivery: 'openwebui_file';
		storage: 'object_storage';
	};
	chat_share: {
		enabled: boolean;
		mode: 'share_link';
	};
	file_preview: {
		enabled: boolean;
		mode: 'inline_or_modal';
		types: string[];
	};
};

type BuildClientCapabilitiesArgs = {
	chatId?: string | null;
	temporaryChatEnabled?: boolean | null;
	canShareChat?: boolean;
};

const PREVIEWABLE_FILE_TYPES = ['image', 'pdf', 'docx', 'text', 'markdown', 'code'];

export const getClientCapabilities = ({
	chatId,
	temporaryChatEnabled,
	canShareChat = true
}: BuildClientCapabilitiesArgs): ClientCapabilities => {
	const hasPersistentChat = typeof chatId === 'string' && !!chatId && !chatId.startsWith('local:');
	const shareEnabled = Boolean(hasPersistentChat && !temporaryChatEnabled && canShareChat);

	return {
		schema_version: 1,
		generated_file_download: {
			enabled: true,
			delivery: 'openwebui_file',
			storage: 'object_storage'
		},
		chat_share: {
			enabled: shareEnabled,
			mode: 'share_link'
		},
		file_preview: {
			enabled: true,
			mode: 'inline_or_modal',
			types: PREVIEWABLE_FILE_TYPES
		}
	};
};
