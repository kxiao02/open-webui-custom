import adapter from '@sveltejs/adapter-static';
import * as child_process from 'node:child_process';
import crypto from 'node:crypto';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import fs from 'node:fs';

const resolveVersionName = () => {
	if (process.env.APP_BUILD_HASH && process.env.APP_BUILD_HASH !== 'dev-build') {
		return process.env.APP_BUILD_HASH;
	}

	try {
		const head = child_process.execSync('git rev-parse HEAD').toString().trim();
		const diff = child_process.execSync('git diff --binary HEAD -- .').toString();
		const untrackedFiles = child_process
			.execSync('git ls-files --others --exclude-standard -- .')
			.toString()
			.split('\n')
			.map((file) => file.trim())
			.filter(Boolean)
			.sort();

		if (!diff && untrackedFiles.length === 0) {
			return head;
		}

		const hash = crypto.createHash('sha1');
		hash.update(diff);
		for (const file of untrackedFiles) {
			hash.update(file);
			hash.update(fs.readFileSync(new URL(file, import.meta.url)));
		}

		const diffHash = hash.digest('hex').slice(0, 12);
		return `${head}-dirty-${diffHash}`;
	} catch {
		try {
			return (
				JSON.parse(fs.readFileSync(new URL('./package.json', import.meta.url), 'utf8'))?.version ||
				Date.now().toString()
			);
		} catch {
			return Date.now().toString();
		}
	}
};

/** @type {import('@sveltejs/kit').Config} */
const config = {
	// Consult https://kit.svelte.dev/docs/integrations#preprocessors
	// for more information about preprocessors
	preprocess: vitePreprocess(),
	kit: {
		// adapter-auto only supports some environments, see https://kit.svelte.dev/docs/adapter-auto for a list.
		// If your environment is not supported or you settled on a specific environment, switch out the adapter.
		// See https://kit.svelte.dev/docs/adapters for more information about adapters.
		adapter: adapter({
			pages: 'build',
			assets: 'build',
			fallback: 'index.html'
		}),
		// poll for new version name every 60 seconds (to trigger reload mechanic in +layout.svelte)
		version: {
			name: resolveVersionName(),
			pollInterval: 60000
		}
	},
	vitePlugin: {
		// inspector: {
		// 	toggleKeyCombo: 'meta-shift', // Key combination to open the inspector
		// 	holdMode: false, // Enable or disable hold mode
		// 	showToggleButton: 'always', // Show toggle button ('always', 'active', 'never')
		// 	toggleButtonPos: 'bottom-right' // Position of the toggle button
		// }
	},
	onwarn: (warning, handler) => {
		const { code } = warning;
		if (code === 'css-unused-selector') return;

		handler(warning);
	}
};

export default config;
