/**
 * scripts/random_exam.js —— QuickAdd 用户脚本（对应 README 第五部分 · 周期性复盘）
 *
 * 挂载方式：QuickAdd 设置 → Macros → 新建「随机重测组卷」 → User Script 指到本文件 → 绑快捷键
 *
 * 功能：
 *  1. 从 3.复盘与题库/02.错题与重测库/数二错题 与 408错题（可选附带根目录老文件 错题测试.md）下所有 *_错题集.md
 *     解析出全部错题（依赖 extract_mistakes.js 写入的 <!-- mistake: {...} --> 标记）。
 *  2. 加权随机抽取 10~15 题：#顽固错题 权重最高优先重测，#已攻克 权重极低。
 *  3. 生成重测卷到 3.复盘与题库/02.错题与重测库/抽测组卷/<日期> 错题重测卷.md，**只保留题干与 _system/media-lib/PDF++ 配图**：
 *     自动剔除 旧 Ink 手稿（<!-- ink: --> 注释 + ```handwritten-ink/-d 代码块 + [!pencil] 残留）、
 *     AI 解析（💡 分叉点 / 🎯 考点标签 段）、"> 我的错误结果" 行。
 *  4. 二刷判定：秒杀做对 → 回原错题集标 #已攻克；依然做错 → 标 #顽固错题。
 */

// ===== 可调配置 =====
const ARCHIVE_FOLDERS = ["3.复盘与题库/02.错题与重测库/数二错题", "3.复盘与题库/02.错题与重测库/408错题"]; // 错题集扫描目录
const EXTRA_ARCHIVE_FILES = ["3.复盘与题库/02.错题与重测库/错题测试.md"]; // 根目录迁移过来的历史错题件（存在才收录）
const OUTPUT_DIR = "3.复盘与题库/02.错题与重测库/抽测组卷";
const COUNT_RANGE = [10, 15]; // 随机抽题数量范围（题库不足时取全部）
const WEIGHTS = { stubborn: 5, conquered: 0.2, normal: 1 }; // 抽中权重

// ===== 工具 =====
let NoticeCtor = null;
try {
	NoticeCtor = require("obsidian").Notice;
} catch (e) {
	NoticeCtor = null;
}
function toast(msg) {
	console.log("[22408·随机组卷]", msg);
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

// ===== 错题解析（与 extract_mistakes.js 的归档格式对应）=====
const META_PREFIX = "<!-- mistake:";
/** 文末复盘/总结的收尾哨兵：遇到它即结束当前错题块，避免总结被吸进最后一题的题干 */
const END_MARKER_RE = /^\s*<!--\s*mistakes-end\s*-->\s*$/;

function parseMetaComment(line) {
	const t = line.trim();
	if (!t.startsWith(META_PREFIX)) return null;
	const s = t.indexOf("{");
	const e = t.lastIndexOf("}");
	if (s < 0 || e <= s) return null;
	try {
		return JSON.parse(t.slice(s, e + 1));
	} catch (err) {
		return null;
	}
}

function collectMistakeBlocks(text) {
	const lines = text.split(/\r?\n/);
	const blocks = [];
	let cur = null;
	for (const line of lines) {
		if (END_MARKER_RE.test(line)) {
			if (cur && cur.lines.length) blocks.push(cur);
			cur = null;
			continue;
		}
		if (line.trim().startsWith(META_PREFIX)) {
			if (cur && cur.lines.length) blocks.push(cur);
			const meta = parseMetaComment(line);
			cur = meta ? { meta, lines: [] } : null;
			continue;
		}
		if (cur) cur.lines.push(line);
	}
	if (cur && cur.lines.length) blocks.push(cur);
	return blocks;
}

const AI_ANALYSIS_RE = /💡\s*AI 思维分叉点与标准解析/;
const EXAM_TAGS_RE = /🎯\s*核心考点标签/;
const MY_RESULT_RE = /我的错误(结果|答案|做法|过程)/;
const INK_FENCE_RE = /^\s*(?:>\s*)?```[ \t]*(handwritten-ink|handdrawn-ink)[ \t]*$/;
const FENCE_RE = /^\s*(?:>\s*)?```/;
const INK_COMMENT_RE = /^\s*(?:>\s*)?<!--\s*ink:/;
const PENCIL_CALLOUT_RE = /^\s*(?:>\s*)?\[!pencil\]/;
const RETEST_TAG_RE = /(^|\s)#(错题|已归档错题|已攻克|顽固错题)(?=\s|$)/g;

/** 生成重测卷用的"净题干"：剔除 Ink 手稿、AI 解析段、我的错误结果与状态标签，只留题干和 media-lib 配图。
 *  归档块里的第一行标题（原题标题）会被单独剔除——重测卷用 meta.heading 重新生成标题，避免重复。 */
function stripForRetest(blockLines) {
	const out = [];
	let inInkFence = false;
	let inAnalysis = false;
	let titleSkipped = false;
	for (const rawLine of blockLines) {
		const line = rawLine.replace(RETEST_TAG_RE, "$1");
		if (INK_FENCE_RE.test(line)) {
			inInkFence = true;
			continue;
		}
		if (inInkFence) {
			if (FENCE_RE.test(line)) inInkFence = false;
			continue;
		}
		if (INK_COMMENT_RE.test(line)) continue;
		if (PENCIL_CALLOUT_RE.test(line)) continue;
		// AI 解析是一整段（💡 行 → 🎯 行之间），必须整段剔除，否则答案会漏进重测卷
		if (AI_ANALYSIS_RE.test(line)) {
			inAnalysis = true;
			continue;
		}
		if (inAnalysis) {
			if (EXAM_TAGS_RE.test(line)) inAnalysis = false;
			continue;
		}
		if (EXAM_TAGS_RE.test(line)) continue;
		if (MY_RESULT_RE.test(line)) continue;
		if (line.trim().startsWith("> 📌 来源：")) continue;
		if (!titleSkipped && /^ {0,3}#{1,6}[ \t]/.test(line)) {
			titleSkipped = true;
			continue;
		}
		out.push(line);
	}
	// 去掉首尾空行并压掉连续 3 行以上的空行
	let res = [];
	for (const l of out) {
		if (l.trim() === "" && (res.length === 0 || res[res.length - 1].trim() === "")) continue;
		res.push(l);
	}
	while (res.length && res[res.length - 1].trim() === "") res.pop();
	while (res.length && res[0].trim() === "") res.shift();
	return res;
}

/** 抽样权重：#顽固错题 最容易抽中，#已攻克 基本不抽 */
function weightForBlock(blockLines) {
	const text = blockLines.join("\n");
	if (/(^|\s)#顽固错题(?=\s|$)/.test(text)) return WEIGHTS.stubborn;
	if (/(^|\s)#已攻克(?=\s|$)/.test(text)) return WEIGHTS.conquered;
	return WEIGHTS.normal;
}

/** 加权不放回抽样 */
function weightedSample(items, n) {
	const pool = items.map((it) => ({ it, w: Math.max(0.0001, it.weight) }));
	const picked = [];
	n = Math.min(n, pool.length);
	for (let k = 0; k < n; k++) {
		const total = pool.reduce((s, x) => s + x.w, 0);
		let r = Math.random() * total;
		let idx = pool.length - 1;
		for (let i = 0; i < pool.length; i++) {
			r -= pool[i].w;
			if (r <= 0) {
				idx = i;
				break;
			}
		}
		picked.push(pool[idx].it);
		pool.splice(idx, 1);
	}
	return picked;
}

function randInt(min, max) {
	return min + Math.floor(Math.random() * (max - min + 1));
}

function basenameNoExt(path) {
	const base = path.split("/").pop() || path;
	return base.replace(/\.md$/, "");
}

// ===== 主流程 =====
async function randomExam(params) {
	const app = resolveApp(params);

	// 1. 收集错题库文件
	const allFiles = app.vault.getMarkdownFiles();
	const files = [
		...allFiles.filter((f) =>
			ARCHIVE_FOLDERS.some((dir) => f.path.startsWith(dir.replace(/\/+$/, "") + "/")),
		),
		...EXTRA_ARCHIVE_FILES.map((p) => allFiles.find((f) => f.path === p)).filter(Boolean),
	];
	if (files.length === 0) {
		toast("错题库里还没有任何错题集，先用「提取错题」归档一批。");
		return;
	}

	// 2. 解析出全部错题块并计算净题干
	const poolItems = [];
	for (const file of files) {
		const text = await app.vault.read(file);
		for (const block of collectMistakeBlocks(text)) {
			const stem = stripForRetest(block.lines);
			if (!block.meta.src || !block.meta.heading || stem.length === 0) continue;
			poolItems.push({ file, meta: block.meta, stem, weight: weightForBlock(block.lines) });
		}
	}
	if (poolItems.length === 0) {
		toast("解析不到可用错题（未发现 <!-- mistake: --> 标记）。");
		return;
	}

	// 3. 加权随机抽题
	const maxCount = Math.min(COUNT_RANGE[1], poolItems.length);
	const minCount = Math.min(COUNT_RANGE[0], maxCount);
	const n = randInt(minCount, maxCount);
	const picked = weightedSample(poolItems, n);

	// 4. 组卷
	const now = new Date();
	const dateStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
	const quesionTexts = picked.map((item) => {
		const srcLink = `[[${basenameNoExt(item.meta.src)}]]`;
		return [
			`## ${item.meta.heading}`,
			"",
			`> 🔗 来源：${srcLink}　做完改判：\`#已攻克\` / \`#顽固错题\``,
			"",
			item.stem.join("\n"),
			"",
			"> ✍️ 做题区（光标放这行下面，直接插 Ink 画布手写重做）",
			"",
			"---",
			"",
		].join("\n");
	});

	const examText = [
		`> 🎲 本次随机抽出 **${picked.length}** 题（候选共 ${poolItems.length} 题）。做完回原错题集改标签，\`#顽固错题\` 下次优先抽中`,
		"> 🖊️ 单窗口铁律：先手写重做，再对答案，不看解析。",
		"",
		quesionTexts.join("\n"),
	].join("\n");

	// 5. 写入抽测组卷目录（重名自动加序号）
	await ensureFolder(app, OUTPUT_DIR);
	let targetPath = `${OUTPUT_DIR}/${dateStr} 错题重测卷.md`;
	for (let v = 2; app.vault.getAbstractFileByPath(targetPath); v++) {
		targetPath = `${OUTPUT_DIR}/${dateStr} 错题重测卷 (${v}).md`;
	}
	await app.vault.create(targetPath, examText);

	toast(`✅ 重测卷已生成（${picked.length} 题）→ ${targetPath}`);
}

async function ensureFolder(app, folderPath) {
	if (!folderPath) return;
	const parts = folderPath.split("/");
	let curPath = [];
	for (const p of parts) {
		curPath.push(p);
		const path = curPath.join("/");
		if (!app.vault.getAbstractFileByPath(path)) {
			try {
				await app.vault.createFolder(path);
			} catch (e) {
				/* 已存在或并发创建 */
			}
		}
	}
}

module.exports = randomExam;
// 内部实现暴露给单元测试用（QuickAdd 只认函数本体，不影响挂载）
module.exports._internal = {
	collectMistakeBlocks,
	stripForRetest,
	weightForBlock,
	weightedSample,
	parseMetaComment,
};
