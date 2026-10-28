/**
 * scripts/run_asr.js —— QuickAdd 用户脚本（README 二、ASR 一键启动入口）
 * 功能：弹出新终端窗口运行 mimo_asr_batch.py，批量转写指定文件夹里的网课视频。
 *       转写结果统一输出到仓库 10.转写结果/。
 * 绑定：QuickAdd 宏「ASR批量转写」→ User Script（快捷键 Ctrl+Shift+A）。
 */
const VIDEO_FOLDER = "F:\\408视频"; // 网课视频所在目录（要换目录改这里）
const ASR_SCRIPT = "D:\\master_knowledge\\scripts\\mimo_asr_batch.py";
const OUTPUT_DIR = "D:\\master_knowledge\\10.转写结果"; // 转写结果统一输出到仓库 10.转写结果/

let NoticeCtor = null;
try {
	NoticeCtor = require("obsidian").Notice;
} catch (e) {
	NoticeCtor = null;
}
function toast(msg) {
	console.log("[22408·ASR]", msg);
	try {
		if (NoticeCtor) new NoticeCtor(msg);
	} catch (e) {
		/* 忽略通知失败 */
	}
}

module.exports = async (params) => {
	const { exec } = require("child_process");
	const folderName = VIDEO_FOLDER.split("\\").filter(Boolean).pop() || "video";
	const outFile = `${OUTPUT_DIR}\\${folderName}_转写结果.txt`;
	// 显式 -o 把结果定到 10.转写结果/，避免默认输出落在视频源目录产生路径冲突
	const cmd = `start cmd /k python "${ASR_SCRIPT}" "${VIDEO_FOLDER}" -o "${outFile}"`;
	exec(cmd, (error) => {
		if (error) {
			toast("ASR 脚本启动失败：" + error.message);
			return;
		}
		toast("ASR 脚本已启动，结果输出到 10.转写结果/，请查看弹出的终端窗口。");
	});
};
