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
		toolId: 'xlsx_read_workbook',
		toolName: '读取表格',
		aliases: ['xlsx_read', 'excel_read', 'spreadsheet_read'],
		category: 'file'
	},
	{
		toolId: 'xlsx_validate_workbook',
		toolName: '校验表格公式',
		aliases: ['xlsx_validate', 'excel_validate', 'spreadsheet_validate'],
		category: 'file'
	},
	{
		toolId: 'xlsx_create_workbook',
		toolName: '新建表格',
		aliases: ['xlsx_create', 'excel_create', 'spreadsheet_create'],
		category: 'file'
	},
	{
		toolId: 'xlsx_add_column_tool',
		toolName: '表格新增列',
		aliases: ['xlsx_add_column', 'excel_add_column'],
		category: 'file'
	},
	{
		toolId: 'xlsx_insert_row_tool',
		toolName: '表格插入行',
		aliases: ['xlsx_insert_row', 'excel_insert_row'],
		category: 'file'
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
		toolId: 'pdf_create_document',
		toolName: '生成 PDF',
		aliases: ['pdf_create', 'document_pdf_create'],
		category: 'file'
	},
	{
		toolId: 'pdf_inspect_form',
		toolName: '检查 PDF 表单',
		aliases: ['pdf_inspect_form_fields', 'pdf_form_inspect'],
		category: 'file'
	},
	{
		toolId: 'pdf_fill_form_tool',
		toolName: '填写 PDF 表单',
		aliases: ['pdf_fill_form', 'pdf_form_fill'],
		category: 'file'
	},
	{
		toolId: 'pdf_reformat_document',
		toolName: '重排 PDF 文档',
		aliases: ['pdf_reformat', 'document_pdf_reformat'],
		category: 'file'
	},
	{
		toolId: 'pdf_remove_pages',
		toolName: '删除 PDF 页面',
		aliases: ['pdf_delete_pages', 'pdf_page_remove'],
		category: 'file'
	},
	{
		toolId: 'pdf_add_pages',
		toolName: '插入 PDF 页面',
		aliases: ['pdf_insert_pages', 'pdf_page_add'],
		category: 'file'
	},
	{
		toolId: 'pdf_locate_pages',
		toolName: '定位 PDF 页面',
		aliases: ['pdf_find_pages', 'pdf_page_locate'],
		category: 'file'
	},
	{
		toolId: 'docx_export_document',
		toolName: '导出 DOCX',
		aliases: ['docx_export', 'word_export', 'markdown_docx'],
		category: 'file'
	},
	{
		toolId: 'pptx_export_presentation',
		toolName: '导出 PPTX',
		aliases: ['pptx_export', 'presentation_export', 'markdown_pptx'],
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
	},
	{
		toolId: 'write_todos',
		toolName: '任务清单',
		aliases: ['todo_list', 'task_list'],
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
	parsedResult?: unknown;
};

const MAX_DYNAMIC_LABEL_LENGTH = 48;

const getStringField = (
	record: Record<string, unknown> | null | undefined,
	keys: string[]
): string => {
	for (const key of keys) {
		const value = record?.[key];
		if (typeof value === 'string' && value.trim()) {
			return value.trim();
		}
	}
	return '';
};

const truncateDisplayText = (value: string, maxLength = MAX_DYNAMIC_LABEL_LENGTH): string => {
	const normalized = value.replace(/\s+/g, ' ').trim();
	if (!normalized || normalized.length <= maxLength) {
		return normalized;
	}
	return `${normalized.slice(0, Math.max(0, maxLength - 3)).trim()}...`;
};

const getHostnameLabel = (urlValue: string): string => {
	const normalized = urlValue.trim();
	if (!normalized) return '';

	const candidates = [
		normalized,
		/^[a-z]+:\/\//i.test(normalized) ? '' : `https://${normalized}`
	].filter(Boolean);

	for (const candidate of candidates) {
		try {
			const hostname = new URL(candidate).hostname.replace(/^www\./i, '').trim();
			if (hostname) {
				return hostname;
			}
		} catch {
			continue;
		}
	}

	return '';
};

const getVisitWebsiteLabel = (
	parsedArgs: Record<string, unknown> | null | undefined,
	parsedResult: unknown
): string => {
	const resultRecord =
		parsedResult && typeof parsedResult === 'object' && !Array.isArray(parsedResult)
			? (parsedResult as Record<string, unknown>)
			: null;
	const resultTitle = getStringField(resultRecord, [
		'title',
		'name',
		'pageTitle',
		'page_title',
		'pageName',
		'page_name',
		'siteName',
		'site_name',
		'websiteName',
		'website_name'
	]);
	const explicitTitle = getStringField(parsedArgs, [
		'title',
		'name',
		'pageTitle',
		'page_title',
		'siteName',
		'site_name',
		'websiteName',
		'website_name'
	]);
	const urlValue = getStringField(parsedArgs, ['url', 'href', 'link', 'websiteUrl', 'website_url']);
	const websiteLabel = truncateDisplayText(
		explicitTitle || resultTitle || getHostnameLabel(urlValue),
		40
	);

	return websiteLabel ? `打开 ${websiteLabel}` : '打开网页';
};

const getSearchToolLabel = (
	parsedArgs: Record<string, unknown> | null | undefined
): string => {
	const query = truncateDisplayText(
		getStringField(parsedArgs, ['query', 'q', 'keywords', 'keyword', 'search_query']),
		56
	);
	return query ? `搜索 ${query}` : '搜索';
};

const getDynamicToolName = (
	toolId: string,
	parsedArgs: Record<string, unknown> | null | undefined,
	parsedResult: unknown
): string => {
	if (toolId === 'internet_search') {
		return getSearchToolLabel(parsedArgs);
	}
	if (toolId === 'visit_webpage') {
		return getVisitWebsiteLabel(parsedArgs, parsedResult);
	}
	return '';
};

export const resolveToolDisplay = ({
	toolId,
	toolName,
	legacyName,
	parsedArgs = null,
	parsedResult = null
}: ResolveToolDisplayInput): {
	toolId: string;
	toolName: string;
	baseToolName: string;
	category: ToolDisplayCategory;
} => {
	const normalizedToolId =
		normalizeToolId(toolId) ||
		normalizeToolId(toolName) ||
		normalizeToolId(legacyName) ||
		inferToolIdFromArgs(parsedArgs);
	const localizedToolName = normalizedToolId ? TOOL_NAME_BY_ID[normalizedToolId] : '';
	const fallbackName = (legacyName ?? '').trim();
	const baseToolName =
		localizedToolName ||
		(toolName ?? '').trim() ||
		fallbackName ||
		'工具调用';
	const resolvedToolName =
		getDynamicToolName(normalizedToolId, parsedArgs, parsedResult) || baseToolName;
	const definition = TOOL_DISPLAY_DEFINITIONS.find((item) => item.toolId === normalizedToolId);

	return {
		toolId: normalizedToolId || fallbackName,
		toolName: resolvedToolName,
		baseToolName,
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
