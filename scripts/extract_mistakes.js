/**
 * scripts/extract_mistakes.js —— QuickAdd 用户脚本（对应 README 第四部分 · 阶段 2）
 *
 * 挂载方式：
 *   QuickAdd 设置 → Macros → 新建「提取错题」 → User Script 指到本文件
 *   → 在 Macro 里加到 Command → 首页添加该 Macro → Hotkeys 绑定全局快捷键
 *
 * 功能：
 *  1. 扫描当前活动笔记中所有「标题行带 #错题」的区块（从该级标题开始，到下一个同级或更高级标题为止，
 *     完整保留题干、media-lib/PDF++ 截图、"我的错误结果"、以及原版 Ink 插入块
 *     （<!-- ink: Ink/xxx.writing --> 注释 + ```handwritten-ink 代码块）。
 *  2. 按标准格式追加到  08.错题与重测库/数二错题|408错题/<当前文件名>_错题集.md，
 *     靠笔记所在目录自动判断科目（可在下方 SUBJECT_RULES 里增减关键字）。
 *  3. 每个错题区块底部预留占位符：💡 AI 思维分叉点与标准解析 / 🎯 核心考点标签。
 *  4. 原题本中所有 #错题 替换为 #已归档错题，防止重复提取；已归档过的题自动跳过。
 *
 * 输出的归档块格式（scripts/random_exam.js 依赖此格式解析，勿改标记）：
 *   <!-- mistake: {"src":"…/原笔记.md","heading":"05…","date":"2026-10-24"} -->
 *   > 📌 来源：[[原笔记]]
 *   <原题标题行 + 题干…>
 *   **💡 AI 思维分叉点与标准解析**：
 *   **🎯 核心考点标签**：
 *   ---
 */

// ===== 可调配置 =====
const ARCHIVE_ROOT = "08.错题与重测库"; // 错题库根目录
const DEFAULT_SUBJECT = "数二错题"; // 目录关键字都没命中时归到该目录
const MISTAKE_TAG = "#错题";
const ARCHIVED_TAG = "#已归档错题";
const AI_ANALYSIS_PLACEHOLDER = "**💡 AI 思维分叉点与标准解析**：";
const EXAM_TAGS_PLACEHOLDER = "**🎯 核心考点标签**：";

// 科目分流规则：当前笔记路径包含 keys 中任一关键字 → 归入 folder
const SUBJECT_RULES = [
	{
		folder: "408错题",
		keys: ["04.数据结构", "05.计算机组成原理", "06.操作系统", "07.计算机网络"],
	},
	{
		folder: "数二错题",
		keys: ["01.高数", "02.线代", "03.数二题库"],
	},
];

// ===== 工具 =====
let NoticeCtor = null;
try {
	NoticeCtor = require("obsidian").Notice;
} catch (e) {
	NoticeCtor = null;
}
function toast(msg) {
	console.log("[22408·提取错题]", msg);
	try {
		if (NoticeCtor) new NoticeCtor(msg);
	} catch (e) {
		/* 忽略通知失败 */
	}
}

function resolveApp(params) {
	if (params && params.app) return params.app;
	if (params && params.quickAddApi && params.quickAddApi.app) return params.quickAddApi.app;
	if (typeof window !== "undefined" && window.app) return window.app;
	throw new Error("拿不到 Obsidian app，请确认用 QuickAdd 挂载本脚本");
}

const TAG_RE = /(^|\s)#错题(?=\s|$)/g; // 独立成词的 #错题（不会误伤 #已归档错题 / #顽固错题）
const TAG_RE_TEST = /(^|\s)#错题(?=\s|$)/;
/** 标题清洗：去掉末尾各种状态标签（#错题/#已归档错题/#已攻克/#顽固错题），
 *  统一用于归档标题与去重 key——否则第二次运行时标题里残留的 #已归档错题 会让 key 变掉，导致重复归档 */
const TITLE_TAG_CLEAN_RE = /(^|\s)#(错题|已归档错题|已攻克|顽固错题)(?=\s|$)/g;
function cleanTitle(title) {
	return title.replace(TITLE_TAG_CLEAN_RE, "$1").replace(/[ \t]+$/, "");
}

function parseHeadingLine(text) {
	const m = /^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*$/.exec(text || "");
	return m ? { level: m[1].length, title: m[2] } : null;
}

/** 找出所有带 #错题 的标题区块：{level,title,start,end}（end 不含，指向下一个同级/更高级标题或文末）。
 *  标签锚点有两种写法都支持：直接写在标题行末尾，或写在区块内正文行（如 "> 我的错误结果：… #错题"），
 *  此时归属到该标签行上方的最近标题；同一区块吃到多个标签只归档一次；嵌套的子标题区块去重不重复提取。 */
function findMistakeBlocks(lines) {
	const tagLineIdx = [];
	lines.forEach((line, idx) => {
		if (TAG_RE_TEST.test(line)) tagLineIdx.push(idx);
	});
	if (tagLineIdx.length === 0) return [];

	// 每个标签行归属到其上方最近的标题（标签行本身是标题则取自身）
	const anchorHeadings = new Map(); // start -> heading
	for (const idx of tagLineIdx) {
		const h = findEnclosingHeading(lines, idx);
		if (!h) continue;
		if (!anchorHeadings.has(h.start)) anchorHeadings.set(h.start, h);
	}

	const blocks = [];
	for (const h of anchorHeadings.values()) {
		let end = h.start + 1;
		while (end < lines.length) {
			const h2 = parseHeadingLine(lines[end]);
			if (h2 && h2.level <= h.level) break;
			end++;
		}
		blocks.push({ level: h.level, title: cleanTitle(h.title), start: h.start, end });
	}
	blocks.sort((a, b) => a.start - b.start);

	// 去掉完全嵌套在前面区块内部的重复块
	const result = [];
	let lastEnd = -1;
	for (const b of blocks) {
		if (b.start < lastEnd && b.end <= lastEnd) continue; // 已包含在前一块里
		result.push(b);
		lastEnd = Math.max(lastEnd, b.end);
	}
	return result;
}

function findEnclosingHeading(lines, idx) {
	for (let i = idx; i >= 0; i--) {
		const h = parseHeadingLine(lines[i]);
		if (h) return { start: i, level: h.level, title: h.title };
	}
	return null;
}

function stripEmptyEnds(lines) {
	let start = 0;
	let end = lines.length;
	while (start < end && lines[start].trim() === "") start++;
	while (end > start && lines[end - 1].trim() === "") end--;
	return lines.slice(start, end);
}

function subjectForPath(path) {
	for (const rule of SUBJECT_RULES) {
		if (rule.keys.some((k) => path.includes(k))) return rule.folder;
	}
	return DEFAULT_SUBJECT;
}

function buildMetaComment(srcPath, heading, dateStr) {
	return JSON.stringify({ src: srcPath, heading, date: dateStr });
}

/** 组装一条归档块（含占位符与 mistake 标记）。
 *  格式：marker → 来源 → 原题标题行（只保留一次，不再额外造 ## 标题）→ 题干 → AI 占位符 → 分隔线 */
function buildArchiveBlock(block, lines, srcFile, dateStr) {
	const headLine = lines[block.start].replace(TAG_RE, "$1").replace(/[ \t]+$/, "");
	const bodyLines = stripEmptyEnds(lines.slice(block.start + 1, block.end)).map((l) => l.replace(TAG_RE, "$1"));
	return [
		"<!-- mistake: " + buildMetaComment(srcFile.path, block.title, dateStr) + " -->",
		"",
		"> 📌 来源：[[" + srcFile.basename + "]]",
		"",
		headLine,
		bodyLines.join("\n"),
		"",
		AI_ANALYSIS_PLACEHOLDER,
		"",
		EXAM_TAGS_PLACEHOLDER,
		"",
		"---",
		"",
	].join("\n");
}

/** 解析已有错题集中的 mistake 标记，用于去重 */
function parseMistakeMetas(text) {
	const metas = [];
	for (const line of text.split(/\r?\n/)) {
		const t = line.trim();
		if (!t.startsWith("<!-- mistake:")) continue;
		const s = t.indexOf("{");
		const e = t.lastIndexOf("}");
		if (s < 0 || e <= s) continue;
		try {
			metas.push(JSON.parse(t.slice(s, e + 1)));
		} catch (err) {
			/* 标记损坏则忽略 */
		}
	}
	return metas;
}

// ===== 主流程 =====
async function extractMistakes(params) {
	const app = resolveApp(params);
	const activeFile = app.workspace.getActiveFile();
	if (!activeFile || activeFile.extension !== "md") {
		toast("当前没有活动的 Markdown 笔记，先打开题本再运行。");
		return;
	}

	const sourceText = await app.vault.read(activeFile);
	const lines = sourceText.split(/\r?\n/);
	const blocks = findMistakeBlocks(lines);

	if (blocks.length === 0) {
		toast("本页没有发现带 #错题 的标题区块，无需提取。");
		return;
	}

	// 1. 计算归档目标文件
	const subject = subjectForPath(activeFile.path);
	const targetDir = `${ARCHIVE_ROOT}/${subject}`;
	const targetPath = `${targetDir}/${activeFile.basename}_错题集.md`;
	const today = new Date();
	const dateStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;

	// 2. 去重（同一来源笔记 + 同一标题只归档一次）
	const existingText = (await readIfExists(app, targetPath)) || "";
	const seen = new Set(
		parseMistakeMetas(existingText).map((m) => `${m.src}::${m.heading}`),
	);

	const newBlocks = [];
	const skipped = [];
	for (const b of blocks) {
		const key = `${activeFile.path}::${b.title}`;
		if (seen.has(key)) {
			skipped.push(b.title);
			continue;
		}
		seen.add(key);
		newBlocks.push(buildArchiveBlock(b, lines, activeFile, dateStr));
	}

	if (newBlocks.length === 0) {
		toast(`发现 ${blocks.length} 个错题区块，但全部已归档过，跳过。`);
		return;
	}

	// 3. 写入错题集（文件名本身就是标题，正文不再重复造 H1）
	let headerText = "";
	if (!existingText) {
		headerText = [
			"> ℹ️ 由 `scripts/extract_mistakes.js` 自动归档；AI 批改按 `AGENTS.md` 错题批改规则执行（结合 `09.答案库/` 与题下文字备注，自动跳过 Ink 画布）。",
			"",
		].join("\n");
	} else if (!existingText.endsWith("\n")) {
		await appendText(app, targetPath, "\n");
	}
	const archiveText = headerText + newBlocks.join("\n");
	await appendText(app, targetPath, archiveText);

	// 4. 原题本 #错题 → #已归档错题
	const replacedText = sourceText.replace(TAG_RE, "$1" + ARCHIVED_TAG);
	await writeText(app, activeFile, replacedText);

	toast(
		`✅ 已提取 ${newBlocks.length} 道错题 → ${targetPath}` +
			(skipped.length ? `（跳过已归档 ${skipped.length} 题）` : ""),
	);
}

// ===== 兼容旧版 Obsidian 的读写封装 =====
async function readIfExists(app, path) {
	const ref = app.vault.getAbstractFileByPath(path);
	if (!ref) return null;
	try {
		return await app.vault.read(ref);
	} catch (e) {
		return null;
	}
}

async function appendText(app, path, text) {
	try {
		if (!app.vault.getAbstractFileByPath(path)) {
			await ensureFolder(app, path.split("/").slice(0, -1).join("/"));
			await app.vault.create(path, "");
		}
		await app.vault.process(path, (cur) => (cur && cur.length ? cur.trimEnd() + "\n\n" + text : text));
	} catch (e) {
		// 旧版没有 vault.process
		const ref = app.vault.getAbstractFileByPath(path);
		const cur = ref ? await app.vault.read(ref) : "";
		await app.vault.modify(ref, cur.trimEnd() + "\n\n" + text);
	}
}

async function writeText(app, file, text) {
	try {
		await app.vault.process(file, () => text);
	} catch (e) {
		await app.vault.modify(file, text);
	}
}

async function ensureFolder(app, folderPath) {
	if (!folderPath) return;
	const parts = folderPath.split("/");
	let cur = [];
	for (const p of parts) {
		cur.push(p);
		const path = cur.join("/");
		if (!app.vault.getAbstractFileByPath(path)) {
			try {
				await app.vault.createFolder(path);
			} catch (e) {
				/* 已存在或并发创建 */
			}
		}
	}
}

module.exports = extractMistakes;
// 内部实现暴露给单元测试用（QuickAdd 只认函数本体，不影响挂载）
module.exports._internal = {
	findMistakeBlocks,
	buildArchiveBlock,
	parseMistakeMetas,
	subjectForPath,
	stripEmptyEnds,
	buildMetaComment,
};
