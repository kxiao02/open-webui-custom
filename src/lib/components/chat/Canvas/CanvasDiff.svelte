<script lang="ts">
	import { getContext } from 'svelte';
	import { canvasState } from '$lib/stores';

	const i18n = getContext('i18n');

	$: originalCode = $canvasState?.originalCode ?? '';
	$: currentCode = $canvasState?.code ?? '';
	$: diffLines = computeDiff(originalCode, currentCode);

	interface DiffLine {
		type: 'equal' | 'add' | 'remove';
		content: string;
		oldLineNum: number | null;
		newLineNum: number | null;
	}

	function computeDiff(oldText: string, newText: string): DiffLine[] {
		const oldLines = oldText.split('\n');
		const newLines = newText.split('\n');
		const result: DiffLine[] = [];

		// Simple LCS-based diff
		const m = oldLines.length;
		const n = newLines.length;

		// Build LCS table
		const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
		for (let i = 1; i <= m; i++) {
			for (let j = 1; j <= n; j++) {
				if (oldLines[i - 1] === newLines[j - 1]) {
					dp[i][j] = dp[i - 1][j - 1] + 1;
				} else {
					dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
				}
			}
		}

		// Backtrack to produce diff
		const ops: Array<{ type: 'equal' | 'add' | 'remove'; line: string }> = [];
		let i = m,
			j = n;
		while (i > 0 || j > 0) {
			if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
				ops.unshift({ type: 'equal', line: oldLines[i - 1] });
				i--;
				j--;
			} else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
				ops.unshift({ type: 'add', line: newLines[j - 1] });
				j--;
			} else {
				ops.unshift({ type: 'remove', line: oldLines[i - 1] });
				i--;
			}
		}

		let oldNum = 0;
		let newNum = 0;
		for (const op of ops) {
			if (op.type === 'equal') {
				oldNum++;
				newNum++;
				result.push({ type: 'equal', content: op.line, oldLineNum: oldNum, newLineNum: newNum });
			} else if (op.type === 'remove') {
				oldNum++;
				result.push({ type: 'remove', content: op.line, oldLineNum: oldNum, newLineNum: null });
			} else {
				newNum++;
				result.push({ type: 'add', content: op.line, oldLineNum: null, newLineNum: newNum });
			}
		}

		return result;
	}

	$: hasChanges = diffLines.some((l) => l.type !== 'equal');
</script>

<div class="h-full w-full overflow-auto font-mono text-xs">
	{#if !hasChanges}
		<div class="flex items-center justify-center h-full text-gray-400 text-sm">
			{$i18n.t('No changes')}
		</div>
	{:else}
		<table class="w-full border-collapse">
			<tbody>
				{#each diffLines as line}
					<tr
						class="{line.type === 'add'
							? 'bg-green-50 dark:bg-green-950/30'
							: line.type === 'remove'
								? 'bg-red-50 dark:bg-red-950/30'
								: ''}"
					>
						<td
							class="select-none text-right px-2 py-0 text-gray-400 border-r border-gray-200 dark:border-gray-700 w-10 shrink-0"
						>
							{line.oldLineNum ?? ''}
						</td>
						<td
							class="select-none text-right px-2 py-0 text-gray-400 border-r border-gray-200 dark:border-gray-700 w-10 shrink-0"
						>
							{line.newLineNum ?? ''}
						</td>
						<td class="select-none px-1 py-0 w-4 text-center shrink-0">
							{#if line.type === 'add'}
								<span class="text-green-600 dark:text-green-400">+</span>
							{:else if line.type === 'remove'}
								<span class="text-red-600 dark:text-red-400">-</span>
							{/if}
						</td>
						<td class="px-2 py-0 whitespace-pre-wrap break-all">
							{line.content}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>
