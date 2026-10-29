#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bili_sync.py —— B 站收藏夹 ⇄ 单个 Markdown 文件同步（Single-File Mode）

全仓库只维护根目录的 `收藏夹.md` 一个文件，每个视频仅占一行。

功能总览
--------
1. 拉取 B 站全部分类收藏夹（含分页），得到 bvid / 标题 / 简介 / UP主 / 时长
2. 解析 `收藏夹.md`：分区标题、`- [ ]` 勾选状态、缩进手写笔记、`<!-- bvid:BVxxx | locked:false -->`
3. 三类增量动作：
   - 手动移动：对比 `scripts/.bili_cache.json`，发现某行被剪切到别的分区 → 立刻写 `locked:true`，以后永不覆盖
   - 云端新增：DeepSeek（deepseek-chat）返回 `{"category": ..., "guide": ...}`，单行插入对应分区顶部
   - 云端取消收藏：未勾选且行下无缩进笔记 → 直接删除；已勾选或有笔记 → 行尾追加 `*(B站已取消)*`
4. 首次运行会把旧版 `11.灵感与收藏库/` 迁移进 `收藏夹.md`（含缩进笔记），随后把旧目录移入 `.trash/`

用法
----
    python scripts/bili_sync.py                # 完整同步一次
    python scripts/bili_sync.py --dry-run      # 只打印计划，不改任何文件
    python scripts/bili_sync.py --skip-ai      # 不调用 DeepSeek，新视频先落到 99_电子榨菜与待定
    python scripts/bili_sync.py --serve        # 启动 HTTP 服务（POST /sync），供油猴脚本一键调用
    python scripts/bili_sync.py -v             # 调试日志
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    import requests
except ImportError:  # pragma: no cover
    print("缺少依赖 requests，请先执行: pip install -r scripts/requirements.txt", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:  # pragma: no cover
    print("缺少依赖 pyyaml，请先执行: pip install -r scripts/requirements.txt", file=sys.stderr)
    sys.exit(1)

try:  # 可选依赖：没有也能跑（脚本内置简易 .env 解析）
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


# =====================================================================
# 常量
# =====================================================================

SCRIPTS_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT = SCRIPTS_DIR.parent

FAV_FILE_NAME = "收藏夹.md"
CACHE_FILE_NAME = ".bili_cache.json"
LEGACY_DIR_NAME = "11.灵感与收藏库"
LEGACY_TRASH_DIR = ".trash"

FAV_TITLE = "# ⚡ 灵感与收藏夹"
USAGE_TEMPLATE = (
    "> 最后同步：{now} | 使用说明：直接将某行剪切移动到其他标题下即可手动改分类"
    "（脚本会自动记忆，绝不覆盖）；勾选 `[x]` 或在行下缩进写字后，"
    "即使 B 站取消收藏也会永久保留。"
)

CATEGORIES = [
    "01_考研408与数学",
    "02_AI与Agent工作流",
    "03_学校课业与开发",
    "04_效率与认知成长",
    "99_电子榨菜与待定",
]
DEFAULT_CATEGORY = "99_电子榨菜与待定"

SECTION_EMOJI = {
    "01_考研408与数学": "🎯",
    "02_AI与Agent工作流": "🤖",
    "03_学校课业与开发": "💻",
    "04_效率与认知成长": "🧠",
    "99_电子榨菜与待定": "🍿",
}

DEFAULT_GUIDE = "先收着，有空再看"
PENDING_GUIDE = "AI 分类暂不可用，已先放进待定区，下次同步会自动补上"
REMOVED_MARK = "*(B站已取消)*"

BILI_FAV_FOLDER_API = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
BILI_FAV_LIST_API = "https://api.bilibili.com/x/v3/fav/resource/list"
BILI_VIDEO_URL = "https://www.bilibili.com/video/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ITEM_RE = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX])\]\s+(?P<rest>.*)$")
BVID_RE = re.compile(r"<!--\s*bvid\s*:\s*(?P<bvid>BV[0-9A-Za-z]+)")
LOCKED_RE = re.compile(r"locked\s*:\s*(?:true|false)", re.I)
LOCKED_TRUE_RE = re.compile(r"locked\s*:\s*true", re.I)
COMMENT_RE = re.compile(r"<!--.*?-->")
GUIDE_RE = re.compile(r"💡\s*\*[^*]*\*")
REMOVED_RE = re.compile(r"\s*\*\(B站已取消\)\*")
FRONTMATTER_RE = re.compile(r"^---\r?\n(?P<fm>.*?)\r?\n---(?P<body>.*)$", re.S)

log = logging.getLogger("bili_sync")


# =====================================================================
# 配置
# =====================================================================


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


class BiliError(RuntimeError):
    """B 站接口异常。"""


@dataclass
class Config:
    uid: str
    sessdata: str
    api_key: str
    base_url: str
    model: str
    vault: Path
    concurrency: int = 4
    timeout: int = 20
    cache_path: Path | None = None

    @property
    def fav_file(self) -> Path:
        return self.vault / FAV_FILE_NAME

    @property
    def cache_file(self) -> Path:
        return self.cache_path or (SCRIPTS_DIR / CACHE_FILE_NAME)

    @property
    def legacy_dir(self) -> Path:
        return self.vault / LEGACY_DIR_NAME


def _load_env_file(path: Path) -> None:
    """不依赖 python-dotenv 的极简 .env 解析（不覆盖已有环境变量）。"""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        log.warning("读取 %s 失败：%s", path, exc)
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(args: argparse.Namespace) -> Config:
    env_path = SCRIPTS_DIR / ".env"
    if load_dotenv is not None:
        load_dotenv(env_path, override=False)
    else:
        _load_env_file(env_path)

    uid = (args.uid or os.environ.get("BILI_UID") or "").strip()
    if not uid:
        raise ConfigError(
            "缺少 BILI_UID。请复制 scripts/.env.example 为 scripts/.env，并填入你的 B 站 UID。"
        )
    if not uid.isdigit():
        raise ConfigError(f"BILI_UID 应该是纯数字（当前为 {uid!r}）。")

    vault_raw = (args.vault or os.environ.get("VAULT_DIR") or "").strip()
    vault = Path(vault_raw).expanduser() if vault_raw else DEFAULT_VAULT
    if not vault.is_absolute():
        vault = (SCRIPTS_DIR / vault).resolve()
    if not vault.exists():
        raise ConfigError(f"VAULT_DIR 不存在：{vault}")

    base_url = (os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip().rstrip("/")
    model = (os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat").strip()

    try:
        concurrency = int(os.environ.get("AI_CONCURRENCY", "4"))
    except ValueError:
        concurrency = 4
    concurrency = max(1, min(8, concurrency))

    return Config(
        uid=uid,
        sessdata=(os.environ.get("BILI_SESSDATA") or "").strip(),
        api_key=(os.environ.get("DEEPSEEK_API_KEY") or "").strip(),
        base_url=base_url,
        model=model,
        vault=vault,
        concurrency=concurrency,
    )


def setup_logging(verbose: bool = False) -> None:
    # 保留控制台原有编码（中文 Windows 上 cp936 能正常显示中文），
    # 只把错误处理改成 replace，避免 emoji 触发 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:  # pragma: no cover - 某些终端不支持
                pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


# =====================================================================
# 工具函数
# =====================================================================


def duration_text(seconds: int) -> str:
    minutes = (seconds or 0) / 60
    if minutes >= 10:
        return f"{minutes:.0f}"
    text = f"{minutes:.1f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def escape_link_text(title: str) -> str:
    return str(title).replace("[", "\\[").replace("]", "\\]")


def _clean_inline(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def category_from_heading(heading: str) -> str:
    text = heading.lstrip("#").strip()
    for category in CATEGORIES:
        if category in text:
            return category
    text = re.sub(r"^[^\w\u4e00-\u9fff]+", "", text).strip()
    return text or "未分区"


# =====================================================================
# B 站接口
# =====================================================================


@dataclass
class BiliItem:
    bvid: str
    title: str
    intro: str
    upper: str
    duration: int
    folders: list[str] = field(default_factory=list)


class BiliClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        if cfg.sessdata:
            self.session.cookies.set("SESSDATA", cfg.sessdata, domain=".bilibili.com")
        else:
            # 匿名访问时带一个随机 buvid3，能显著降低 -352 风控概率
            self.session.cookies.set("buvid3", f"{uuid4().hex}infoc", domain=".bilibili.com")
            log.info("未配置 BILI_SESSDATA，只能读取公开收藏夹（私密收藏夹会被跳过）。")

    # ------------------------------------------------------------------
    def _get(self, url: str, params: dict[str, Any], retries: int = 3) -> dict[str, Any]:
        last_error = ""
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.cfg.timeout)
                resp.raise_for_status()
                payload = resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = str(exc)
                wait = min(8, 2**attempt)
                log.warning("请求失败（第 %d 次）：%s，%.1fs 后重试", attempt, exc, wait)
                time.sleep(wait)
                continue

            code = payload.get("code")
            if code == 0:
                return payload.get("data") or {}

            message = payload.get("message") or "未知错误"
            if code in (-352, -412, -509, -799):
                raise BiliError(
                    f"B 站风控/限流（code={code}，{message}）。"
                    "请降低频率，或在 .env 里配置 BILI_SESSDATA 后重试。"
                )
            if code == -101:
                raise BiliError("B 站要求登录（code=-101），请检查 BILI_SESSDATA 是否过期。")
            raise BiliError(f"B 站接口返回错误 code={code}：{message}")

        raise BiliError(f"请求 {url} 连续失败：{last_error}")

    # ------------------------------------------------------------------
    def list_folders(self) -> list[dict[str, Any]]:
        data = self._get(BILI_FAV_FOLDER_API, {"up_mid": self.cfg.uid, "web_location": "333.1387"})
        folders = data.get("list") or []
        if not folders:
            raise BiliError(
                "没有读到任何收藏夹。请确认 BILI_UID 正确；"
                "若收藏夹是私密的，还需要配置 BILI_SESSDATA。"
            )
        return folders

    # ------------------------------------------------------------------
    def list_items(self, media_id: int | str, folder_title: str) -> list[BiliItem]:
        items: list[BiliItem] = []
        page = 1
        while True:
            data = self._get(
                BILI_FAV_LIST_API,
                {
                    "media_id": media_id,
                    "pn": page,
                    "ps": 20,
                    "keyword": "",
                    "order": "mtime",
                    "type": 0,
                    "tid": 0,
                    "platform": "web",
                },
            )
            medias = data.get("medias") or []
            for media in medias:
                title = (media.get("title") or "").strip()
                bvid = (media.get("bvid") or "").strip()
                # 失效稿件通常标题被替换为「已失效视频」，且没有 bvid
                if not bvid or title == "已失效视频":
                    continue
                title = title or bvid
                upper = ((media.get("upper") or {}).get("name") or "未知UP主").strip()
                items.append(
                    BiliItem(
                        bvid=bvid,
                        title=title,
                        intro=(media.get("intro") or "").strip(),
                        upper=upper,
                        duration=int(media.get("duration") or 0),
                        folders=[folder_title],
                    )
                )

            if data.get("has_more") != 1:
                break
            page += 1
            time.sleep(0.4)
        return items

    # ------------------------------------------------------------------
    def collect_all(self) -> tuple[dict[str, BiliItem], bool, list[str]]:
        """返回 (bvid -> item, 是否完整成功, 错误列表)。"""
        folders = self.list_folders()
        merged: dict[str, BiliItem] = {}
        errors: list[str] = []
        complete = True

        for idx, folder in enumerate(folders, start=1):
            media_id = folder.get("id")
            title = folder.get("title") or f"收藏夹{media_id}"
            count = folder.get("media_count", "?")
            log.info("[%d/%d] 拉取收藏夹《%s》（%s 个）", idx, len(folders), title, count)
            try:
                items = self.list_items(media_id, title)
            except BiliError as exc:
                complete = False
                errors.append(f"收藏夹《{title}》拉取失败：{exc}")
                log.error("收藏夹《%s》拉取失败：%s", title, exc)
                continue

            for item in items:
                existing = merged.get(item.bvid)
                if existing:
                    if title not in existing.folders:
                        existing.folders.append(title)
                else:
                    merged[item.bvid] = item
            time.sleep(0.25)

        log.info("云端共 %d 个有效视频（去重后），涉及 %d 个收藏夹。", len(merged), len(folders))
        return merged, complete, errors


# =====================================================================
# DeepSeek 分类（只返回 category + guide）
# =====================================================================

SYSTEM_PROMPT = """你是一个 B 站收藏视频分类引擎。用户是正在备考计算机考研（数学二 + 408：数据结构、计组、操作系统、计算机网络）的大学生，同时有学校课业（Web 开发、人机交互、软件需求分析）并研究 AI Agent 工作流。
请阅读视频标题、简介、UP主、时长，输出严格的 JSON（不要任何多余文字）：
{
  "category": "只能从 [01_考研408与数学, 02_AI与Agent工作流, 03_学校课业与开发, 04_效率与认知成长, 99_电子榨菜与待定] 中五选一",
  "guide": "25 字以内的极简食用建议：一句话说明核心内容 + 最适合在什么状态下看（如：写计组实验卡壳时看10分钟）"
}"""

_CATEGORY_KEYWORDS = [
    ("01_考研408与数学", ("408", "考研", "数学", "高数", "线代", "数据结构", "计组", "操作系统", "计算机网络", "组成原理", "王道")),
    ("02_AI与Agent工作流", ("ai", "agent", "大模型", "llm", "mcp", "提示词", "prompt", "智能体", "rag", "工作流")),
    ("03_学校课业与开发", ("web", "前端", "后端", "编程", "开发", "python", "java", "课程", "作业", "人机交互", "需求分析", "实验")),
    ("04_效率与认知成长", ("效率", "认知", "时间管理", "笔记", "学习法", "专注", "成长", "思维")),
    ("99_电子榨菜与待定", ("游戏", "搞笑", "娱乐", "音乐", "影视", "vlog", "吃", "鬼畜")),
]


def _guess_category(text: str) -> str:
    lowered = text.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword.lower() in lowered for keyword in keywords):
            return category
    return DEFAULT_CATEGORY


def normalize_ai(raw: dict[str, Any], item: BiliItem) -> dict[str, Any]:
    category = str(raw.get("category") or raw.get("ai_category") or "").strip()
    if category not in CATEGORIES:
        category = _guess_category(f"{item.title} {item.intro}")

    guide = _clean_inline(raw.get("guide") or raw.get("ai_guide") or "")
    guide = guide.replace("*", "＊")
    if len(guide) > 60:
        guide = guide[:60].rstrip("，。,.、 ")

    return {
        "category": category,
        "guide": guide or DEFAULT_GUIDE,
        "pending": False,
    }


def fallback_ai(reason: str = "") -> dict[str, Any]:
    return {
        "category": DEFAULT_CATEGORY,
        "guide": PENDING_GUIDE,
        "pending": True,
        "reason": reason,
    }


def parse_json_loose(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("模型没有返回合法 JSON")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("模型返回的 JSON 不是对象")
    return data


class DeepSeekClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            }
        )

    def classify(self, item: BiliItem, retries: int = 3) -> dict[str, Any]:
        user_payload = {
            "title": item.title,
            "intro": item.intro[:500],
            "up": item.upper,
            "duration_min": duration_text(item.duration),
            "bili_folder": " / ".join(item.folders),
        }
        body = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "temperature": 0.3,
            "max_tokens": 200,
            "response_format": {"type": "json_object"},
        }

        last_error = ""
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.post(f"{self.cfg.base_url}/chat/completions", json=body, timeout=60)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.RequestException(f"HTTP {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return normalize_ai(parse_json_loose(content), item)
            except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
                last_error = str(exc)
                wait = min(10, 2**attempt)
                log.warning("DeepSeek 分类失败（第 %d 次）%s：%s", attempt, item.bvid, exc)
                if attempt < retries:
                    time.sleep(wait)

        return fallback_ai(last_error)


def classify_items(cfg: Config, items: list[BiliItem], skip_ai: bool) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    if skip_ai:
        log.info("--skip-ai 已启用，%d 个新视频放入待定区。", len(items))
        return {item.bvid: fallback_ai("skip-ai") for item in items}

    if not cfg.api_key:
        log.warning("未配置 DEEPSEEK_API_KEY，%d 个新视频先放入待定区。", len(items))
        return {item.bvid: fallback_ai("no-api-key") for item in items}

    client = DeepSeekClient(cfg)
    done = 0
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = {pool.submit(client.classify, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            done += 1
            try:
                results[item.bvid] = future.result()
            except Exception as exc:  # noqa: BLE001 - 单条失败不能拖垮整轮同步
                log.warning("分类异常 %s：%s", item.bvid, exc)
                results[item.bvid] = fallback_ai(str(exc))
            log.info("AI 分类进度 %d/%d", done, len(items))

    for item in items:
        results.setdefault(item.bvid, fallback_ai("unknown"))
    return results


# =====================================================================
# 收藏夹.md 模型
# =====================================================================


@dataclass
class Section:
    heading: str
    category: str
    lines: list[str] = field(default_factory=list)


@dataclass
class VideoLine:
    bvid: str
    text: str
    section: Section
    checked: bool
    locked: bool
    has_notes: bool
    removed: bool


@dataclass
class FavDoc:
    preamble: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    def section(self, category: str) -> Section:
        for section in self.sections:
            if section.category == category:
                return section
        emoji = SECTION_EMOJI.get(category, "📌")
        section = Section(heading=f"## {emoji} {category}", category=category, lines=[])
        self.sections.append(section)
        return section

    def render(self) -> str:
        out = list(self.preamble)
        while out and not out[-1].strip():
            out.pop()
        for section in self.sections:
            body = list(section.lines)
            while body and not body[0].strip():
                body.pop(0)
            while body and not body[-1].strip():
                body.pop()
            out.append("")
            out.append(section.heading)
            out.extend(body)
        return "\n".join(out) + "\n"


def new_doc() -> FavDoc:
    doc = FavDoc(
        preamble=[
            FAV_TITLE,
            "",
            USAGE_TEMPLATE.format(now=datetime.now().strftime("%Y-%m-%d %H:%M")),
        ]
    )
    for category in CATEGORIES:
        doc.sections.append(Section(heading=f"## {SECTION_EMOJI[category]} {category}", category=category, lines=[]))
    return doc


def load_doc(path: Path) -> FavDoc:
    if not path.exists():
        return new_doc()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        log.warning("读取 %s 失败：%s", path, exc)
        return new_doc()

    doc = FavDoc()
    current: Section | None = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if line.startswith("## "):
            current = Section(heading=line, category=category_from_heading(line), lines=[])
            doc.sections.append(current)
            continue
        if current is None:
            doc.preamble.append(line)
        else:
            current.lines.append(line)
    if not doc.sections:
        doc.sections = new_doc().sections
    return doc


def write_doc(path: Path, doc: FavDoc) -> None:
    """先写临时文件再替换，避免中途崩溃把唯一的收藏文件写坏。"""
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(doc.render(), encoding="utf-8")
    tmp.replace(path)


def touch_timestamp(doc: FavDoc) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for idx, line in enumerate(doc.preamble):
        if not line.startswith("> 最后同步"):
            continue
        updated = re.sub(
            r"最后同步：\s*[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}",
            f"最后同步：{now}",
            line,
            count=1,
        )
        if updated == line:
            updated = re.sub(r"最后同步：[^|]*", f"最后同步：{now} ", line, count=1)
        doc.preamble[idx] = updated
        return
    doc.preamble.append(USAGE_TEMPLATE.format(now=now))


def scan_videos(doc: FavDoc) -> list[VideoLine]:
    found: list[VideoLine] = []
    for section in doc.sections:
        lines = section.lines
        i = 0
        while i < len(lines):
            line = lines[i]
            match = ITEM_RE.match(line)
            bvid_match = BVID_RE.search(line) if match else None
            if not (match and bvid_match):
                i += 1
                continue

            j = i + 1
            has_notes = False
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip() or not nxt[:1].isspace() or ITEM_RE.match(nxt):
                    break
                has_notes = True
                j += 1

            found.append(
                VideoLine(
                    bvid=bvid_match.group("bvid"),
                    text=line,
                    section=section,
                    checked=match.group("mark").lower() == "x",
                    locked=bool(LOCKED_TRUE_RE.search(line)),
                    has_notes=has_notes,
                    removed=REMOVED_MARK in line,
                )
            )
            i = j if has_notes else i + 1
    return found


def insert_top(section: Section, block: list[str]) -> None:
    idx = 0
    while idx < len(section.lines) and not section.lines[idx].strip():
        idx += 1
    section.lines[idx:idx] = block


# ---- 单行字段改写 ----------------------------------------------------


def build_line(item: BiliItem, guide: str, locked: bool = False) -> str:
    title = escape_link_text(item.title)
    upper = _clean_inline(item.upper) or "未知UP主"
    upper = upper.replace("`", "'")
    guide = _clean_inline(guide).replace("*", "＊") or DEFAULT_GUIDE
    minutes = duration_text(item.duration)
    return (
        f"- [ ] **[{title}]({BILI_VIDEO_URL}{item.bvid})** `{minutes}min` `@{upper}`"
        f" — 💡 *{guide}* <!-- bvid:{item.bvid} | locked:{'true' if locked else 'false'} -->"
    )


def set_checked(text: str, checked: bool) -> str:
    mark = "x" if checked else " "
    return re.sub(r"^(\s*[-*]\s+)\[[ xX]\]", lambda m: f"{m.group(1)}[{mark}]", text, count=1)


def set_locked(text: str, locked: bool) -> str:
    value = "true" if locked else "false"
    if LOCKED_RE.search(text):
        return LOCKED_RE.sub(f"locked:{value}", text, count=1)
    if "<!--" in text:
        return text.replace("-->", f" | locked:{value} -->", 1)
    return f"{text} <!-- bvid:unknown | locked:{value} -->"


def set_guide(text: str, guide: str) -> str:
    guide = _clean_inline(guide).replace("*", "＊") or DEFAULT_GUIDE
    if GUIDE_RE.search(text):
        return GUIDE_RE.sub(f"💡 *{guide}*", text, count=1)
    match = COMMENT_RE.search(text)
    if match:
        return f"{text[:match.start()].rstrip()} — 💡 *{guide}* {text[match.start():]}"
    return f"{text.rstrip()} — 💡 *{guide}*"


def set_removed(text: str, on: bool = True) -> str:
    if on:
        if REMOVED_MARK in text:
            return text
        match = COMMENT_RE.search(text)
        if match:
            return f"{text[:match.start()].rstrip()} {REMOVED_MARK} {text[match.start():]}"
        return f"{text.rstrip()} {REMOVED_MARK}"
    return REMOVED_RE.sub("", text).rstrip()


# =====================================================================
# 本地缓存（记住每个 bvid 上次所在分区，用于识别手动移动）
# =====================================================================


class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.items: dict[str, dict[str, Any]] = {}

    def load(self) -> "Cache":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("缓存文件损坏，按空缓存处理：%s", exc)
            return self
        items = raw.get("items") if isinstance(raw, dict) else None
        if isinstance(items, dict):
            for bvid, entry in items.items():
                if isinstance(entry, dict):
                    self.items[bvid] = {
                        "category": str(entry.get("category") or ""),
                        "locked": bool(entry.get("locked")),
                        "pending": bool(entry.get("pending")),
                    }
        return self

    def save(self) -> None:
        payload = {
            "version": 2,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "items": self.items,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


# =====================================================================
# 旧版目录迁移（只跑一次）
# =====================================================================


def _read_legacy_note(path: Path) -> tuple[dict[str, Any], str] | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        log.warning("读取旧笔记失败 %s：%s", path, exc)
        return None
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None
    try:
        fm = yaml.safe_load(match.group("fm")) or {}
    except yaml.YAMLError as exc:
        log.warning("YAML 解析失败 %s：%s", path, exc)
        return None
    if not isinstance(fm, dict) or not str(fm.get("bvid") or "").strip():
        return None
    return fm, match.group("body")


def _legacy_guide(body: str) -> str:
    match = re.search(r"\*\*建议场景：\*\*\s*(.+)", body)
    if not match:
        return DEFAULT_GUIDE
    return _clean_inline(match.group(1))[:80] or DEFAULT_GUIDE


def _legacy_notes(body: str) -> list[str]:
    """把旧笔记里用户手写的内容转成缩进子弹行。"""
    heading = "## 💡 我的学习/折腾笔记"
    index = body.find(heading)
    if index == -1:
        return []
    tail = body[index + len(heading):]
    tail = re.sub(r"<!--.*?-->", "", tail, flags=re.S)
    notes = []
    for raw in tail.splitlines():
        line = raw.strip()
        if not line or line in {"-", "*", "---"}:
            continue
        notes.append(f"  - {line.lstrip('-* ').strip()}")
    return notes


def _legacy_item(fm: dict[str, Any]) -> BiliItem | None:
    bvid = str(fm.get("bvid") or "").strip()
    if not bvid:
        return None
    seconds = 0
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(fm.get("duration") or ""))
    if match:
        seconds = int(float(match.group(1)) * 60)
    return BiliItem(
        bvid=bvid,
        title=str(fm.get("title") or bvid).strip(),
        intro="",
        upper=str(fm.get("up") or "未知UP主").strip(),
        duration=seconds,
    )


def migrate_legacy(cfg: Config, cache: Cache, summary: "SyncSummary", dry_run: bool) -> FavDoc | None:
    """把 `11.灵感与收藏库/` 迁移进 `收藏夹.md`，成功后旧目录进 `.trash/`。

    返回迁移后的文档（没有旧目录时返回 None）。
    """
    legacy = cfg.legacy_dir
    if not legacy.exists():
        return None

    paths = [
        path
        for path in sorted(legacy.rglob("*.md"))
        if not path.name.startswith("00_") and "回收站" not in str(path)
    ]
    log.info("检测到旧版收藏库（%d 篇笔记），迁移进 %s ……", len(paths), FAV_FILE_NAME)

    doc = load_doc(cfg.fav_file)
    existing = {video.bvid for video in scan_videos(doc)}
    blocks: dict[str, list[list[str]]] = {}
    migrated: list[str] = []

    for path in paths:
        parsed = _read_legacy_note(path)
        if parsed is None:
            continue
        fm, body = parsed
        item = _legacy_item(fm)
        if item is None or item.bvid in existing:
            continue

        category = str(fm.get("ai_category") or "").strip()
        if category not in CATEGORIES:
            category = _guess_category(f"{item.title} {fm.get('sub_tag') or ''}")
        locked = bool(fm.get("user_locked"))
        guide = _legacy_guide(body)
        line = build_line(item, guide, locked=locked)
        if str(fm.get("status") or "").startswith("已取消收藏"):
            line = set_removed(line)
        if fm.get("ai_pending") is True:
            cache.items[item.bvid] = {"category": category, "locked": locked, "pending": True}
        else:
            cache.items[item.bvid] = {"category": category, "locked": locked, "pending": False}

        blocks.setdefault(category, []).append([line] + _legacy_notes(body))
        existing.add(item.bvid)
        migrated.append(item.bvid)

    for category, category_blocks in blocks.items():
        section = doc.section(category)
        for block in category_blocks:
            insert_top(section, block)

    if migrated:
        summary.migrated += len(migrated)
        if not dry_run:
            touch_timestamp(doc)
            write_doc(cfg.fav_file, doc)
            cache.save()

            rendered = cfg.fav_file.read_text(encoding="utf-8")
            missing = [bvid for bvid in migrated if bvid not in rendered]
            if missing:
                summary.errors.append(f"迁移校验失败，{len(missing)} 个 bvid 未写入，旧目录已保留")
                log.error("迁移校验失败：%s", "、".join(missing[:5]))
                return doc
            # 迁移当轮可能紧接着做取消收藏清理，先留一份完整快照，避免误删无处可查
            snapshot = cfg.vault / LEGACY_TRASH_DIR / "收藏夹_迁移快照.md"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cfg.fav_file, snapshot)
            log.info("已迁移 %d 篇旧笔记（快照：%s）。", len(migrated), snapshot.relative_to(cfg.vault))
    else:
        log.info("旧目录里没有需要迁移的笔记。")

    if dry_run:
        log.info("[dry-run] 将 %d 篇旧笔记迁移进 %s，并把旧目录移入 .trash/", len(migrated), FAV_FILE_NAME)
        return doc

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    target = cfg.vault / LEGACY_TRASH_DIR / f"{LEGACY_DIR_NAME}_{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(legacy), str(target))
        log.info("旧目录已移入回收站：%s", target.relative_to(cfg.vault))
    except OSError as exc:
        summary.errors.append(f"旧目录移动失败：{exc}")
        log.error("旧目录移动失败：%s", exc)
    return doc


# =====================================================================
# 同步主流程
# =====================================================================


@dataclass
class SyncSummary:
    added: int = 0
    removed: int = 0
    kept_removed: int = 0
    locked: int = 0
    reclassified: int = 0
    migrated: int = 0
    ai_failed: int = 0
    total_notes: int = 0
    unwatched: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.errors,
            "added": self.added,
            "removed": self.removed,
            "removed_to_recycle": self.removed,
            "kept_removed": self.kept_removed,
            "locked": self.locked,
            "reclassified": self.reclassified,
            "migrated": self.migrated,
            "ai_failed": self.ai_failed,
            "total_notes": self.total_notes,
            "unwatched": self.unwatched,
            "bloated": [],
            "errors": self.errors,
            "duration_seconds": round(self.duration_seconds, 1),
            "dry_run": self.dry_run,
        }

    def one_line(self) -> str:
        parts = [f"新增 {self.added} 个", f"清理 {self.removed} 个已取消收藏"]
        if self.kept_removed:
            parts.append(f"保留 {self.kept_removed} 个有笔记/已勾选")
        if self.migrated:
            parts.append(f"迁移旧笔记 {self.migrated} 篇")
        return "，".join(parts)


def detect_manual_moves(
    videos: list[VideoLine], cache: Cache, summary: SyncSummary, dry_run: bool
) -> None:
    for video in videos:
        previous = cache.items.get(video.bvid)
        if not previous:
            continue
        previous_category = str(previous.get("category") or "").strip()
        if not previous_category or previous_category == video.section.category:
            continue
        if video.locked:
            continue
        log.info("检测到手动换区：%s（%s → %s），已锁定", video.bvid, previous_category, video.section.category)
        summary.locked += 1
        if dry_run:
            continue
        new_text = set_locked(video.text, True)
        for idx, line in enumerate(video.section.lines):
            if line == video.text:
                video.section.lines[idx] = new_text
                break
        video.text = new_text
        video.locked = True


def delete_videos(doc: FavDoc, bvids: set[str]) -> int:
    removed = 0
    for section in doc.sections:
        kept: list[str] = []
        lines = section.lines
        i = 0
        while i < len(lines):
            line = lines[i]
            match = ITEM_RE.match(line)
            bvid_match = BVID_RE.search(line) if match else None
            if match and bvid_match and bvid_match.group("bvid") in bvids:
                removed += 1
                i += 1
                while i < len(lines):
                    nxt = lines[i]
                    if not nxt.strip() or not nxt[:1].isspace() or ITEM_RE.match(nxt):
                        break
                    i += 1
                continue
            kept.append(line)
            i += 1
        section.lines = kept
    return removed


def dedupe(doc: FavDoc, summary: SyncSummary) -> None:
    seen: set[str] = set()
    for section in doc.sections:
        kept: list[str] = []
        lines = section.lines
        i = 0
        while i < len(lines):
            line = lines[i]
            match = ITEM_RE.match(line)
            bvid_match = BVID_RE.search(line) if match else None
            if match and bvid_match:
                bvid = bvid_match.group("bvid")
                if bvid in seen:
                    log.warning("发现重复条目，已删除后出现的一行：%s", bvid)
                    i += 1
                    while i < len(lines):
                        nxt = lines[i]
                        if not nxt.strip() or not nxt[:1].isspace() or ITEM_RE.match(nxt):
                            break
                        i += 1
                    continue
                seen.add(bvid)
            kept.append(line)
            i += 1
        section.lines = kept
    summary.total_notes = len(seen)


def run_sync(cfg: Config, dry_run: bool = False, skip_ai: bool = False) -> SyncSummary:
    started = time.time()
    summary = SyncSummary(dry_run=dry_run)
    cache = Cache(cfg.cache_file).load()

    # ---------- 0. 旧版目录迁移 ----------
    doc = migrate_legacy(cfg, cache, summary, dry_run)

    # ---------- 1. 解析单文件 ----------
    if doc is None:
        doc = load_doc(cfg.fav_file)
    videos = scan_videos(doc)
    log.info("本地 %s 共有 %d 个视频条目。", FAV_FILE_NAME, len(videos))

    # ---------- 2. 手动移动 → 锁定 ----------
    detect_manual_moves(videos, cache, summary, dry_run)

    # ---------- 3. 拉取云端 ----------
    cloud, cloud_complete, fetch_errors = BiliClient(cfg).collect_all()
    summary.errors.extend(fetch_errors)

    # ---------- 4. 取消收藏清理 ----------
    if not cloud or not cloud_complete:
        reason = "云端一个视频都没读到" if not cloud else "云端数据不完整"
        stale = [video for video in videos if video.bvid not in cloud]
        if stale:
            log.warning("%s，为安全起见跳过删除同步（%d 条待确认）。", reason, len(stale))
        else:
            log.warning("%s，跳过删除同步。", reason)
    else:
        deleted_bvids: set[str] = set()
        marked = 0
        resurrected = 0
        for video in videos:
            if video.bvid in cloud:
                if video.removed and not dry_run:
                    for idx, line in enumerate(video.section.lines):
                        if line == video.text:
                            video.section.lines[idx] = set_removed(line, on=False)
                            break
                    resurrected += 1
                continue
            if video.removed:
                continue
            if video.checked or video.has_notes:
                marked += 1
                summary.kept_removed += 1
                if dry_run:
                    log.info("[dry-run] 保留并标记已取消：%s", video.text)
                    continue
                for idx, line in enumerate(video.section.lines):
                    if line == video.text:
                        video.section.lines[idx] = set_removed(line)
                        break
                video.text = set_removed(video.text)
                video.removed = True
            else:
                deleted_bvids.add(video.bvid)

        if deleted_bvids:
            if dry_run:
                log.info("[dry-run] 将删除 %d 条已取消收藏的行", len(deleted_bvids))
                summary.removed += len(deleted_bvids)
            else:
                summary.removed += delete_videos(doc, deleted_bvids)
                log.info("已删除 %d 条已取消收藏的空行。", summary.removed)
        if marked:
            log.info("保留 %d 条已取消收藏但有勾选/笔记的行。", marked)
        if resurrected:
            log.info("%d 条视频重新出现在 B 站收藏夹，已撤销「已取消」标记。", resurrected)

    # ---------- 5. 待补分类 + 新增 ----------
    videos = scan_videos(doc)
    local_bvids = {video.bvid for video in videos}
    pending = [
        video
        for video in videos
        if cache.items.get(video.bvid, {}).get("pending") and not video.locked and video.bvid in cloud
    ]
    new_items = [item for bvid, item in cloud.items() if bvid not in local_bvids]

    if pending or new_items:
        to_classify = [cloud[video.bvid] for video in pending] + new_items
        log.info("需要 AI 分类：%d 条补分类 + %d 条新增。", len(pending), len(new_items))
        ai_map = classify_items(cfg, to_classify, skip_ai)

        for video in pending:
            ai = ai_map.get(video.bvid) or fallback_ai("unknown")
            if ai.get("pending"):
                if dry_run:
                    log.info("[dry-run] 补分类仍失败：%s", video.bvid)
                continue
            target = doc.section(ai["category"])
            if dry_run:
                log.info("[dry-run] 将补分类：%s → %s", video.bvid, ai["category"])
                summary.reclassified += 1
                continue
            new_text = set_guide(video.text, ai["guide"])
            if target.category != video.section.category and not video.has_notes:
                new_text = set_locked(new_text, False)
                for idx, line in enumerate(video.section.lines):
                    if line == video.text:
                        del video.section.lines[idx]
                        break
                insert_top(target, [new_text])
                final_category = target.category
            else:
                # 已有缩进笔记的条目不动位置，避免笔记与视频行分离
                for idx, line in enumerate(video.section.lines):
                    if line == video.text:
                        video.section.lines[idx] = new_text
                        break
                final_category = video.section.category
            cache.items[video.bvid] = {"category": final_category, "locked": video.locked, "pending": False}
            summary.reclassified += 1
            log.info("补分类完成：%s → %s", video.bvid, target.category)

        for item in reversed(new_items):
            ai = ai_map.get(item.bvid) or fallback_ai("unknown")
            if ai.get("pending") and not skip_ai:
                summary.ai_failed += 1
            if dry_run:
                log.info("[dry-run] 将新增：%s → %s", item.title, ai["category"])
                summary.added += 1
                continue
            section = doc.section(ai["category"])
            insert_top(section, [build_line(item, ai["guide"])])
            cache.items[item.bvid] = {
                "category": section.category,
                "locked": False,
                "pending": bool(ai.get("pending")),
            }
            log.info("已新增：%s → %s", item.title, section.category)
            summary.added += 1
    else:
        log.info("没有新增或待补分类的视频。")

    # ---------- 6. 收尾校验 + 写盘 ----------
    final_videos = scan_videos(doc)
    dedupe(doc, summary)
    final_videos = scan_videos(doc)
    summary.total_notes = len(final_videos)
    summary.unwatched = sum(1 for video in final_videos if not video.checked)

    if not dry_run:
        touch_timestamp(doc)
        write_doc(cfg.fav_file, doc)

        cache.items = {
            video.bvid: {
                "category": video.section.category,
                "locked": video.locked,
                "pending": bool(cache.items.get(video.bvid, {}).get("pending")),
            }
            for video in final_videos
        }
        cache.save()
        log.info("已写入：%s", cfg.fav_file)

    summary.duration_seconds = time.time() - started
    for error in summary.errors:
        log.error(error)
    log.info("同步完成：%s（耗时 %.1fs）", summary.one_line(), summary.duration_seconds)
    return summary


# =====================================================================
# Flask 服务
# =====================================================================

_SYNC_LOCK = threading.Lock()


def create_app(cfg: Config):
    """构建 Flask 应用（单独拆出来，方便本地调试与测试）。"""
    try:
        from flask import Flask, jsonify, request
    except ImportError:
        print("缺少依赖 flask，请执行: pip install -r scripts/requirements.txt", file=sys.stderr)
        sys.exit(1)

    app = Flask(__name__)
    try:
        from flask_cors import CORS

        CORS(app, resources={r"/*": {"origins": "*"}})
    except ImportError:
        log.warning("未安装 flask-cors，将使用手写 CORS 头。")

    @app.after_request
    def add_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        # Chrome 私有网络访问（https 页面 → 127.0.0.1）需要的预检头
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "bili-single-file-sync", "vault": str(cfg.vault)})

    @app.post("/sync")
    def sync_endpoint():
        if not _SYNC_LOCK.acquire(blocking=False):
            return jsonify({"ok": False, "error": "已有同步任务正在进行，请稍后再试。"}), 409
        try:
            payload = request.get_json(silent=True) or {}
            summary = run_sync(
                cfg,
                dry_run=bool(payload.get("dry_run")),
                skip_ai=bool(payload.get("skip_ai")),
            )
            data = summary.to_dict()
            data["message"] = summary.one_line()
            return jsonify(data)
        except Exception as exc:  # noqa: BLE001 - 接口层必须兜底，避免油猴脚本一直转圈
            log.exception("同步失败")
            return jsonify({"ok": False, "error": str(exc)}), 500
        finally:
            _SYNC_LOCK.release()

    return app


def run_server(cfg: Config, port: int) -> None:
    app = create_app(cfg)
    log.info("同步服务已启动：http://127.0.0.1:%d", port)
    log.info("健康检查：http://127.0.0.1:%d/health ｜ 触发同步：POST /sync", port)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)


# =====================================================================
# CLI
# =====================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="B 站收藏夹 ⇄ 单文件（收藏夹.md）同步与 AI 策展",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--serve", action="store_true", help="启动本地 HTTP 服务（默认端口 5055）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("BILI_SYNC_PORT", "5055")), help="服务端口")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要发生的变更，不写文件")
    parser.add_argument("--skip-ai", action="store_true", help="跳过 DeepSeek，新视频直接放入待定区")
    parser.add_argument("--uid", help="临时覆盖 BILI_UID")
    parser.add_argument("--vault", help="临时覆盖 VAULT_DIR")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    try:
        cfg = load_config(args)
    except ConfigError as exc:
        log.error(str(exc))
        return 2

    if args.serve:
        run_server(cfg, args.port)
        return 0

    try:
        summary = run_sync(cfg, dry_run=args.dry_run, skip_ai=args.skip_ai)
    except BiliError as exc:
        log.error("同步中止：%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("用户中断。")
        return 130

    print()
    print("=" * 56)
    print(f" ✅ {summary.one_line()}")
    print(f"    单文件条目 {summary.total_notes} 条 · 未看 {summary.unwatched} 条 · 新锁定 {summary.locked} 条")
    if summary.reclassified:
        print(f"    🤖 {summary.reclassified} 条待定条目已补上 AI 分类")
    if summary.ai_failed:
        print(f"    ⚠️ {summary.ai_failed} 条 AI 分类失败，已放入待定区")
    print(f"    📄 文件：{cfg.fav_file}")
    print("=" * 56)
    return 0 if not summary.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
