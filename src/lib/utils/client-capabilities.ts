export type ClientCapabilities = {
	schema_version: 2;
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
	diagram_render: {
		enabled: boolean;
		engines: string[];
		surfaces: string[];
		assistant_message_engines: string[];
		file_preview_engines: string[];
		file_extensions: string[];
	};
	workspace_tool_draft: {
		enabled: boolean;
		mode: 'confirm_then_edit';
		format: 'python_tool_class';
	};
	workspace_skill_draft: {
		enabled: boolean;
		mode: 'confirm_then_edit';
		format: 'markdown_skill';
	};
};

type BuildClientCapabilitiesArgs = {
	chatId?: string | null;
	temporaryChatEnabled?: boolean | null;
	canShareChat?: boolean;
	canDraftTool?: boolean;
	canDraftSkill?: boolean;
};

const PREVIEWABLE_FILE_TYPES = [
	'image',
	'pdf',
	'docx',
	'xlsx',
	'pptx',
	'html',
	'markdown',
	'mermaid',
	'text',
	'code',
	'json',
	'csv',
	'notebook',
	'sqlite',
	'audio',
	'video'
];

export const getClientCapabilities = ({
	chatId,
	temporaryChatEnabled,
	canShareChat = true,
	canDraftTool = true,
	canDraftSkill = true
}: BuildClientCapabilitiesArgs): ClientCapabilities => {
	const hasPersistentChat = typeof chatId === 'string' && !!chatId && !chatId.startsWith('local:');
	const shareEnabled = Boolean(hasPersistentChat && !temporaryChatEnabled && canShareChat);

	return {
		schema_version: 2,
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
		},
		diagram_render: {
			enabled: true,
			engines: ['mermaid', 'vega', 'vega-lite'],
			surfaces: ['assistant_message'],
			assistant_message_engines: ['mermaid', 'vega', 'vega-lite'],
			file_preview_engines: ['mermaid'],
			file_extensions: ['.md', '.markdown', '.mdx', '.mermaid', '.mmd']
		},
		workspace_tool_draft: {
			enabled: canDraftTool,
			mode: 'confirm_then_edit',
			format: 'python_tool_class'
		},
		workspace_skill_draft: {
			enabled: canDraftSkill,
			mode: 'confirm_then_edit',
			format: 'markdown_skill'
		}
	};
};
