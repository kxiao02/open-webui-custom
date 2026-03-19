<script lang="ts">
	import CodeEditor from '$lib/components/common/CodeEditor.svelte';
	import { canvasState } from '$lib/stores';

	let codeEditor;

	$: lang = $canvasState?.lang ?? '';
	$: code = $canvasState?.code ?? '';
</script>

<div class="h-full w-full">
	<CodeEditor
		bind:this={codeEditor}
		value={code}
		id="canvas-editor"
		{lang}
		onChange={(value) => {
			if ($canvasState) {
				$canvasState = { ...$canvasState, code: value };
			}
		}}
		onSave={() => {}}
	/>
</div>
