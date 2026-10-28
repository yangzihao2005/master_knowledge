#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mimo_asr_batch.py —— 用小米 MiMo ASR（mimo-v2.5-asr）批量转写考研网课音频

功能：
  1. 遍历指定文件夹里的视频 / 音频文件
  2. 用 ffmpeg 提取音频并转成 mp3（16 kHz 单声道，省流量省空间）
  3. 超过 1 分钟的音频自动按时间段切片，避免超出模型上下文
  4. 每个切片：读取 → Base64 编码 → data URL → input_audio 请求体 → 调用 API
  5. 每次 API 调用之间固定间隔 0.6 秒（RPM=100 限流保护）
  6. 遇到 429 / 5xx / 网络错误自动重试，指数退避（带抖动）
  7. 所有识别结果按顺序合并输出为一个 txt，附带进度条与错误日志

依赖：
  pip install requests
  系统需安装 ffmpeg（保证 ffmpeg、ffprobe 在 PATH 中）
  Windows 可用: winget install Gyan.FFmpeg   或   choco install ffmpeg

用法：
  python mimo_asr_batch.py "D:/网课音频"
  python mimo_asr_batch.py "D:/网课音频" -o "D:/网课音频/转写结果.txt"
  python mimo_asr_batch.py "D:/网课音频" --fresh        # 忽略缓存，全部重跑
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先执行: pip install requests", file=sys.stderr)
    sys.exit(1)


# =====================================================================
# ★★★★★★  需要你填入 API Key 的位置  ★★★★★★
#
#   把你的密钥写进下面这行的引号里，例如：
#       API_KEY = "sk-abcdefgh12345678"
#
#   若这里留空，脚本会自动读取环境变量 MIMO_API_KEY（推荐做法，
#   可以避免密钥被误提交 / 误分享）：
#       Windows PowerShell:  $env:MIMO_API_KEY = "sk-xxxx"
#       Linux / macOS:       export MIMO_API_KEY="sk-xxxx"
# =====================================================================
API_KEY = "sk-c7cmgkek911ur1e35ygzge9iflgwxkwmdp7mcsp520bsfi2v"   # ← 【在这里填入你的 API Key】
# =====================================================================


API_URL = "https://api.xiaomimimo.com/v1/chat/completions"
MODEL = "mimo-v2.5-asr"          # 模型名固定为 mimo-v2.5-asr

AUDIO_EXTS = {".wav", ".mp3"}
VIDEO_EXTS = {
    ".mp4", ".m4a", ".flac", ".aac", ".mkv", ".mov", ".webm",
    ".wma", ".ogg", ".opus", ".ts", ".flv", ".avi", ".wmv",
}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS

DEFAULT_SLICE_MINUTES = 1        # 超过 1 分钟就切片
DEFAULT_INTERVAL = 0.6           # 两次 API 调用之间的间隔（秒）
MAX_RETRIES = 6                  # 429 / 5xx / 网络错误的最大重试次数
MAX_B64_BYTES = 10 * 1024 * 1024  # Base64 编码后不超过 10 MB
CHUNK_BITRATE = "48k"            # 16kHz 单声道 48kbps，7 分钟 ≈ 2.5 MB，远低于 10 MB 限制

log = logging.getLogger("mimo_asr")


# ----------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------
def force_utf8_stdio() -> None:
    """Windows 管道/重定向下默认用 GBK 编码，中文会变乱码，统一改成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (stream.encoding or "").lower().replace("-", "")
            if enc != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def hms(seconds: float) -> str:
    """秒 → HH:MM:SS"""
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def progress(done: int, total: int, msg: str) -> None:
    """简单的控制台进度条（纯 ASCII，兼容 Windows 控制台编码）"""
    total = max(total, 1)
    pct = 100.0 * done / total
    width = 24
    filled = int(width * done / total)
    bar = "#" * filled + "-" * (width - filled)
    line = f"\r  [{bar}] {pct:5.1f}%  {done}/{total}  {msg[:44]}"
    sys.stdout.write(line.ljust(88))
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")


def setup_logging(log_file: Path) -> None:
    """日志同时输出到控制台和文件（错误日志方便事后排查）"""
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)


def check_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(
                f"错误: 未找到 {tool}。请安装 ffmpeg 并把它加入 PATH。\n"
                "  Windows: winget install Gyan.FFmpeg   或   choco install ffmpeg\n"
                "  macOS:   brew install ffmpeg\n"
                "  Ubuntu:  sudo apt install ffmpeg"
            )


# ----------------------------------------------------------------------
# ffmpeg：取时长 / 提取切片
# ----------------------------------------------------------------------
def media_duration(path: Path) -> float:
    """用 ffprobe 读取媒体文件总时长（秒）"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {out.stderr.strip()[:200]}")
    return float(out.stdout.strip())


def extract_audio(src: Path, dst: Path, start: float, duration: float) -> None:
    """
    用 ffmpeg 把 src 的 [start, start+duration] 片段提取成 mp3（16kHz 单声道）。
    -ss 放在 -i 之前属于快速定位，配合重编码在现代 ffmpeg 上精度足够且速度快。
    """
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", str(src)]
    if duration > 0:
        cmd += ["-t", f"{duration:.2f}"]
    cmd += [
        "-vn",              # 丢弃视频轨
        "-ac", "1",         # 单声道
        "-ar", "16000",     # 16 kHz 采样率（语音识别常用配置）
        "-b:a", CHUNK_BITRATE,
        "-f", "mp3",
        str(dst),
    ]
    out = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {out.stderr.strip()[:200]}")


# ----------------------------------------------------------------------
# MiMo ASR 接口调用
# ----------------------------------------------------------------------
def build_payload(data_url: str, language: str) -> dict:
    """
    构造请求体：
      - 音频放在 message content 的 input_audio 对象里
      - input_audio.data 是完整的 data URL 字符串（data:audio/mpeg;base64,xxx）
      - input_audio.format 写 "mp3" 作为双重保险
      - asr_options 里传 {"language": "zh"}，中文场景手动指定
    """
    return {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": data_url,     # 完整 data URL 字符串
                            "format": "mp3",      # "mp3" / "wav"，双重保险
                        },
                    }
                ],
            }
        ],
        "asr_options": {"language": language},
    }


def extract_text(resp_json: dict) -> str:
    """从 chat/completions 响应里取出识别文本"""
    try:
        content = resp_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"响应格式异常: {json.dumps(resp_json, ensure_ascii=False)[:500]}")
    if isinstance(content, list):          # 兼容分段返回的结构
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return (content or "").strip()


def call_asr(session: "requests.Session", data_url: str, api_key: str, language: str) -> str:
    """
    调用一次 ASR 接口。429 / 5xx / 网络错误自动重试（指数退避）。
    认证头用 api-key，不是 Authorization: Bearer。
    """
    headers = {
        "api-key": api_key,                 # ★ 注意：不是 Authorization: Bearer
        "Content-Type": "application/json",
    }
    payload = build_payload(data_url, language)
    last_err = ""

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.post(API_URL, headers=headers, json=payload, timeout=(15, 300))
        except requests.RequestException as exc:      # 网络抖动，也重试
            last_err = f"网络错误: {exc}"
            wait = min(2 ** attempt, 60)
            log.warning("  第 %d 次尝试 %s，%d 秒后重试...", attempt + 1, last_err, wait)
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            return extract_text(resp.json())

        if resp.status_code == 429 or resp.status_code >= 500:
            last_err = f"HTTP {resp.status_code}: {resp.text.strip()[:200]}"
            # 优先遵守服务端返回的 Retry-After，否则指数退避 1,2,4,8,16,32...
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else min(2 ** attempt, 60)
            except ValueError:
                wait = min(2 ** attempt, 60)
            log.warning("  第 %d 次尝试 %s，%.1f 秒后重试...", attempt + 1, last_err, wait)
            time.sleep(wait)
            continue

        # 其他 4xx 属于请求参数问题，重试无意义，直接报错
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text.strip()[:500]}")

    raise RuntimeError(f"重试 {MAX_RETRIES} 次仍失败: {last_err}")


def transcribe_range(
    src: Path,
    start: float,
    duration: float,
    tmpdir: Path,
    session: "requests.Session",
    api_key: str,
    language: str,
    tag: str,
) -> str:
    """
    转写媒体文件的一个时间段：
    提取音频 → 检查体积 → Base64 → data URL → 调用 API。
    若切片 Base64 后仍超过 10 MB，则对半拆分继续切（安全兜底）。
    """
    raw = tmpdir / f"{tag}.mp3"
    extract_audio(src, raw, start, duration)

    # 估算 Base64 后体积：base64 膨胀约 4/3
    raw_size = raw.stat().st_size
    approx_b64 = int(raw_size * 4 / 3) + 8
    if approx_b64 > MAX_B64_BYTES:
        raw.unlink(missing_ok=True)
        if duration <= 30:
            raise RuntimeError(f"切片过大且无法再分: {duration:.0f}s")
        half = duration / 2.0
        log.info("  切片 Base64 后约 %.1f MB 超限，对半拆分重试", approx_b64 / 1048576)
        t1 = transcribe_range(src, start, half, tmpdir, session, api_key, language, tag + "a")
        t2 = transcribe_range(src, start + half, duration - half, tmpdir, session, api_key, language, tag + "b")
        return (t1 + "\n" + t2).strip()

    data = raw.read_bytes()
    raw.unlink(missing_ok=True)

    b64 = base64.b64encode(data).decode("ascii")
    data_url = f"data:audio/mpeg;base64,{b64}"   # ★ data 字段必须是完整 data URL

    text = call_asr(session, data_url, api_key, language)

    # ★ 每次 API 调用之间的固定间隔，避免触发 RPM=100 限流
    time.sleep(DEFAULT_INTERVAL)
    return text


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main() -> None:
    global MODEL, DEFAULT_INTERVAL   # 允许命令行覆盖模型名 / 调用间隔

    parser = argparse.ArgumentParser(description="用小米 MiMo ASR 批量转写网课音频/视频")
    parser.add_argument("folder", help="存放网课音频/视频的文件夹")
    parser.add_argument("-o", "--output", help="输出 txt 路径（默认: <文件夹>/转写结果.txt）")
    parser.add_argument("--api-key", help="API Key（优先级高于脚本内和环境变量）")
    parser.add_argument("--model", default=MODEL, help=f"模型名（默认 {MODEL}）")
    parser.add_argument("--language", default="zh", help="识别语言（默认 zh）")
    parser.add_argument("--slice-minutes", type=float, default=DEFAULT_SLICE_MINUTES,
                        help=f"切片长度（分钟），超过此长度就切片（默认 {DEFAULT_SLICE_MINUTES}）")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help=f"两次调用之间的间隔秒数（默认 {DEFAULT_INTERVAL}）")
    parser.add_argument("--fresh", action="store_true", help="忽略缓存断点，全部重新转写")
    args = parser.parse_args()

    MODEL = args.model
    DEFAULT_INTERVAL = args.interval

    # ---- API Key：命令行 > 脚本内填的 > 环境变量 ----
    api_key = (args.api_key or API_KEY or os.environ.get("MIMO_API_KEY", "")).strip()
    if not api_key:
        sys.exit(
            "错误: 未提供 API Key。\n"
            "  请打开本脚本，搜索【在这里填入你的 API Key】填入密钥；\n"
            "  或设置环境变量 MIMO_API_KEY；或用 --api-key 传入。"
        )

    # ---- 输入文件夹 ----
    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"错误: 文件夹不存在: {folder}")

    # media_files = sorted(
    #     p for p in folder.iterdir()
    #     if p.is_file() and p.suffix.lower() in MEDIA_EXTS
    # )
    
    # 使用 rglob('*') 递归搜索所有子文件夹
    media_files = sorted(
        p for p in folder.rglob('*')
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS
    )
    if not media_files:
        sys.exit(f"错误: 在 {folder} 里没有找到音频/视频文件（支持: {', '.join(sorted(MEDIA_EXTS))}）")

    output_txt = Path(args.output).expanduser().resolve() if args.output else folder / "转写结果.txt"
    output_txt.parent.mkdir(parents=True, exist_ok=True)
    log_file = output_txt.with_suffix(".errors.log")
    ckpt_file = output_txt.with_suffix(".checkpoint.json")

    setup_logging(log_file)
    check_ffmpeg()

    # ---- 断点缓存：中途失败重跑时跳过已完成的切片 ----
    checkpoint: dict[str, str] = {}
    if ckpt_file.exists() and not args.fresh:
        try:
            checkpoint = json.loads(ckpt_file.read_text(encoding="utf-8"))
            log.info("加载断点缓存 %d 条，未完成部分继续跑", len(checkpoint))
        except (json.JSONDecodeError, OSError):
            checkpoint = {}

    def save_checkpoint() -> None:
        ckpt_file.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    slice_seconds = max(60.0, args.slice_minutes * 60.0)

    # ---- 统计总切片数，用于进度显示 ----
    plan: list[tuple[Path, float, int]] = []   # (文件, 总时长, 切片数)
    for f in media_files:
        try:
            dur = media_duration(f)
            n = max(1, math.ceil(dur / slice_seconds))
            plan.append((f, dur, n))
        except Exception as exc:
            log.error("读取时长失败，跳过 %s: %s", f.name, exc)

    total_slices = sum(n for _, _, n in plan)
    log.info("共发现 %d 个文件，合计约 %d 段待转写，输出: %s", len(plan), total_slices, output_txt)

    session = requests.Session()
    all_blocks: list[str] = []
    done_slices = 0
    failed_slices: list[str] = []
    t0 = time.time()

    tmpdir = Path(tempfile.mkdtemp(prefix="mimo_asr_"))
    try:
        for fi, (src, dur, n_slices) in enumerate(plan, 1):
            log.info("▶ [%d/%d] %s（时长 %s，切 %d 段）", fi, len(plan), src.name, hms(dur), n_slices)
            parts: list[str] = []

            for si in range(n_slices):
                start = si * slice_seconds
                seg_dur = min(slice_seconds, dur - start)
                key = f"{src.name}@{start:.0f}+{seg_dur:.0f}"
                label = f"{src.name} 段{si + 1}/{n_slices}"

                if key in checkpoint:                     # 断点续跑
                    text = checkpoint[key]
                    log.debug("缓存命中: %s", key)
                else:
                    try:
                        text = transcribe_range(
                            src, start, seg_dur, tmpdir, session,
                            api_key, args.language,
                            tag=f"f{fi}s{si}",
                        )
                        checkpoint[key] = text
                        save_checkpoint()
                    except Exception as exc:
                        msg = f"{label}: {exc}"
                        log.error("转写失败 %s", msg)
                        failed_slices.append(msg)
                        text = f"【本段转写失败: {exc}】"

                parts.append(f"（段 {si + 1}/{n_slices}  {hms(start)} - {hms(start + seg_dur)}）\n{text}\n")
                done_slices += 1
                progress(done_slices, total_slices, label)

            header = (
                f"\n{'=' * 64}\n"
                f"【{src.name}】  时长 {hms(dur)}  共 {n_slices} 段\n"
                f"{'=' * 64}\n"
            )
            all_blocks.append(header + "\n".join(parts))

            # 每处理完一个文件就落盘一次，长时间任务不怕中断
            output_txt.write_text("\n".join(all_blocks), encoding="utf-8")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 收尾 ----
    elapsed = time.time() - t0
    log.info("完成！用时 %s，结果已写入: %s", hms(elapsed), output_txt)
    if failed_slices:
        log.warning("共有 %d 段转写失败，详见日志: %s", len(failed_slices), log_file)
        for m in failed_slices:
            log.warning("  - %s", m)
    if not failed_slices and ckpt_file.exists():
        ckpt_file.unlink(missing_ok=True)   # 全部成功后清理断点缓存


if __name__ == "__main__":
    force_utf8_stdio()
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。重新运行同一命令可从断点继续。", file=sys.stderr)
        sys.exit(130)
