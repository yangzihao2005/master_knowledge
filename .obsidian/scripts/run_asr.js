module.exports = async (params) => {
    const { exec } = require("child_process");
    const scriptPath = "D:\\master_knowledge\\scripts\\mimo_asr_batch.py";
    const folderPath = "D:\\考研";

    // 用 start 命令弹出一个新的 cmd 窗口，这样你能看到进度条
    const cmd = `start cmd /k python "${scriptPath}" "${folderPath}"`;

    exec(cmd, (error, stdout, stderr) => {
        if (error) {
            new Notice("ASR脚本启动失败: " + error.message);
            return;
        }
        new Notice("ASR脚本已启动，请查看弹出的终端窗口。");
    });
};