#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bailian_asr_batch.py —— 用阿里云百炼 Qwen3-ASR-Flash-Filetrans 批量转写网课视频/音频

相比旧版 mimo_asr_batch.py 的区别：
  * 默认每 1 分钟一段（--part-minutes 可调），每段一个异步任务
  * 接口本身返回「句级时间戳」，切片只是为了让输出按分钟分段，不靠偏移猜
  * 输出按 1 分钟一段分组（Markdown 小标题 / txt 段头），同时保留句级时间戳
  * 音频走百炼「临时上传凭证」传到临时 OSS（48 小时有效），不需要自己开 OSS Bucket
  * 支持多任务并发、断点续跑、失败重试

流程（每个片段）：
  ffmpeg 抽 16kHz 单声道 mp3
    → GET  /api/v1/uploads?action=getPolicy   拿上传凭证
    → POST upload_host                       上传文件，得到 oss:// 临时地址
    → POST /api/v1/services/audio/asr/transcription   提交异步任务
    → GET  /api/v1/tasks/{task_id}           轮询
    → 下载 transcription_url 的 JSON（24 小时内有效）

依赖：
  pip install requests
  ffmpeg / ffprobe 在 PATH 中

用法：
  # 先干跑，看看文件数、总时长、预计费用
  python bailian_asr_batch.py "F:\\408视频" --dry-run

  # 正式转写，结果输出到 F:\\408视频\\转写结果
  python bailian_asr_batch.py "F:\\408视频"

  # 指定输出目录、并发 4、开 ITN（数字转阿拉伯数字）
  python bailian_asr_batch.py "F:\\408视频" -o "D:\\转写" --workers 4 --itn

  # 强制重跑（忽略断点）
  python bailian_asr_batch.py "F:\\408视频" --fresh

API Key 读取顺序：
  --api-key  >  环境变量 DASHSCOPE_API_KEY  >  脚本同目录 .env 里的 DASHSCOPE_API_KEY
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先执行: pip install requests", file=sys.stderr)
    sys.exit(1)


# ======================================================================
# 默认配置
# ======================================================================
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
MODEL = "qwen3-asr-flash-filetrans"

PRICE_PER_SECOND = 0.00022          # 元/秒（以官网为准）
FREE_SECONDS = 36_000               # 新用户免费 10 小时

MEDIA_EXTS = {
    ".mp4", ".mkv", ".mov", ".webm", ".flv", ".avi", ".wmv", ".ts", ".m4a",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus", ".wma",
}

AUDIO_BITRATE = "48k"               # 16kHz 单声道 mp3，2 小时约 43MB
BYTES_PER_SECOND = 6000             # 48kbps ≈ 6000 B/s，用于估算尺寸
MAX_RETRIES = 6

log = logging.getLogger("bailian_asr")


# ======================================================================
# 基础工具
# ======================================================================
def force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def hms(seconds: float, with_ms: bool = False) -> str:
    s = max(0.0, float(seconds))
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    if with_ms:
        ms = int(round((s - int(s)) * 1000))
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"
    return f"{h:02d}:{m:02d}:{sec:02d}"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def output_stem(src: Path, root: Path) -> str:
    """用相对路径拼输出文件名，避免不同子目录里同名视频互相覆盖。"""
    try:
        rel = src.relative_to(root).with_suffix("")
        parts = [p for p in rel.parts if p]
    except ValueError:
        parts = [src.stem]
    name = "__".join(parts) if parts else src.stem
    return re.sub(r'[<>:"/\\|?*]+', "_", name)


def check_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(
                f"错误: 未找到 {tool}。请安装 ffmpeg 并加入 PATH。\n"
                "  Windows: winget install Gyan.FFmpeg"
            )


def setup_logging(log_file: Path) -> None:
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


def load_api_key(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    env = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if env:
        return env
    # 兼容脚本同目录的 .env
    dotenv = Path(__file__).with_name(".env")
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "DASHSCOPE_API_KEY":
                return v.strip().strip('"').strip("'")
    return ""


# ======================================================================
# ffmpeg
# ======================================================================
def media_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {out.stderr.strip()[:200]}")
    return float(out.stdout.strip())


def extract_audio(src: Path, dst: Path, start: float, duration: float) -> None:
    """把 src 的 [start, start+duration] 抽成 16kHz 单声道 mp3。"""
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-vn", "-ac", "1", "-ar", "16000",
        "-b:a", AUDIO_BITRATE,
        "-f", "mp3", str(dst),
    ]
    out = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {out.stderr.strip()[:300]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError("ffmpeg 输出为空")


# ======================================================================
# 百炼客户端
# ======================================================================
class BailianError(RuntimeError):
    pass


class BailianClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = MODEL,
        language: str | None = "zh",
        enable_itn: bool = False,
        enable_words: bool = False,
        diarize: bool = False,
        vocabulary: dict[str, int] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        self.enable_itn = enable_itn
        self.enable_words = enable_words
        self.diarize = diarize
        self.vocabulary = vocabulary
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})
        # OSS 的预签名上传/下载地址不能带 Authorization 头，否则会签名冲突
        self.plain = requests.Session()

    # ---------- 底层请求 ----------
    def _request(self, method: str, url: str, auth: bool = True, **kwargs) -> requests.Response:
        session = self.session if auth else self.plain
        last_err = ""
        for attempt in range(MAX_RETRIES):
            try:
                resp = session.request(method, url, timeout=(20, 300), **kwargs)
            except requests.RequestException as exc:
                last_err = f"网络错误: {exc}"
                wait = min(2 ** attempt, 60)
                log.warning("  %s，%d 秒后重试...", last_err, wait)
                time.sleep(wait)
                continue

            if resp.status_code < 400:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}: {resp.text.strip()[:300]}"
                wait = min(2 ** attempt, 60)
                log.warning("  %s，%d 秒后重试...", last_err, wait)
                time.sleep(wait)
                continue
            raise BailianError(f"HTTP {resp.status_code}: {resp.text.strip()[:800]}")
        raise BailianError(f"重试 {MAX_RETRIES} 次仍失败: {last_err}")

    # ---------- 临时上传 ----------
    def get_upload_policy(self) -> dict[str, Any]:
        url = f"{self.base_url}/uploads"
        last: Exception | None = None
        # 正常用当前模型申请；个别账号/模型不支持时退回通用 ASR 模型名
        for model in (self.model, "qwen3-asr-flash"):
            try:
                resp = self._request(
                    "GET", url,
                    params={"action": "getPolicy", "model": model},
                )
                data = resp.json().get("data")
                if data:
                    return data
            except BailianError as exc:
                last = exc
        raise BailianError(f"获取上传凭证失败: {last}")

    def upload_audio(self, path: Path) -> str:
        """上传到百炼临时 OSS，返回 oss://... 地址（48 小时有效）。"""
        last_err = ""
        for attempt in range(MAX_RETRIES):
            policy = self.get_upload_policy()
            key = f"{policy['upload_dir']}/{uuid.uuid4().hex}_{path.name}"
            try:
                with path.open("rb") as fh:
                    files = {
                        "OSSAccessKeyId": (None, policy["oss_access_key_id"]),
                        "Signature": (None, policy["signature"]),
                        "policy": (None, policy["policy"]),
                        "x-oss-object-acl": (None, policy["x_oss_object_acl"]),
                        "x-oss-forbid-overwrite": (None, policy["x_oss_forbid_overwrite"]),
                        "key": (None, key),
                        "success_action_status": (None, "200"),
                        "file": (path.name, fh, "audio/mpeg"),
                    }
                    # 每次都重新打开文件，避免重试时句柄已读到末尾
                    resp = self.plain.post(
                        policy["upload_host"], files=files, timeout=(20, 900)
                    )
            except requests.RequestException as exc:
                last_err = f"上传网络错误: {exc}"
                wait = min(2 ** attempt, 60)
                log.warning("  %s，%d 秒后重试...", last_err, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 200:
                return f"oss://{key}"
            last_err = f"上传失败 HTTP {resp.status_code}: {resp.text[:300]}"
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = min(2 ** attempt, 60)
                log.warning("  %s，%d 秒后重试...", last_err, wait)
                time.sleep(wait)
                continue
            raise BailianError(last_err)
        raise BailianError(f"上传重试 {MAX_RETRIES} 次仍失败: {last_err}")

    # ---------- 异步任务 ----------
    @property
    def is_qwen3(self) -> bool:
        return "qwen3-asr-flash-filetrans" in self.model

    def submit(self, oss_url: str) -> str:
        if self.is_qwen3:
            # qwen3-asr-flash-filetrans：单文件、file_url（字符串）
            input_obj: dict[str, Any] = {"file_url": oss_url}
            parameters: dict[str, Any] = {
                "channel_id": [0],
                "enable_itn": self.enable_itn,
                "enable_words": self.enable_words,
            }
            if self.language and self.language != "auto":
                parameters["language"] = self.language
        else:
            # qwen-audio-3.x / paraformer：file_urls（数组）
            input_obj = {"file_urls": [oss_url]}
            parameters = {"channel_id": [0]}
            if self.language and self.language != "auto":
                parameters["language_hints"] = [self.language, "en"]
            if self.diarize:
                parameters["diarization_enabled"] = True
            if self.vocabulary:
                parameters["vocabulary"] = self.vocabulary

        body = {"model": self.model, "input": input_obj, "parameters": parameters}
        headers = {
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
            # 让服务端解析 oss:// 临时地址
            "X-DashScope-OssResourceResolve": "enable",
        }
        resp = self._request(
            "POST",
            f"{self.base_url}/services/audio/asr/transcription",
            headers=headers,
            json=body,
        )
        task_id = (resp.json().get("output") or {}).get("task_id")
        if not task_id:
            raise BailianError(f"提交任务失败: {resp.text[:500]}")
        return task_id

    def poll(self, task_id: str, timeout: float = 7200.0) -> dict[str, Any]:
        """轮询任务直到 SUCCEEDED / FAILED，返回 {results, usage_seconds}。"""
        url = f"{self.base_url}/tasks/{task_id}"
        deadline = time.time() + timeout
        delay = 5.0
        while time.time() < deadline:
            resp = self._request("GET", url)
            body = resp.json()
            output = body.get("output") or {}
            status = output.get("task_status")

            if status == "SUCCEEDED":
                results = self._normalize_results(output)
                failed = [r for r in results if r.get("subtask_status") != "SUCCEEDED"]
                if not results or (failed and len(failed) == len(results)):
                    r = failed[0] if failed else {}
                    raise BailianError(
                        f"子任务失败: {r.get('code') or output.get('code')} "
                        f"{r.get('message') or output.get('message')}"
                    )
                usage = body.get("usage") or output.get("usage") or {}
                return {
                    "results": results,
                    "usage_seconds": usage.get("seconds") or usage.get("duration"),
                }
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise BailianError(
                    f"任务状态 {status}: {output.get('code')} {output.get('message')}"
                )

            time.sleep(delay)
            delay = min(delay * 1.5, 20.0)

        raise BailianError(f"轮询超时（task_id={task_id} 仍可稍后重跑续跑）")

    @staticmethod
    def _normalize_results(output: dict[str, Any]) -> list[dict[str, Any]]:
        """兼容两种返回结构：
        qwen3:   output.result.transcription_url
        3.x:     output.results[].transcription_url
        """
        results = output.get("results")
        if results:
            return results
        one = (output.get("result") or {}).get("transcription_url")
        if one:
            return [{"subtask_status": "SUCCEEDED", "transcription_url": one}]
        return []

    def download_result(self, url: str) -> dict[str, Any]:
        # transcription_url 是预签名 OSS 地址，不能带 Authorization 头
        resp = self._request("GET", url, auth=False)
        return resp.json()


# ======================================================================
# 结果格式化
# ======================================================================
def iter_sentences(result: dict[str, Any], offset_ms: int = 0):
    for transcript in result.get("transcripts") or []:
        for sent in transcript.get("sentences") or []:
            text = (sent.get("text") or "").strip()
            if not text:
                continue
            yield {
                "begin": int(sent.get("begin_time", 0)) + offset_ms,
                "end": int(sent.get("end_time", 0)) + offset_ms,
                "text": text,
                "speaker": sent.get("speaker_id"),
            }


def build_srt(sentences: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, s in enumerate(sentences, 1):
        lines.append(str(i))
        lines.append(f"{hms(s['begin'] / 1000, True)} --> {hms(s['end'] / 1000, True)}")
        prefix = f"[说话人{s['speaker']}] " if s.get("speaker") is not None else ""
        lines.append(prefix + s["text"])
        lines.append("")
    return "\n".join(lines)


def build_txt(sentences: list[dict[str, Any]], gap_seconds: float = 1.2) -> str:
    """按停顿断段，中文句子本身已带标点，直接拼接。"""
    paragraphs: list[str] = []
    buf: list[str] = []
    prev_end = None
    for s in sentences:
        if prev_end is not None and (s["begin"] - prev_end) / 1000.0 >= gap_seconds:
            if buf:
                paragraphs.append("".join(buf))
                buf = []
        buf.append(s["text"])
        prev_end = s["end"]
    if buf:
        paragraphs.append("".join(buf))
    return "\n\n".join(paragraphs) + "\n"


def group_segments(
    sentences: list[dict[str, Any]],
    segment_seconds: float,
    duration: float | None = None,
) -> list[tuple[int, float, float, list[dict[str, Any]]]]:
    """把句子按固定时长分桶（默认 1 分钟），与 API 切片长度无关。"""
    buckets: dict[int, list[dict[str, Any]]] = {}
    for s in sorted(sentences, key=lambda x: x["begin"]):
        idx = int(s["begin"] / 1000.0 // segment_seconds)
        buckets.setdefault(idx, []).append(s)
    result = []
    for idx, sents in sorted(buckets.items()):
        start = idx * segment_seconds
        end = (idx + 1) * segment_seconds
        if duration:
            end = min(end, duration)
        result.append((idx, start, end, sents))
    return result


def build_txt_segments(segments: list[tuple[int, float, float, list[dict[str, Any]]]]) -> str:
    """按分桶分组的纯文本，段头格式与旧版脚本一致。"""
    total = max((s[0] for s in segments), default=-1) + 1
    lines: list[str] = []
    for idx, start, end, sents in segments:
        lines.append(f"（段 {idx + 1}/{total}  {hms(start)} - {hms(end)}）")
        lines.append(build_txt(sents).strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_markdown(
    title: str,
    segments: list[tuple[int, float, float, list[dict[str, Any]]]],
    duration: float,
    model: str,
) -> str:
    lines = [
        f"# {title}",
        "",
        f"> 时长 {hms(duration)} · 模型 `{model}` · 转写于 {time.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    total = max((s[0] for s in segments), default=-1) + 1
    for idx, start, end, sents in segments:
        lines.append(f"### 段 {idx + 1}/{total}　{hms(start)} - {hms(end)}")
        lines.append("")
        for s in sents:
            prefix = f"[说话人{s['speaker']}] " if s.get("speaker") is not None else ""
            lines.append(f"- `[{hms(s['begin'] / 1000)}]` {prefix}{s['text']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ======================================================================
# 状态文件
# ======================================================================
class State:
    """断点状态；所有写操作串行化，工作线程只提交副本，避免序列化时被并发修改。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {"version": 1, "files": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {"version": 1, "files": {}}

    def get(self, key: str) -> dict[str, Any] | None:
        return self.data["files"].get(key)

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self.lock:
            self.data["files"][key] = value
            self.save()

    def update_part(self, key: str, index: int, part: dict[str, Any]) -> None:
        with self.lock:
            record = self.data["files"].setdefault(key, {"parts": []})
            parts = record.setdefault("parts", [])
            while len(parts) <= index:
                parts.append({"index": len(parts)})
            parts[index] = dict(part)      # 存副本，后续工作线程再改也不影响
            self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


# ======================================================================
# 单片段 / 单文件
# ======================================================================
def plan_parts(duration: float, part_seconds: float) -> list[tuple[int, float, float]]:
    parts = []
    start = 0.0
    idx = 0
    while start < duration - 0.5:
        dur = min(part_seconds, duration - start)
        parts.append((idx, start, dur))
        start += dur
        idx += 1
    return parts


def run_part(
    client: BailianClient,
    src: Path,
    part: dict[str, Any],
    tmpdir: Path,
    raw_dir: Path,
    keep_audio: bool,
    poll_timeout: float,
    state: State,
    key: str,
) -> dict[str, Any]:
    """执行一个片段：上传 → 提交 → 轮询 → 下载 JSON。返回更新后的 part。"""
    index = part["index"]
    start = part["start"]
    dur = part["duration"]

    # 断点续跑：已有 task_id 就直接复用，避免重复计费
    result: dict[str, Any] | None = None
    if part.get("task_id"):
        log.info("  段%d：复用已有任务 %s", index + 1, part["task_id"])
        try:
            output = client.poll(part["task_id"], timeout=poll_timeout)
            result = download_first(client, output["results"])
            part["usage"] = output.get("usage_seconds")
        except BailianError as exc:
            log.warning("  段%d：旧任务不可用（%s），重新提交", index + 1, exc)
            part.pop("task_id", None)

    if result is None:
        audio_path = tmpdir / f"part_{index:03d}.mp3"
        log.info("  段%d：抽音频 %s - %s", index + 1, hms(start), hms(start + dur))
        extract_audio(src, audio_path, start, dur)

        log.info("  段%d：上传 %s", index + 1, human_size(audio_path.stat().st_size))
        oss_url = client.upload_audio(audio_path)
        part["oss_url"] = oss_url
        part["uploaded_at"] = int(time.time())
        if not keep_audio:
            audio_path.unlink(missing_ok=True)

        log.info("  段%d：提交任务", index + 1)
        task_id = client.submit(oss_url)
        part["task_id"] = task_id
        state.update_part(key, index, part)   # 立刻落盘，崩溃后可复用任务
        log.info("  段%d：task_id=%s，等待转写...", index + 1, task_id)

        output = client.poll(task_id, timeout=poll_timeout)
        result = download_first(client, output["results"])
        part["usage"] = output.get("usage_seconds")

    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"part_{index:03d}.json"
    raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    part["status"] = "done"
    part["json"] = str(raw_path)
    return part


def download_first(client: BailianClient, results: list[dict[str, Any]]) -> dict[str, Any]:
    for r in results:
        if r.get("subtask_status") == "SUCCEEDED" and r.get("transcription_url"):
            return client.download_result(r["transcription_url"])
    raise BailianError("任务成功但没有可下载的 transcription_url")


def prepare_record(
    src: Path,
    rel_key: str,
    state: State,
    part_seconds: float,
    out_stem: str,
    fresh: bool = False,
) -> dict[str, Any]:
    """读取时长、规划片段，并复用上次状态里已完成的片段。"""
    record = state.get(rel_key) or {}
    duration = record.get("duration") or media_duration(src)
    record["duration"] = duration
    record["out_stem"] = out_stem

    old_parts = {
        p["index"]: p for p in record.get("parts", []) if "index" in p
    }
    parts: list[dict[str, Any]] = []
    for idx, start, dur in plan_parts(duration, part_seconds):
        p = dict(old_parts.get(idx) or {})
        p.update({"index": idx, "start": start, "duration": dur})
        if fresh:
            for k in ("status", "json", "task_id", "oss_url", "uploaded_at", "usage", "error"):
                p.pop(k, None)
        else:
            jp = Path(p.get("json") or "")
            if jp.is_file() and jp.stat().st_size > 0:
                p["status"] = "done"   # 上次已下载但没来得及更新状态
            elif p.get("status") == "done":
                p.pop("status", None)  # JSON 丢了，必须重跑
        parts.append(p)

    record["parts"] = parts
    record.pop("done", None)
    state.put(rel_key, record)
    return record


def write_outputs(
    src: Path,
    record: dict[str, Any],
    out_dir: Path,
    model: str,
    formats: set[str],
    segment_seconds: float = 60.0,
) -> bool:
    """合并所有已完成片段，输出 md / srt / txt。"""
    sentences: list[dict[str, Any]] = []
    for p in sorted(record["parts"], key=lambda x: x["index"]):
        json_path = Path(p.get("json") or "")
        if p.get("status") != "done" or not json_path.exists():
            continue
        offset_ms = int(round(p["start"] * 1000))
        data = json.loads(json_path.read_text(encoding="utf-8"))
        sentences.extend(iter_sentences(data, offset_ms))

    if not sentences:
        return False

    segments = group_segments(sentences, segment_seconds, record.get("duration"))

    stem = record.get("out_stem") or src.stem
    duration = record.get("duration") or 0
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    if "md" in formats:
        path = out_dir / f"{stem}.md"
        path.write_text(
            build_markdown(stem, segments, duration, model),
            encoding="utf-8",
        )
        written.append(path.name)
    if "srt" in formats:
        path = out_dir / f"{stem}.srt"
        path.write_text(build_srt(sentences), encoding="utf-8")
        written.append(path.name)
    if "txt" in formats:
        path = out_dir / f"{stem}.txt"
        path.write_text(build_txt_segments(segments), encoding="utf-8")
        written.append(path.name)

    record["done"] = all(p.get("status") == "done" for p in record["parts"])
    record["outputs"] = written
    return True


# ======================================================================
# 主流程
# ======================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="用阿里云百炼 Qwen3-ASR-Flash-Filetrans 批量转写视频/音频"
    )
    parser.add_argument("folder", help="视频/音频所在文件夹（会递归搜索）")
    parser.add_argument("-o", "--output", help="输出目录（默认 <folder>/转写结果）")
    parser.add_argument("--api-key", help="百炼 API Key；默认读 DASHSCOPE_API_KEY")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"API 地址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--model", default=MODEL, help=f"模型（默认 {MODEL}）")
    parser.add_argument("--language", default="zh",
                        help="语言，zh/en/...；auto 表示自动识别（默认 zh）")
    parser.add_argument("--itn", action="store_true",
                        help="开启逆文本标准化（'一百二十三' → '123'）")
    parser.add_argument("--words", action="store_true", help="返回词级时间戳（体积更大）")
    parser.add_argument("--diarize", action="store_true", help="开启说话人分离")
    parser.add_argument("--vocab", help="热词，逗号分隔，如 '页表,中断,进程调度'")
    parser.add_argument("--part-minutes", type=float, default=1.0,
                        help="每个任务的音频长度（分钟，默认 1；调大可显著减少请求数）")
    parser.add_argument("--segment-minutes", type=float, default=1.0,
                        help="输出按多少分钟分段（默认 1，与 API 切片长度无关）")
    parser.add_argument("--workers", type=int, default=3, help="并发任务数（默认 3）")
    parser.add_argument("--poll-timeout", type=float, default=7200.0,
                        help="单个任务最长等待秒数（默认 7200）")
    parser.add_argument("--formats", default="md,srt,txt",
                        help="输出格式，逗号分隔（默认 md,srt,txt）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不调用 API")
    parser.add_argument("--fresh", action="store_true", help="忽略断点，全部重跑")
    parser.add_argument("--keep-audio", action="store_true", help="保留临时抽出的 mp3")
    args = parser.parse_args()
    args.formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    if not args.formats:
        sys.exit("错误: --formats 不能为空")
    if not args.formats <= {"md", "srt", "txt"}:
        sys.exit("错误: --formats 只能包含 md,srt,txt")
    if args.part_minutes <= 0:
        sys.exit("错误: --part-minutes 必须大于 0")
    if args.segment_minutes <= 0:
        sys.exit("错误: --segment-minutes 必须大于 0")
    if args.workers < 1:
        sys.exit("错误: --workers 至少为 1")

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"错误: 文件夹不存在: {folder}")

    out_dir = Path(args.output).expanduser().resolve() if args.output else folder / "转写结果"
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(
        p for p in folder.rglob("*")
        if p.is_file()
        and p.suffix.lower() in MEDIA_EXTS
        and out_dir not in p.parents        # 别把输出目录里的临时音频当成输入
    )
    if not files:
        sys.exit(f"错误: 在 {folder} 里没有找到音视频文件")

    state_file = out_dir / "_asr_state.json"
    log_file = out_dir / "_asr.log"
    setup_logging(log_file)
    check_ffmpeg()

    # 百炼临时上传单文件上限一般是 100MB，按 48kbps 估算把单段控制在 ~90MB 内
    max_part_seconds = 90 * 1024 * 1024 / BYTES_PER_SECOND
    if args.part_minutes * 60.0 > max_part_seconds:
        log.warning(
            "  --part-minutes %.0f 过大，按 90MB 上限压到 %.0f 分钟",
            args.part_minutes, max_part_seconds / 60.0,
        )
        args.part_minutes = max_part_seconds / 60.0

    if args.fresh and state_file.exists():
        state_file.rename(state_file.with_suffix(".bak.json"))
        log.info("--fresh: 旧断点已备份为 _asr_state.bak.json")

    # 统计时长
    plan: list[tuple[Path, float]] = []
    total_duration = 0.0
    for f in files:
        try:
            d = media_duration(f)
        except Exception as exc:
            log.error("读取时长失败，跳过 %s: %s", f.name, exc)
            continue
        plan.append((f, d))
        total_duration += d

    cost = total_duration * PRICE_PER_SECOND
    billable = max(0.0, total_duration - FREE_SECONDS)
    log.info(
        "共 %d 个文件，总时长 %s，预计计费 %s 元（免费额度抵扣 %s 后约 %s 元）",
        len(plan), hms(total_duration), f"{cost:.2f}",
        hms(min(total_duration, FREE_SECONDS)), f"{billable * PRICE_PER_SECOND:.2f}",
    )
    est_parts = sum(
        len(plan_parts(d, args.part_minutes * 60.0)) for _, d in plan
    )
    log.info(
        "按每段 %.0f 分钟切分，共约 %d 个异步任务；输出按 %.0f 分钟分段",
        args.part_minutes, est_parts, args.segment_minutes,
    )

    if args.dry_run:
        log.info("--dry-run 结束，未调用 API")
        return

    api_key = load_api_key(args.api_key)
    if not api_key:
        sys.exit(
            "错误: 未找到百炼 API Key。\n"
            "  设置环境变量 DASHSCOPE_API_KEY，或在脚本同目录 .env 里写入:\n"
            "  DASHSCOPE_API_KEY=sk-xxxx"
        )

    vocab = None
    if args.vocab:
        vocab = {}
        for word in (w.strip() for w in args.vocab.split(",")):
            if word:
                vocab[word] = 5

    client = BailianClient(
        api_key=api_key,
        base_url=args.base_url,
        model=args.model,
        language=args.language,
        enable_itn=args.itn,
        enable_words=args.words,
        diarize=args.diarize,
        vocabulary=vocab,
    )

    if client.is_qwen3 and (args.diarize or vocab):
        log.warning(
            "  %s 不支持说话人分离/热词，已忽略这些参数；"
            "如需该功能请改用 --model qwen-audio-3.1-asr-flash-filetrans",
            args.model,
        )

    state = State(state_file)
    stats = {"seconds": 0.0}
    t0 = time.time()

    # 先把所有文件的长短和片段规划好，再统一调度，做到跨文件并发
    records: dict[str, tuple[Path, dict[str, Any]]] = {}
    for src, _duration in plan:
        rel_key = f"{src.relative_to(folder)}|{src.stat().st_size}"
        try:
            records[rel_key] = (
                src,
                prepare_record(
                    src, rel_key, state,
                    args.part_minutes * 60.0,
                    output_stem(src, folder),
                    args.fresh,
                ),
            )
        except Exception as exc:
            log.error("准备失败，跳过 %s: %s", src.name, exc)

    pending: list[tuple[str, Path, dict[str, Any]]] = []
    for key, (src, record) in records.items():
        for p in record["parts"]:
            if p.get("status") != "done" or not Path(p.get("json") or "").exists():
                pending.append((key, src, p))

    cached = sum(len(r["parts"]) for _, r in records.values()) - len(pending)
    log.info("待转写 %d 段，断点命中 %d 段，并发 %d", len(pending), cached, args.workers)

    tmpdirs: dict[str, Path] = {}
    if pending:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            for key, src, p in pending:
                record = records[key][1]
                out_stem = record.get("out_stem") or src.stem
                tmpdir = out_dir / "_audio" / out_stem
                tmpdir.mkdir(parents=True, exist_ok=True)
                tmpdirs[key] = tmpdir
                work = dict(p)          # 工作线程改副本，状态由 update_part 统一写
                futures[
                    pool.submit(
                        run_part, client, src, work, tmpdir,
                        out_dir / "raw" / out_stem,
                        args.keep_audio, args.poll_timeout, state, key,
                    )
                ] = (key, src, p)

            for fut in as_completed(futures):
                key, src, original = futures[fut]
                record = records[key][1]
                try:
                    updated = fut.result()
                    for i, q in enumerate(record["parts"]):
                        if q["index"] == updated["index"]:
                            record["parts"][i] = updated
                            break
                    state.update_part(key, updated["index"], updated)
                    log.info(
                        "  ✓ %s 段%d/%d 完成",
                        src.name, updated["index"] + 1, len(record["parts"]),
                    )
                except Exception as exc:
                    original["status"] = "failed"
                    original["error"] = str(exc)
                    state.update_part(key, original["index"], original)
                    log.error(
                        "  ✗ %s 段%d/%d 失败: %s",
                        src.name, original["index"] + 1, len(record["parts"]), exc,
                    )

        if not args.keep_audio:
            for d in tmpdirs.values():
                shutil.rmtree(d, ignore_errors=True)

    # 统一合并输出
    for key, (src, record) in records.items():
        try:
            if write_outputs(
                src, record, out_dir, args.model, args.formats,
                args.segment_minutes * 60.0,
            ):
                state.put(key, record)
                stats["seconds"] += record["duration"]
                log.info("  → %s: %s", src.name, ", ".join(record.get("outputs", [])))
            else:
                log.error("  %s 没有可用结果", src.name)
        except Exception as exc:
            log.error("输出失败 %s: %s", src.name, exc)

    elapsed = time.time() - t0
    log.info(
        "全部结束：成功处理 %s，用时 %s，输出目录 %s",
        hms(stats["seconds"]), hms(elapsed), out_dir,
    )
    log.info("按官网单价估算本批费用约 %.2f 元", stats["seconds"] * PRICE_PER_SECOND)


if __name__ == "__main__":
    force_utf8_stdio()
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。重新运行同一命令可从断点继续。", file=sys.stderr)
        sys.exit(130)
