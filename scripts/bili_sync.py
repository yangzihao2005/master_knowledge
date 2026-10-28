#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bili_sync.py —— B 站收藏夹 ➔ Obsidian 双向同步与 AI 策展

功能总览
--------
1. 拉取 B 站全部分类收藏夹（含分页），得到 bvid / 标题 / 简介 / UP主 / 时长 / 原收藏夹
2. 扫描本地 `11.灵感与收藏库/` 下的笔记，读取 YAML Frontmatter
3. 双向 Diff：
   - 本地手拖过分类（父文件夹 != ai_category）→ 自动写回 ai_category 并置 user_locked: true
   - 云端新增（本地无对应 bvid）→ 调 DeepSeek 分类，生成笔记
   - 云端已消失（本地有、B站全库都没有）→ 无手写内容移入回收站；有手写内容保留并标记
4. 重新生成 `00_收藏夹全景索引.md`（分类统计 + 臃肿提醒 + 未看清单）
5. 支持 `--serve` 启动本地 Flask 服务（默认 127.0.0.1:5055），供油猴脚本一键调用

用法
----
    python scripts/bili_sync.py                # 完整同步一次
    python scripts/bili_sync.py --dry-run      # 只打印计划，不改任何文件
    python scripts/bili_sync.py --skip-ai      # 不调用 DeepSeek，新视频先落到 99_待定
    python scripts/bili_sync.py --serve        # 启动 HTTP 服务（POST /sync）
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

LIB_DIR_NAME = "11.灵感与收藏库"
RECYCLE_DIR_NAME = "_已取消收藏回收站"
INDEX_FILE_NAME = "00_收藏夹全景索引.md"
NOTES_HEADING = "## 💡 我的学习/折腾笔记"

CATEGORIES = [
    "01_考研408与数学",
    "02_AI与Agent工作流",
    "03_学校课业与开发",
    "04_效率与认知成长",
    "99_电子榨菜与待定",
]
DEFAULT_CATEGORY = "99_电子榨菜与待定"

COGNITIVE_LOADS = ["🔴深度", "🟡平稳", "🟢低耗"]
DEFAULT_LOAD = "🟢低耗"
LOAD_ORDER = {name: i for i, name in enumerate(COGNITIVE_LOADS)}

FAV_FOLDER_API = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
FAV_LIST_API = "https://api.bilibili.com/x/v3/fav/resource/list"
BILI_VIDEO_URL = "https://www.bilibili.com/video/"

BLOATED_THRESHOLD = 20

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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

    @property
    def lib_dir(self) -> Path:
        return self.vault / LIB_DIR_NAME

    @property
    def recycle_dir(self) -> Path:
        return self.lib_dir / RECYCLE_DIR_NAME

    @property
    def index_file(self) -> Path:
        return self.lib_dir / INDEX_FILE_NAME


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
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 某些终端不支持
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


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
                wait = min(8, 2 ** attempt)
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
        data = self._get(FAV_FOLDER_API, {"up_mid": self.cfg.uid, "web_location": "333.1387"})
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
                FAV_LIST_API,
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
# DeepSeek 分类
# =====================================================================

SYSTEM_PROMPT = """你是一个 B 站收藏视频分类与学习指导引擎。当前用户的背景是：正在备考计算机考研（数学二 + 408：数据结构、计算机组成原理、操作系统、计算机网络），同时有大学课程作业（Web开发、人机交互、软件需求分析）以及研究 AI Agent 工作流的爱好。
请根据输入的视频标题、简介、UP主、时长，输出以下 JSON 格式：
{
  "ai_category": "只能从 [01_考研408与数学, 02_AI与Agent工作流, 03_学校课业与开发, 04_效率与认知成长, 99_电子榨菜与待定] 中五选一",
  "sub_tag": "具体细分标签，如：计组、数据结构、Agent、前端、娱乐等",
  "cognitive_load": "只能从 [🔴深度, 🟡平稳, 🟢低耗] 中三选一",
  "ai_guide": "用一句大白话说明：这视频核心讲了啥？最适合我在什么精神状态下看（如：适合写计组实验报告卡壳时看10分钟，或累了当榨菜看）？",
  "link_to_vault": "如果与考研科目相关，输出双链如 [[04.数据结构]] 或 [[05.计算机组成原理]]，否则输出空字符串"
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


def normalize_ai(raw: dict[str, Any], item: BiliItem) -> dict[str, str]:
    category = str(raw.get("ai_category") or "").strip()
    if category not in CATEGORIES:
        category = _guess_category(category or f"{item.title} {item.intro}")

    load = str(raw.get("cognitive_load") or "").strip()
    if load not in COGNITIVE_LOADS:
        load = DEFAULT_LOAD

    sub_tag = str(raw.get("sub_tag") or "").strip() or "待定"
    guide = str(raw.get("ai_guide") or "").strip() or "先收藏着，等有空再看。"
    link = str(raw.get("link_to_vault") or "").strip()

    return {
        "ai_category": category,
        "sub_tag": sub_tag,
        "cognitive_load": load,
        "ai_guide": guide,
        "link_to_vault": link,
    }


def fallback_ai(item: BiliItem, reason: str = "") -> dict[str, str]:
    return {
        "ai_category": DEFAULT_CATEGORY,
        "sub_tag": "待定",
        "cognitive_load": DEFAULT_LOAD,
        "ai_guide": "AI 分类暂不可用，已先放进待定区；下次同步会自动补上分类。",
        "link_to_vault": "",
        "reason": reason,
    }


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

    def classify(self, item: BiliItem, retries: int = 3) -> dict[str, str]:
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
            "max_tokens": 500,
            "response_format": {"type": "json_object"},
        }

        last_error = ""
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.post(
                    f"{self.cfg.base_url}/chat/completions",
                    json=body,
                    timeout=60,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.RequestException(f"HTTP {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                raw = parse_json_loose(content)
                return normalize_ai(raw, item)
            except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
                last_error = str(exc)
                wait = min(10, 2 ** attempt)
                log.warning("DeepSeek 分类失败（第 %d 次）%s：%s", attempt, item.bvid, exc)
                if attempt < retries:
                    time.sleep(wait)

        return fallback_ai(item, last_error)


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


# =====================================================================
# 本地笔记
# =====================================================================


@dataclass
class LocalNote:
    path: Path
    fm: dict[str, Any]
    body: str  # 含 frontmatter 结束符之后的原始内容（保留换行）
    bvid: str


FRONTMATTER_RE = re.compile(r"^---\r?\n(?P<fm>.*?)\r?\n---(?P<body>.*)$", re.S)


def read_note(path: Path) -> LocalNote | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        log.warning("读取笔记失败 %s：%s", path, exc)
        return None

    match = FRONTMATTER_RE.match(text)
    if not match:
        return None
    try:
        fm = yaml.safe_load(match.group("fm")) or {}
    except yaml.YAMLError as exc:
        log.warning("YAML 解析失败 %s：%s", path, exc)
        return None
    if not isinstance(fm, dict):
        return None

    bvid = str(fm.get("bvid") or "").strip()
    if not bvid:
        return None
    return LocalNote(path=path, fm=fm, body=match.group("body"), bvid=bvid)


def write_note(path: Path, fm: dict[str, Any], body: str) -> None:
    fm_text = yaml.safe_dump(
        fm,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    ).rstrip("\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm_text}\n---{body}", encoding="utf-8")


def scan_notes(lib_dir: Path, recycle_dir: Path) -> list[LocalNote]:
    notes: list[LocalNote] = []
    if not lib_dir.exists():
        return notes
    for path in sorted(lib_dir.rglob("*.md")):
        if recycle_dir in path.parents:
            continue
        if path.name.startswith("00_"):
            continue
        note = read_note(path)
        if note is not None:
            notes.append(note)
    return notes


def ensure_dirs(cfg: Config) -> None:
    cfg.lib_dir.mkdir(parents=True, exist_ok=True)
    for category in CATEGORIES:
        (cfg.lib_dir / category).mkdir(parents=True, exist_ok=True)
    cfg.recycle_dir.mkdir(parents=True, exist_ok=True)


def duration_text(seconds: int) -> str:
    if not seconds:
        return "0"
    minutes = seconds / 60
    if minutes >= 10:
        return f"{minutes:.0f}"
    text = f"{minutes:.1f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def safe_filename(title: str, bvid: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > 80:
        name = name[:80].rstrip(" .")
    if not name:
        name = "未命名视频"
    return f"{name}_{bvid}.md"


def build_note_body(item: BiliItem, ai: dict[str, str]) -> str:
    guide = ai["ai_guide"]
    link = ai["link_to_vault"] or "—"
    folder = " / ".join(item.folders) or "未知"
    intro = item.intro or "（该视频没有简介）"
    minutes = duration_text(item.duration)
    url = f"{BILI_VIDEO_URL}{item.bvid}"

    return f"""

# 📺 {item.title}

> [!tip] 🤖 DeepSeek 食用指南
> **建议场景：** {guide}
> **关联知识库：** {link} | **原收藏夹：** `{folder}` | **时长：** {minutes} 分钟
> 🔗 [点击直达 B 站观看]({url})

## 📝 视频简介
{intro}

---
{NOTES_HEADING}
"""


def build_frontmatter(item: BiliItem, ai: dict[str, str]) -> dict[str, Any]:
    fm: dict[str, Any] = {
        "bvid": item.bvid,
        "title": item.title,
        "up": item.upper,
        "duration": f"{duration_text(item.duration)} 分钟",
        "url": f"{BILI_VIDEO_URL}{item.bvid}",
        "bili_folder": " / ".join(item.folders),
        "ai_category": ai["ai_category"],
        "sub_tag": ai["sub_tag"],
        "cognitive_load": ai["cognitive_load"],
        "status": "未看",
        "user_locked": False,
        "sync_date": datetime.now().strftime("%Y-%m-%d"),
    }
    if ai.get("reason"):
        # 标记为待补分类，下一轮同步会自动重试（用户手动锁定后不再重试）
        fm["ai_pending"] = True
    return fm


def replace_guide(body: str, item: BiliItem, ai: dict[str, str]) -> str:
    """只替换笔记顶部的「食用指南」callout，保留用户手写内容。"""
    link = ai["link_to_vault"] or "—"
    folder = " / ".join(item.folders) or "未知"
    minutes = duration_text(item.duration)
    block = (
        "> [!tip] 🤖 DeepSeek 食用指南\n"
        f"> **建议场景：** {ai['ai_guide']}\n"
        f"> **关联知识库：** {link} | **原收藏夹：** `{folder}` | **时长：** {minutes} 分钟\n"
        f"> 🔗 [点击直达 B 站观看]({BILI_VIDEO_URL}{item.bvid})\n"
    )
    pattern = re.compile(r"> \[!tip\] 🤖 DeepSeek 食用指南\r?\n(?:>.*\r?\n)+")
    if not pattern.search(body):
        return body
    return pattern.sub(block, body, count=1)


def has_handwritten_notes(body: str) -> bool:
    """判断「我的学习/折腾笔记」标题下是否有用户手写内容。"""
    index = body.find(NOTES_HEADING)
    if index == -1:
        return False
    tail = body[index + len(NOTES_HEADING):]
    tail = re.sub(r"<!--.*?-->", "", tail, flags=re.S)
    tail = re.sub(r"[\s\u3000]+", "", tail)
    tail = re.sub(r"^[-*_>#`~=|]+$", "", tail)
    return bool(tail)


def move_to_recycle(path: Path, recycle_dir: Path) -> Path:
    recycle_dir.mkdir(parents=True, exist_ok=True)
    target = recycle_dir / path.name
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        target = recycle_dir / f"{path.stem}_{stamp}{path.suffix}"
    shutil.move(str(path), str(target))
    return target


# =====================================================================
# 同步主流程
# =====================================================================


@dataclass
class SyncSummary:
    added: int = 0
    removed_to_recycle: int = 0
    kept_removed: int = 0
    locked: int = 0
    reclassified: int = 0
    bloated: list[str] = field(default_factory=list)
    total_notes: int = 0
    unwatched: int = 0
    ai_failed: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.errors,
            "added": self.added,
            "removed_to_recycle": self.removed_to_recycle,
            "kept_removed": self.kept_removed,
            "locked": self.locked,
            "reclassified": self.reclassified,
            "bloated": self.bloated,
            "total_notes": self.total_notes,
            "unwatched": self.unwatched,
            "ai_failed": self.ai_failed,
            "errors": self.errors,
            "duration_seconds": round(self.duration_seconds, 1),
            "dry_run": self.dry_run,
        }

    def one_line(self) -> str:
        return (
            f"新增 {self.added} 个视频，清理 {self.removed_to_recycle} 个已取消收藏视频，"
            f"发现 {len(self.bloated)} 个臃肿目录"
        )


def apply_auto_lock(notes: list[LocalNote], cfg: Config, summary: SyncSummary, dry_run: bool) -> None:
    for note in notes:
        actual_folder = note.path.parent.name
        recorded = str(note.fm.get("ai_category") or "").strip()
        # 只对位于预设分类目录里的笔记做判定，避免放在根目录的笔记被误锁
        if actual_folder not in CATEGORIES or actual_folder == recorded:
            continue

        if dry_run:
            log.info("[dry-run] 将锁定 %s：%s → %s", note.path.name, recorded, actual_folder)
        else:
            note.fm["ai_category"] = actual_folder
            note.fm["user_locked"] = True
            try:
                write_note(note.path, note.fm, note.body)
            except OSError as exc:
                summary.errors.append(f"锁定失败 {note.path.name}：{exc}")
                log.error("锁定失败 %s：%s", note.path.name, exc)
                continue
            log.info("检测到你手动调整了分类，已锁定：%s（%s → %s）", note.path.name, recorded, actual_folder)
        summary.locked += 1


def classify_items(cfg: Config, items: list[BiliItem], skip_ai: bool) -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    if skip_ai:
        log.info("--skip-ai 已启用，%d 个新视频放入待定区。", len(items))
        return {item.bvid: fallback_ai(item, "skip-ai") for item in items}

    if not cfg.api_key:
        log.warning("未配置 DEEPSEEK_API_KEY，%d 个新视频先放入待定区。", len(items))
        return {item.bvid: fallback_ai(item, "no-api-key") for item in items}

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
                results[item.bvid] = fallback_ai(item, str(exc))
            log.info("AI 分类进度 %d/%d", done, len(items))

    for item in items:
        results.setdefault(item.bvid, fallback_ai(item, "unknown"))
    return results


def run_sync(cfg: Config, dry_run: bool = False, skip_ai: bool = False) -> SyncSummary:
    started = time.time()
    summary = SyncSummary(dry_run=dry_run)
    ensure_dirs(cfg)

    # ---------- 1. 本地扫描 + 自动锁定 ----------
    notes = scan_notes(cfg.lib_dir, cfg.recycle_dir)
    log.info("本地已有 %d 篇视频笔记。", len(notes))
    apply_auto_lock(notes, cfg, summary, dry_run)

    local_by_bvid: dict[str, LocalNote] = {}
    for note in notes:
        if note.bvid in local_by_bvid:
            log.warning("发现重复 bvid 笔记，保留 %s，忽略 %s", local_by_bvid[note.bvid].path.name, note.path.name)
            continue
        local_by_bvid[note.bvid] = note

    # ---------- 2. 拉取云端 ----------
    cloud, cloud_complete, fetch_errors = BiliClient(cfg).collect_all()
    summary.errors.extend(fetch_errors)

    # ---------- 2.5 重试上一轮 AI 分类失败的笔记 ----------
    pending = [
        note
        for note in local_by_bvid.values()
        if note.fm.get("ai_pending") is True
        and note.fm.get("user_locked") is not True
        and note.bvid in cloud
    ]
    if pending:
        log.info("发现 %d 篇待补分类的笔记，重试 AI 分类……", len(pending))
        pending_ai = classify_items(cfg, [cloud[note.bvid] for note in pending], skip_ai)
        for note in pending:
            ai = pending_ai.get(note.bvid) or {}
            if ai.get("reason"):
                continue  # 仍然失败，保留 ai_pending 等下一轮
            item = cloud[note.bvid]
            category = ai["ai_category"] if ai["ai_category"] in CATEGORIES else DEFAULT_CATEGORY
            new_path = cfg.lib_dir / category / note.path.name
            if new_path.exists() and new_path != note.path:
                log.warning("补分类目标文件已存在，跳过：%s", new_path)
                continue

            note.fm.update(
                {
                    "ai_category": category,
                    "sub_tag": ai["sub_tag"],
                    "cognitive_load": ai["cognitive_load"],
                    "bili_folder": " / ".join(item.folders),
                    "sync_date": datetime.now().strftime("%Y-%m-%d"),
                }
            )
            note.fm.pop("ai_pending", None)
            body = replace_guide(note.body, item, ai)

            if dry_run:
                log.info("[dry-run] 将补分类并移动：%s → %s", note.path.name, category)
            else:
                try:
                    write_note(new_path, note.fm, body)
                    if new_path != note.path:
                        note.path.unlink()
                except OSError as exc:
                    summary.errors.append(f"补分类失败 {note.bvid}：{exc}")
                    log.error("补分类失败 %s：%s", note.bvid, exc)
                    continue
                note.path = new_path
                note.body = body
                log.info("补分类成功：%s → %s", note.path.name, category)
            summary.reclassified += 1

    # ---------- 3. 增量新增 ----------
    new_items = [item for bvid, item in cloud.items() if bvid not in local_by_bvid]
    if new_items:
        log.info("发现 %d 个新收藏，开始 AI 分类……", len(new_items))
        ai_map = classify_items(cfg, new_items, skip_ai)
        for item in new_items:
            ai = ai_map[item.bvid]
            if ai.get("reason") and ai.get("reason") != "skip-ai":
                summary.ai_failed += 1
            category = ai["ai_category"] if ai["ai_category"] in CATEGORIES else DEFAULT_CATEGORY
            target = cfg.lib_dir / category / safe_filename(item.title, item.bvid)
            if target.exists():
                log.warning("目标文件已存在，跳过：%s", target)
                continue
            if dry_run:
                log.info("[dry-run] 将新增：%s → %s", item.title, category)
            else:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    write_note(target, build_frontmatter(item, ai), build_note_body(item, ai))
                except OSError as exc:
                    summary.errors.append(f"写入失败 {item.bvid}：{exc}")
                    log.error("写入失败 %s：%s", item.bvid, exc)
                    continue
                log.info("已新增：%s → %s", item.title, category)
            summary.added += 1
    else:
        log.info("没有新收藏需要处理。")

    # ---------- 4. 删除同步 ----------
    removed = [note for note in local_by_bvid.values() if note.bvid not in cloud]
    if removed and (not cloud_complete or not cloud):
        reason = "云端数据不完整" if not cloud_complete else "云端一个视频都没读到"
        log.warning("%s，为安全起见跳过删除同步（%d 篇待确认）。", reason, len(removed))
    elif removed:
        for note in removed:
            if str(note.fm.get("status") or "") == "已取消收藏(保留笔记)":
                continue

            handwritten = has_handwritten_notes(note.body)
            if handwritten:
                note.fm["status"] = "已取消收藏(保留笔记)"
                if dry_run:
                    log.info("[dry-run] 将保留笔记并标记取消收藏：%s", note.path.name)
                else:
                    try:
                        write_note(note.path, note.fm, note.body)
                    except OSError as exc:
                        summary.errors.append(f"标记失败 {note.path.name}：{exc}")
                        log.error("标记失败 %s：%s", note.path.name, exc)
                        continue
                    log.info("视频已取消收藏，保留你的笔记：%s", note.path.name)
                summary.kept_removed += 1
            else:
                if dry_run:
                    log.info("[dry-run] 将移入回收站：%s", note.path.name)
                else:
                    try:
                        target = move_to_recycle(note.path, cfg.recycle_dir)
                    except OSError as exc:
                        summary.errors.append(f"移动失败 {note.path.name}：{exc}")
                        log.error("移动失败 %s：%s", note.path.name, exc)
                        continue
                    log.info("视频已取消收藏，空笔记移入回收站：%s", target.name)
                summary.removed_to_recycle += 1
    else:
        log.info("没有需要清理的已取消收藏视频。")

    # ---------- 5. 重建全局索引 ----------
    if dry_run:
        preview = scan_notes(cfg.lib_dir, cfg.recycle_dir)
        summary.total_notes = len(preview)
        summary.unwatched = sum(1 for note in preview if str(note.fm.get("status") or "") == "未看")
        log.info("[dry-run] 跳过索引重建。")
    else:
        summary.total_notes, summary.unwatched, summary.bloated = build_index(cfg)
        log.info("索引已更新：%s", cfg.index_file)

    if summary.bloated:
        log.warning("臃肿目录（>%d 条）：%s", BLOATED_THRESHOLD, "、".join(summary.bloated))

    summary.duration_seconds = time.time() - started
    for error in summary.errors:
        log.error(error)
    log.info("同步完成：%s（耗时 %.1fs）", summary.one_line(), summary.duration_seconds)
    return summary


# =====================================================================
# 索引生成
# =====================================================================


def _find_guide(body: str) -> str:
    match = re.search(r"\*\*建议场景：\*\*\s*(.+)", body)
    if not match:
        return "—"
    return _cell(match.group(1).strip())


def _cell(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).replace("|", "/").strip() or "—"


def build_index(cfg: Config) -> tuple[int, int, list[str]]:
    notes = scan_notes(cfg.lib_dir, cfg.recycle_dir)
    by_category: dict[str, list[LocalNote]] = {name: [] for name in CATEGORIES}
    for note in notes:
        category = str(note.fm.get("ai_category") or "").strip()
        if category not in by_category:
            category = note.path.parent.name if note.path.parent.name in CATEGORIES else DEFAULT_CATEGORY
        by_category.setdefault(category, []).append(note)

    bloated = [name for name, group in by_category.items() if len(group) > BLOATED_THRESHOLD]
    locked = sum(1 for note in notes if note.fm.get("user_locked") is True)
    unwatched = [note for note in notes if str(note.fm.get("status") or "") == "未看"]

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        "# 📚 收藏夹全景索引",
        "",
        f"> 由 `scripts/bili_sync.py` 自动生成 · 最后同步：**{now}**",
        f"> 有效笔记 **{len(notes)}** 篇 · 未看 **{len(unwatched)}** 篇 · 你手动锁定 **{locked}** 篇",
        "",
        "## 一、分类统计",
        "",
        "| 分类 | 数量 | 状态 |",
        "| --- | --- | --- |",
    ]

    for category in CATEGORIES:
        count = len(by_category.get(category, []))
        flag = f"🔴 臃肿（建议拆分，阈值 {BLOATED_THRESHOLD}）" if count > BLOATED_THRESHOLD else "✅ 正常"
        lines.append(f"| {category} | {count} | {flag} |")

    extra = [name for name in by_category if name not in CATEGORIES]
    for category in extra:
        lines.append(f"| {category}（非预设） | {len(by_category[category])} | ⚠️ 建议归并 |")

    lines.append("")
    if bloated:
        lines.append(f"> [!warning] 🔴 臃肿提醒：{('、'.join(bloated))} 超过 {BLOATED_THRESHOLD} 条，建议让 Claudian 生成拆分方案。")
    else:
        lines.append(f"> [!tip] 所有分类目录都在 {BLOATED_THRESHOLD} 条以内，结构健康。")
    lines.append("")

    lines.extend(
        [
            "## 二、待看清单（status: 未看）",
            "",
        ]
    )

    if not unwatched:
        lines.append("> 暂无未看视频。去 B 站收藏点新东西，或把已看视频的 `status` 改成 `已看` 吧。")
    else:
        unwatched.sort(
            key=lambda note: (
                CATEGORIES.index(str(note.fm.get("ai_category")))
                if str(note.fm.get("ai_category")) in CATEGORIES
                else len(CATEGORIES),
                LOAD_ORDER.get(str(note.fm.get("cognitive_load")), 9),
                str(note.fm.get("title") or ""),
            )
        )
        lines.extend(
            [
                "| 分类 | 认知负荷 | 时长 | 标题 | AI 一句话指导 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for note in unwatched:
            fm = note.fm
            title = _cell(fm.get("title") or note.path.stem)
            alias = title.replace("[", "(").replace("]", ")")
            link = f"[[{note.path.stem}\\|{alias}]]"
            lines.append(
                "| {cat} | {load} | {duration} | {link} | {guide} |".format(
                    cat=_cell(fm.get("ai_category")),
                    load=_cell(fm.get("cognitive_load")),
                    duration=_cell(fm.get("duration")),
                    link=link,
                    guide=_find_guide(note.body),
                )
            )

    preserved = sum(1 for note in notes if str(note.fm.get("status") or "") == "已取消收藏(保留笔记)")
    lines.extend(
        [
            "",
            "---",
            "",
            "## 三、维护说明",
            "",
            "- 新增/清理/重建索引：`python scripts/bili_sync.py`",
            "- 浏览器一键同步：先 `python scripts/bili_sync.py --serve`，再点油猴悬浮按钮",
            "- `user_locked: true` 的笔记，AI 与 Claudian 都不得擅自移动",
            f"- 已取消收藏但保留了手写笔记的视频：**{preserved}** 篇（留在原分类，不再参与待看推荐）",
            "",
        ]
    )

    cfg.index_file.write_text("\n".join(lines), encoding="utf-8")
    return len(notes), len(unwatched), bloated


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
        return jsonify({"ok": True, "service": "bili-obsidian-sync", "vault": str(cfg.vault)})

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
        description="B 站收藏夹 ➔ Obsidian 双向同步与 AI 策展",
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

    ensure_dirs(cfg)

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
    print(f"    本地笔记 {summary.total_notes} 篇 · 未看 {summary.unwatched} 篇 · 新锁定 {summary.locked} 篇")
    if summary.kept_removed:
        print(f"    其中 {summary.kept_removed} 篇因有手写笔记被保留")
    if summary.reclassified:
        print(f"    🤖 {summary.reclassified} 篇待定笔记已补上 AI 分类")
    if summary.ai_failed:
        print(f"    ⚠️ {summary.ai_failed} 篇 AI 分类失败，已放入待定区")
    if summary.bloated:
        print(f"    🔴 臃肿目录：{'、'.join(summary.bloated)}")
    print(f"    📄 索引：{cfg.index_file}")
    print("=" * 56)
    return 0 if not summary.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
