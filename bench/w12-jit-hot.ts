// W12 test extension: JIT work at load (a table built in a hot loop, as plugin init code does), on every streamed message
// update and tool result during a turn (a hash of the text), and on demand (/hot: a word count over 8 MB, timed in Pi).
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function fnv(text: string, rounds: number): number {
	let h = 0x811c9dc5;
	for (let r = 0; r < rounds; r++) {
		for (let i = 0; i < text.length; i++) {
			h ^= text.charCodeAt(i);
			h = Math.imul(h, 0x01000193) >>> 0;
		}
	}
	return h;
}

function countWords(text: string): number {
	let words = 0;
	let inWord = false;
	for (let i = 0; i < text.length; i++) {
		const c = text.charCodeAt(i);
		const space = c === 32 || (c >= 9 && c <= 13) || c === 160;
		if (!space && !inWord) words++;
		inWord = !space;
	}
	return words;
}

interface Entry {
	key: string;
	value: number;
	tags: string[];
}

// Load: build and index a table, the way a plugin prepares its data.
const loadStart = performance.now();
const table: Entry[] = [];
for (let i = 0; i < 60000; i++) {
	const key = `k${i.toString(36)}`;
	table.push({ key, value: fnv(key, 4), tags: i % 3 === 0 ? ["a", key] : [key] });
}
const index = new Map<string, Entry>();
for (const e of table) index.set(e.key, e);
let check = 0;
for (let i = 0; i < 60000; i += 7) check ^= index.get(`k${i.toString(36)}`)?.value ?? 0;
const loadMs = performance.now() - loadStart;

const text = "lorem ipsum dolor sit amet, consectetur adipiscing elit\n".repeat(150000);

export default function jitHot(pi: ExtensionAPI) {
	let updates = 0;
	let acc = check;
	pi.on("message_update", (event) => {
		updates++;
		acc ^= fnv(JSON.stringify(event.message).slice(-400), 20);
	});
	pi.on("tool_result", (event) => {
		acc ^= fnv(JSON.stringify(event.content).slice(0, 4000), 4);
	});
	pi.registerCommand("hot", {
		description: "W12 JIT test",
		handler: async (_args, ctx) => {
			const start = performance.now();
			const words = countWords(text) + (acc & 1);
			ctx.ui.notify(`hot ${words} words (${(performance.now() - start).toFixed(2)} ms) load ${loadMs.toFixed(1)} ms updates ${updates}`, "info");
		},
	});
}
