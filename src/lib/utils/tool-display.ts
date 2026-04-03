export type ToolDisplayCategory = 'web' | 'file' | 'code' | 'time' | 'utility';

export type ToolDisplayDefinition = {
	toolId: string;
	toolName: string;
	aliases?: string[];
	category: ToolDisplayCategory;
};

export const TOOL_DISPLAY_DEFINITIONS: ToolDisplayDefinition[] = [
	{
		toolId: 'internet_search',
		toolName: '网络搜索',
		aliases: ['联网搜索', 'web_search', 'search', '搜索'],
		category: 'web'
	},
	{
		toolId: 'visit_webpage',
		toolName: '网页读取',
		aliases: ['网页读取', 'fetch_url', 'webpage', 'url'],
		category: 'web'
	},
	{
		toolId: 'current_server_time',
		toolName: '服务器时间',
		aliases: ['服务器时间', 'time', 'clock', 'date', '时间'],
		category: 'time'
	},
	{
		toolId: 'math_calculator',
		toolName: '数学计算',
		aliases: ['数学计算', 'calculator', 'math', '算式计算'],
		category: 'utility'
	},
	{
		toolId: 'tool_self_check',
		toolName: '工具自检',
		aliases: ['工具自检', 'self_check', 'self_tool_check'],
		category: 'utility'
	},
	{
		toolId: 'read_structured_file',
		toolName: '读取结构化文件',
		aliases: ['读取结构化文件'],
		category: 'file'
	},
	{
		toolId: 'write_structured_file',
		toolName: '写入结构化文件',
		aliases: ['写入结构化文件'],
		category: 'file'
	},
	{
		toolId: 'gotenberg_convert',
		toolName: 'PDF 转换',
		aliases: ['PDF转换', 'PDF 转换'],
		category: 'file'
	},
	{
		toolId: 'ls',
		toolName: '列出目录',
		aliases: ['list_dir', 'list_directory'],
		category: 'file'
	},
	{
		toolId: 'glob',
		toolName: '文件匹配',
		aliases: ['find_files'],
		category: 'file'
	},
	{
		toolId: 'grep',
		toolName: '文本搜索',
		aliases: ['search_in_files'],
		category: 'file'
	},
	{
		toolId: 'read_file',
		toolName: '读取文件',
		aliases: ['file_read'],
		category: 'file'
	},
	{
		toolId: 'display_file',
		toolName: '查看文件',
		aliases: ['open_file', 'preview_file'],
		category: 'file'
	},
	{
		toolId: 'write_file',
		toolName: '写入文件',
		aliases: ['file_write'],
		category: 'file'
	},
	{
		toolId: 'replace_file_content',
		toolName: '替换文件内容',
		aliases: ['replace_file', 'file_replace'],
		category: 'file'
	},
	{
		toolId: 'edit_file',
		toolName: '编辑文件',
		aliases: ['update_file'],
		category: 'file'
	},
	{
		toolId: 'execute',
		toolName: '执行命令',
		aliases: ['run_command', 'exec', 'command'],
		category: 'code'
	},
	{
		toolId: 'task',
		toolName: '委派任务',
		aliases: ['delegate_task', 'subagent_task'],
		category: 'utility'
	}
];

export const TOOL_NAME_BY_ID = Object.fromEntries(
	TOOL_DISPLAY_DEFINITIONS.map((definition) => [definition.toolId, definition.toolName])
) as Record<string, string>;

const TOOL_ID_BY_ALIAS = new Map<string, string>();

for (const definition of TOOL_DISPLAY_DEFINITIONS) {
	TOOL_ID_BY_ALIAS.set(definition.toolId.toLowerCase(), definition.toolId);
	for (const alias of definition.aliases ?? []) {
		TOOL_ID_BY_ALIAS.set(alias.toLowerCase(), definition.toolId);
	}
}

export const normalizeToolId = (value: string | undefined | null): string => {
	const normalized = (value ?? '').trim();
	if (!normalized) return '';
	return TOOL_ID_BY_ALIAS.get(normalized.toLowerCase()) ?? normalized;
};

const inferToolIdFromArgs = (parsedArgs: Record<string, unknown> | null | undefined): string => {
	const keys = new Set(Object.keys(parsedArgs ?? {}));
	if (keys.size === 0) return '';

	if (keys.has('query') || keys.has('max_results') || keys.has('keywords')) {
		return 'internet_search';
	}
	if (keys.has('url') || keys.has('urls') || keys.has('link')) {
		return 'visit_webpage';
	}
	if (keys.has('expression') || keys.has('formula')) {
		return 'math_calculator';
	}
	if (keys.has('content') && (keys.has('path') || keys.has('file_path') || keys.has('filename'))) {
		return 'write_file';
	}
	if (keys.has('path') || keys.has('file_path') || keys.has('filename')) {
		return 'read_file';
	}
	if (keys.has('command') || keys.has('cmd')) {
		return 'execute';
	}

	return '';
};

export type ResolveToolDisplayInput = {
	toolId?: string | null;
	toolName?: string | null;
	legacyName?: string | null;
	parsedArgs?: Record<string, unknown> | null;
};

export const resolveToolDisplay = ({
	toolId,
	toolName,
	legacyName,
	parsedArgs = null
}: ResolveToolDisplayInput): {
	toolId: string;
	toolName: string;
	category: ToolDisplayCategory;
} => {
	const normalizedToolId =
		normalizeToolId(toolId) || normalizeToolId(legacyName) || inferToolIdFromArgs(parsedArgs);
	const localizedToolName = normalizedToolId ? TOOL_NAME_BY_ID[normalizedToolId] : '';
	const fallbackName = (legacyName ?? '').trim();
	const resolvedToolName =
		localizedToolName ||
		(toolName ?? '').trim() ||
		fallbackName ||
		'工具调用';
	const definition = TOOL_DISPLAY_DEFINITIONS.find((item) => item.toolId === normalizedToolId);

	return {
		toolId: normalizedToolId || fallbackName,
		toolName: resolvedToolName,
		category:
			definition?.category ??
			(normalizedToolId === 'read_file' || normalizedToolId === 'write_file' ? 'file' : 'utility')
	};
};

export const getToolDisplayName = (
	value: string | undefined | null,
	parsedArgs: Record<string, unknown> | null = null
): string => {
	return resolveToolDisplay({ legacyName: value, parsedArgs }).toolName;
};

export const getToolDisplayCategory = (
	value: string | undefined | null,
	parsedArgs: Record<string, unknown> | null = null
): ToolDisplayCategory => {
	return resolveToolDisplay({ legacyName: value, parsedArgs }).category;
};
