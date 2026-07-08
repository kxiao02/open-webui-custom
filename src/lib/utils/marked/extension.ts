// Helper function to find matching closing tag
function findMatchingClosingTag(src: string, openTag: string, closeTag: string): number {
	const lowerSrc = src.toLowerCase();
	const lowerOpenTag = openTag.toLowerCase();
	const lowerCloseTag = closeTag.toLowerCase();

	const firstOpenIndex = lowerSrc.indexOf(lowerOpenTag);
	if (firstOpenIndex === -1) {
		return -1;
	}

	let depth = 1;
	let index = firstOpenIndex + lowerOpenTag.length;

	while (depth > 0 && index < lowerSrc.length) {
		const nextOpenIndex = lowerSrc.indexOf(lowerOpenTag, index);
		const nextCloseIndex = lowerSrc.indexOf(lowerCloseTag, index);

		if (nextCloseIndex === -1) {
			return -1;
		}

		if (nextOpenIndex !== -1 && nextOpenIndex < nextCloseIndex) {
			depth++;
			index = nextOpenIndex + lowerOpenTag.length;
		} else {
			depth--;
			index = nextCloseIndex + lowerCloseTag.length;
		}
	}

	return depth === 0 ? index : -1;
}

// Function to parse attributes from tag
function parseAttributes(tag: string): { [key: string]: string } {
	const attributes: { [key: string]: string } = {};
	const attrRegex = /(\w+)="(.*?)"/g;
	let match;
	while ((match = attrRegex.exec(tag)) !== null) {
		attributes[match[1]] = match[2];
	}
	return attributes;
}

function detailsTokenizer(src: string) {
	// Accept common variants:
	// - `<details ...>\n`
	// - `<details ...>` (no trailing newline)
	// - compact malformed tag like `<detailstype="reasoning"...>`
	const detailsRegex =
		/^\s*<details(?:(\s+[^>]*)|(?=[a-zA-Z_:][-a-zA-Z0-9_:.]*=)([^>]*))?>\s*/i;
	const summaryRegex = /^<summary>([\s\S]*?)<\/summary>\s*/i;

	const detailsMatch = detailsRegex.exec(src);
	if (detailsMatch) {
		const endIndex = findMatchingClosingTag(src, '<details', '</details>');
		if (endIndex === -1) return;

		const fullMatch = src.slice(0, endIndex);
		const detailsTag = detailsMatch[0];
		const attributes = parseAttributes(detailsTag); // Parse attributes from <details>

		let content = fullMatch.slice(detailsTag.length, -10).trim(); // Remove <details> and </details>
		let summary = '';

		const summaryMatch = summaryRegex.exec(content);
		if (summaryMatch) {
			summary = summaryMatch[1].trim();
			content = content.slice(summaryMatch[0].length).trim();
		}

		return {
			type: 'details',
			raw: fullMatch,
			summary: summary,
			text: content,
			attributes: attributes // Include extracted attributes from <details>
		};
	}
}

function detailsStart(src: string) {
	return src.match(/^\s*<details(?:\b|(?=[a-zA-Z_:][-a-zA-Z0-9_:.]*=))/i) ? 0 : -1;
}

function detailsRenderer(token: any) {
	const attributesString = token.attributes
		? Object.entries(token.attributes)
				.map(([key, value]) => `${key}="${value}"`)
				.join(' ')
		: '';

	return `<details ${attributesString}>
  ${token.summary ? `<summary>${token.summary}</summary>` : ''}
  ${token.text}
  </details>`;
}

// Extension wrapper function
function detailsExtension() {
	return {
		name: 'details',
		level: 'block',
		start: detailsStart,
		tokenizer: detailsTokenizer,
		renderer: detailsRenderer
	};
}

export default function () {
	return {
		extensions: [detailsExtension()]
	};
}
