const DEFAULT_HISTORY_SUGGESTION_LIMIT = 8

const GENERIC_TITLE_SET = new Set([
	'new chat',
	'untitled',
	'untitled chat',
	'新对话',
	'新聊天',
	'新的聊天',
	'未命名',
	'未命名对话'
])

const normalizeText = (value: string): string => {
	return (value ?? '').trim().toLowerCase()
}

export const getHardcodedSuggestionPrompts = () => {
	return [
		{
			id: 'hardcoded-1',
			content: '总结我最近关注的主题，并给出下一步建议',
			title: ['总结最近主题', '推荐']
		},
		{
			id: 'hardcoded-2',
			content: '根据我之前的问题，给一个可执行的任务清单',
			title: ['生成任务清单', '推荐']
		},
		{
			id: 'hardcoded-3',
			content: '把我最近的需求按优先级排序并说明原因',
			title: ['需求优先级排序', '推荐']
		},
		{
			id: 'hardcoded-4',
			content: '提炼我最近对话里的关键信息，输出3条行动项',
			title: ['提炼行动项', '推荐']
		},
		{
			id: 'hardcoded-5',
			content: '帮我把最近的工作内容整理成可汇报的一页摘要',
			title: ['生成汇报摘要', '推荐']
		}
	]
}

export const buildHistoryTitleSuggestions = (
	chats: Array<{ id?: string; title?: string; updated_at?: number | null }> = [],
	limit: number = DEFAULT_HISTORY_SUGGESTION_LIMIT
) => {
	const dedupedTitles = new Set<string>()

	const sortedChats = [...(chats ?? [])].sort(
		(a, b) => Number(b?.updated_at ?? 0) - Number(a?.updated_at ?? 0)
	)

	const suggestions: Array<{ id: string; content: string; title: [string, string] }> = []

	for (const chat of sortedChats) {
		const rawTitle = (chat?.title ?? '').trim()
		if (!rawTitle) continue

		const normalizedTitle = normalizeText(rawTitle)
		if (!normalizedTitle || GENERIC_TITLE_SET.has(normalizedTitle)) continue
		if (dedupedTitles.has(normalizedTitle)) continue

		dedupedTitles.add(normalizedTitle)
		suggestions.push({
			id: `history-${chat?.id ?? normalizedTitle}`,
			content: rawTitle,
			title: [rawTitle, '历史记录']
		})

		if (suggestions.length >= limit) break
	}

	return suggestions
}
