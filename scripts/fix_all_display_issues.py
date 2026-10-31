#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_all_display_issues.py — 全库 Markdown 显示问题一站式修复

对仓库下所有 .md 笔记执行修复：
  1) 代码块围栏粘连修复 + 裸代码块自动补全语言（cpp / python / html），随后整块隔离保护
  2) LaTeX 分隔符统一：\\( \\) → $...$；\\[ \\] → $$ 块并独立成行；
     多行 / 含 \\begin 的 $$...$$ 强制规范为独立成行并与正文留空行；
     行内 $ 剥离内侧空格；行内公式末尾误包的中文标点外移
  3) 表格上方补空行；标题 # 后补空格 / 上方补空行；行中标题粘连拆分；列表 "- 内容" 补空格
  4) 还原保护区（YAML / 代码块 / 行内代码 / 双链嵌入 / PDF++ 链接 / URL），压缩冗余空行

用法：
  python scripts/fix_all_display_issues.py            # 实际修复并写入
  python scripts/fix_all_display_issues.py --dry-run  # 只出扫描报告，不写入
"""

import argparse
import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {'.git', '.obsidian', '.trash', 'node_modules', '.agents',
             '.claudian', 'scripts', '_system'}
SKIP_FILES = {'经济记录.md', 'B站收藏夹.md', '任务.md', 'AGENTS.md'}

stats = Counter()
samples = []
MAX_SAMPLES = 60


def _sample(cat, path, before, after):
    if len(samples) < MAX_SAMPLES:
        samples.append((cat, path,
                        str(before).replace('\n', '⏎')[:180],
                        str(after).replace('\n', '⏎')[:180]))


# ---------------------------------------------------------------- placeholders
_TOK_RE = re.compile('\x00MDFIX(\\d+)\x00')
_ph = {}


def _protect(body):
    tok = '\x00MDFIX{}\x00'.format(len(_ph))
    _ph[tok] = body
    return tok


def _restore(text):
    return _TOK_RE.sub(lambda m: _ph[m.group(0)], text)


# ---------------------------------------------------------------- fence scanning
FENCE_OPEN_RE = re.compile(r'^[ \t]*`{3,}([\w+\-._]*)[ \t]*$')
FENCE_CLOSE_RE = re.compile(r'^[ \t]*`{3,}[ \t]*$')


def scan_fences(lines):
    """状态机扫描：返回 (opens=[{'i','lang'}], closes=[{'i'}], unclosed_start|None)。
    前提：粘连问题已在 fix_fences_glue 阶段拆分，所有围栏标记独立成行。"""
    opens, closes = [], []
    inside = False
    start = None
    for i, line in enumerate(lines):
        if not inside:
            m = FENCE_OPEN_RE.match(line)
            if m:
                opens.append({'i': i, 'lang': m.group(1)})
                inside = True
                start = i
        else:
            if FENCE_CLOSE_RE.match(line):
                closes.append({'i': i})
                inside = False
    return opens, closes, (start if inside else None)


def fix_fences_glue(lines, path):
    """开启/闭合围栏与其他文本粘连在同一行 → 拆开。"""
    out = []
    inside = False
    for line in lines:
        trip = line.find('```')
        if not inside:
            if trip != -1 and line[:trip].strip():
                # 正文```lang 粘连 → 拆成正文行 + 围栏行
                m2 = re.match(r'^`{3,}([\w+\-._]*)$', line[trip:], re.M)
                tail = line[trip:]
                m2 = re.match(r'^`{3,}(\S*)$', tail)
                if m2:
                    fence_line = tail.strip()
                    text_part = line[:trip].rstrip()
                else:
                    # ``` 后面还有别的内容：内容并回正文行，围栏独立
                    m3 = re.match(r'^`{3,}([\w+\-._]*)[ \t]+(.*)$', tail)
                    if m3:
                        fence_line = '```' + m3.group(1)
                        text_part = (line[:trip].rstrip() + ' ' + m3.group(2)).rstrip()
                    else:
                        fence_line = tail.split()[0]
                        text_part = (line[:trip].rstrip() + ' ' + tail[len(fence_line):]).rstrip()
                stats['fence_glue_split'] += 1
                _sample('围栏', path, line, text_part + ' ⏎ ' + fence_line)
                out.append(text_part)
                out.append(fence_line)
                inside = True
                continue
        else:
            stripped = line.rstrip()
            if not line.lstrip().startswith('```') and stripped.endswith('```') \
                    and stripped[:len(stripped) - 3].strip():
                # code``` 粘连闭合 → 拆开（整行是 ``` 时走正常闭合）
                e = stripped.rfind('```')
                if stripped[:e].strip():
                    stats['fence_glue_split'] += 1
                    _sample('围栏', path, line, stripped[:e].rstrip() + ' ⏎ ```')
                    out.append(stripped[:e].rstrip())
                    out.append('```')
                    inside = False
                    continue
        # 正常状态机推进
        if not inside and FENCE_OPEN_RE.match(line):
            inside = True
        elif inside and FENCE_CLOSE_RE.match(line):
            inside = False
        out.append(line)
    return out


def detect_lang(body):
    low = body.lower()
    if any(m in low for m in ('#include', 'int main', 'printf', 'scanf', 'malloc',
                              'struct ', 'typedef ', 'elemtype', 'lnode', 'bitree',
                              'vector<', 'cout', 'cin', 'std::')):
        return 'cpp'
    if any(m in low for m in ('def ', 'import ', 'print(')):
        return 'python'
    if any(m in low for m in ('<html', '<div', '<!doctype')):
        return 'html'
    return ''


def fix_fences(lines, path):
    lines = fix_fences_glue(lines, path)
    opens, closes, _ = scan_fences(lines)
    # 围栏前后补空行：开栏行之前、闭栏行之后（不动代码体内部）
    open_idx = {o['i'] for o in opens}
    close_idx = {c['i'] for c in closes}
    new = []
    for i, line in enumerate(lines):
        if i in open_idx and new and new[-1].strip() and '\x00' not in new[-1]:
            new.append('')
            stats['fence_blank_added'] += 1
        new.append(line)
        if i in close_idx and i + 1 < len(lines) and lines[i + 1].strip() \
                and '\x00' not in lines[i + 1]:
            new.append('')
            stats['fence_blank_added'] += 1
    # 裸围栏自动补语言
    opens, closes, _ = scan_fences(new)
    closes_sorted = sorted(c['i'] for c in closes)
    for o in opens:
        if o['lang']:
            continue
        end = next((c for c in closes_sorted if c > o['i']), len(new) - 1)
        body = new[o['i'] + 1:end]
        detected = detect_lang('\n'.join(body))
        if detected:
            pad = len(new[o['i']]) - len(new[o['i']].lstrip())
            new[o['i']] = ' ' * pad + '```' + detected
            stats['lang_added'] += 1
            _sample('语言', path,
                    '```   ({} 行裸代码)'.format(len(body)),
                    '```{}  [自动判定 {}]'.format(detected, detected))
    return new


# ---------------------------------------------------------------- protection
INLINE_CODE_RE = re.compile(r'`{1,3}[^`\n]*`{1,3}')
WIKILINK_RE = re.compile(r'!?\[\[[^\[\]\n]+\]\]')
MD_LINK_RE = re.compile(r'\[[^\[\]\n]*\]\([^)\n]+\)')
URL_RE = re.compile(r'(https?://[^\s)\]\u3000]+)')
FRONTMATTER_RE = re.compile(r'\A---\n[\s\S]*?\n---\n?')


def protect_code_blocks(text):
    lines = text.split('\n')
    opens, closes, unclosed = scan_fences(lines)
    spans = []
    for o in opens:
        c = next((c['i'] for c in closes if c['i'] > o['i']), None)
        spans.append((o['i'], c if c is not None else len(lines) - 1))
    # 从后往前整块替换为占位符（占位符仅一层，不嵌套）
    for (a, b) in sorted(set(spans), reverse=True):
        body = '\n'.join(lines[a:b + 1])
        lines[a:b + 1] = [_protect(body)]
    text = '\n'.join(lines)
    return text, bool(unclosed)


def protect_markup(text):
    # YAML frontmatter（可能误伤数学/表格规则的第一道防线）
    m = FRONTMATTER_RE.match(text)
    if m:
        text = _protect(m.group(0)) + text[m.end():]
    text, unclosed = protect_code_blocks(text)
    # 行内代码
    text = INLINE_CODE_RE.sub(lambda mm: _protect(mm.group(0)), text)
    # 双链 / 嵌入 / PDF++ 坐标链接
    text = WIKILINK_RE.sub(lambda mm: _protect(mm.group(0)), text)
    # markdown 链接与裸 URL
    text = MD_LINK_RE.sub(lambda mm: _protect(mm.group(0)), text)
    text = URL_RE.sub(lambda mm: _protect(mm.group(0)), text)
    return text


# ---------------------------------------------------------------- math fixes
INLINE_PAREN_RE = re.compile(r'\\\((.+?)\\\)', re.S)
DISPLAY_BRACKET_RE = re.compile(r'\\\[([\s\S]+?)\\\]')
CJK_PUNCT_SET = '。．，、；：！？'


def is_mathy(content):
    if not content or len(content) > 300:
        return False
    if not content.strip():
        return False
    if re.search(r'[\u4e00-\u9fff]|[\u3000-\u303f]|[\uff01-\uff5e]', content):
        return False
    return bool(re.search(r'[A-Za-z\\^_{}=+\-*/<>]', content))


def fix_math(text, path):
    # a) \( \) → $...$
    def _paren(mm):
        stats['paren_to_dollar'] += 1
        after = '$' + mm.group(1).strip() + '$'
        _sample('行内\\(\\)', path, mm.group(0), after)
        return after
    text = INLINE_PAREN_RE.sub(_paren, text)
    # b) \[ \] → 独立成行的 $$ 块
    def _bracket(mm):
        stats['bracket_to_block'] += 1
        after = '\n\n$$\n' + mm.group(1).strip().strip('\n') + '\n$$\n\n'
        _sample('\\[\\]', path, mm.group(0), after)
        return after
    text = DISPLAY_BRACKET_RE.sub(_bracket, text)
    # c) $$ 块规范化（内容跨行或含 \begin 时，$$ 必须独立成行）
    text = normalize_block_dollars(text, path)
    # d) 行内 $ 内侧空格剥离 + 末尾中文标点外移
    text = fix_inline_dollars(text, path)
    return text


def normalize_block_dollars(text, path):
    positions = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '\\':
            i += 2
            continue
        if ch == '$' and i + 1 < n and text[i + 1] == '$':
            positions.append(i)
            i += 2
            continue
        i += 1
    if len(positions) % 2 != 0:
        print('  警告: {} 中 $$ 出现奇数次({})，跳过该文件的块级规范化'
              .format(path, len(positions)))
        positions = positions[:len(positions) - 1]
    # 自后向前配对处理，保证更早的位置仍有效
    for k in range(len(positions) - 2, -1, -2):
        p1, p2 = positions[k], positions[k + 1]
        content = text[p1 + 2:p2]
        if '\n' not in content and '\\begin{' not in content:
            continue
        s_idx = text.rfind('\n', 0, p1) + 1
        e_pos = text.find('\n', p2)
        seg_end = e_pos + 1 if e_pos != -1 else len(text)
        prefix = text[s_idx:p1]
        suffix = text[p2 + 2:e_pos] if e_pos != -1 else text[p2 + 2:]
        body = content.strip('\n').rstrip()
        if not body:
            continue
        nxt = text[seg_end:].split('\n', 1)[0] if e_pos != -1 else ''
        new_seg = ''
        if prefix.strip():
            new_seg += prefix.rstrip() + '\n\n'
        elif s_idx > 0 and text[:s_idx - 1].rsplit('\n', 1)[-1].strip():
            # $$ 已在行首，但上一行非空 → 补空行
            new_seg += '\n'
        new_seg += '$$\n' + body + '\n$$'
        if suffix.strip():
            new_seg += '\n\n' + suffix.strip() + '\n'
        elif nxt.strip():
            new_seg += '\n\n'
        else:
            new_seg += '\n'
        text = text[:s_idx] + new_seg + text[seg_end:]
        stats['block_dollar_norm'] += 1
        _sample('$$块', path,
                (prefix.strip() or '') + '$$' + body[:40] + '$$',
                '$$⏎' + body[:40] + '⏎$$   (独立成行)')
    return text


def _strip_cjk_tail(content):
    moved = ''
    while content and content[-1] in CJK_PUNCT_SET:
        moved = content[-1] + moved
        content = content[:-1]
    return content, moved


def fix_inline_dollars(text, path):
    out = []
    for line in text.split('\n'):
        if '$$' in line or '$' not in line or '\x00' in line:
            out.append(line)
            continue
        pos = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '\\':
                i += 2
                continue
            if ch == '$':
                pos.append(i)
            i += 1
        if len(pos) < 2 or len(pos) % 2 != 0:
            out.append(line)
            continue
        pieces = []
        cursor = 0
        touched = False
        for k in range(0, len(pos) - 1, 2):
            a, b = pos[k], pos[k + 1]
            raw = line[a + 1:b]
            stripped, moved = _strip_cjk_tail(raw)
            inner = stripped.strip()
            newseg = '$' + inner + '$' + moved
            changed = newseg != '$' + raw + '$'
            valid = bool(inner) and is_mathy(inner)
            pieces.append(line[cursor:a])
            pieces.append(newseg if (valid and changed) else line[a:b + 1])
            if valid and changed:
                stats['inline_trim'] += 1
                touched = True
                _sample('行内$', path, '$' + raw + '$', newseg)
            cursor = b + 1
        pieces.append(line[cursor:])
        out.append(''.join(pieces) if touched else line)
    return '\n'.join(out)


# ---------------------------------------------------------------- structure
HEADING_START_RE = re.compile(r'^\s*#{1,6}\s')
INLINE_SPLIT_RE = re.compile(r'^(.{1,200}?[^\s])\s+(#{2,6}\s+[^\s]{1,60})$')
LIST_FIX_RE = re.compile(r'^(\s*)-(?![\s\-\d])(.*)$')
MATH_LINE_RE = re.compile(r'^\s*\$\$\s*$')


def fix_structure(text, path):
    lines = text.split('\n')
    # 标记 $$ 块的内部行（列表/标题规则跳过）
    in_math = [False] * len(lines)
    inside = False
    for i, line in enumerate(lines):
        if MATH_LINE_RE.match(line):
            inside = not inside
            in_math[i] = True
            continue
        in_math[i] = inside

    out = []
    for i, line in enumerate(lines):
        mathline = in_math[i]
        if '\x00' not in line and not mathline:
            # -- 行中标题粘连拆分（仅 ## 及以上，降低误伤标签） --
            ms = INLINE_SPLIT_RE.match(line)
            if ms and not HEADING_START_RE.match(line):
                stats['heading_inline_split'] += 1
                _sample('标题', path, line, ms.group(1).rstrip() + ' ⏎ ' + ms.group(2))
                out.append(ms.group(1).rstrip())
                out.append(ms.group(2))
                continue
            # -- 标题 # 后补空格（保守：>=2 个 #，或单个 # 后紧跟数字） --
            mh = re.match(r'^(\s*)(#{1,6})([^\s#].*)$', line)
            if mh and (len(mh.group(2)) >= 2 or re.match(r'\d', mh.group(3))):
                newline = mh.group(1) + mh.group(2) + ' ' + mh.group(3)
                stats['heading_space'] += 1
                _sample('标题', path, line, newline)
                line = newline
            # -- 列表 "-内容" 补空格（避开 ---/负数） --
            ml = LIST_FIX_RE.match(line)
            if ml and not HEADING_START_RE.match(line):
                newline = ml.group(1) + '- ' + ml.group(2)
                stats['list_space'] += 1
                _sample('列表', path, line, newline)
                line = newline
        # -- 标题上方补空行 --
        if HEADING_START_RE.match(line) and not mathline and '\x00' not in line and out:
            prev = out[-1]
            if prev.strip() and '\x00' not in prev and not HEADING_START_RE.match(prev) \
                    and not re.match(r'^\s*\$\$', prev):
                out.append('')
                stats['heading_blank'] += 1
        out.append(line)

    # -- 表格上方补空行 --
    def is_table_row(ln):
        return '\x00' not in ln and ln.lstrip().startswith('|') and ln.count('|') >= 2
    res = []
    n = len(out)
    for i, line in enumerate(out):
        if is_table_row(line) and res and res[-1].strip() and '\x00' not in res[-1] \
                and not is_table_row(res[-1]) \
                and i + 1 < n and is_table_row(out[i + 1]):
            res.append('')
            stats['table_blank'] += 1
        res.append(line)
    return '\n'.join(res)


BLANK_RUN_RE = re.compile(r'\n{4,}')


def collapse_blanks(text):
    def repl(m):
        blanks = m.group(0).count('\n') - 1
        if blanks >= 3:
            stats['blank_collapsed'] += blanks - 2
        return '\n\n\n'
    return BLANK_RUN_RE.sub(repl, text)


# ---------------------------------------------------------------- pipeline
def process_text(orig, relpath, had_crlf):
    text = orig.replace('\r\n', '\n')
    # 1) 围栏粘连拆分 / 前后空行 / 语言补全
    text = '\n'.join(fix_fences(text.split('\n'), relpath))
    # 保护隔离: YAML + 代码块 + 行内代码 + 双链 + 链接 URL
    text = protect_markup(text)
    # 2) 数学
    text = fix_math(text, relpath)
    # 3) 结构
    text = fix_structure(text, relpath)
    # 4) 还原 + 空行压缩
    text = _restore(text)
    text = collapse_blanks(text)
    if orig.endswith('\n') and not text.endswith('\n'):
        text += '\n'
    if had_crlf:
        text = text.replace('\n', '\r\n')
    return text


def find_md_files():
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.lower().endswith('.md') and fn not in SKIP_FILES:
                found.append(os.path.join(dirpath, fn))
    return sorted(found)


def main():
    ap = argparse.ArgumentParser(description='全库 Markdown 显示问题一站式修复')
    ap.add_argument('--dry-run', action='store_true', help='只输出报告，不写入文件')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    files = find_md_files()
    scanned = changed = 0
    for path in files:
        rel = os.path.relpath(path, ROOT)
        with open(path, 'rb') as f:
            raw = f.read()
        bom = raw.startswith(b'\xef\xbb\xbf')
        try:
            original = raw.decode('utf-8-sig' if bom else 'utf-8')
            enc = 'utf-8-sig' if bom else 'utf-8'
        except UnicodeDecodeError:
            try:
                original = raw.decode('gbk')
                enc = 'gbk'
            except UnicodeDecodeError:
                print('  跳过（非 UTF-8）: ' + rel)
                continue
        scanned += 1
        had_crlf = '\r\n' in original
        try:
            fixed = process_text(original, rel, had_crlf)
        except Exception as exc:
            import traceback
            print('  异常跳过 {}: {}'.format(rel, exc))
            traceback.print_exc()
            continue
        if fixed != original:
            changed += 1
            if not args.dry_run:
                with open(path, 'w', encoding=enc, newline='') as f:
                    f.write(fixed)

    print('=' * 62)
    print('全库显示问题扫描报告  ' +
          ('（--dry-run 预览，未写入）' if args.dry_run else ''))
    print('=' * 62)
    total_math = stats['paren_to_dollar'] + stats['bracket_to_block'] + \
        stats['block_dollar_norm'] + stats['inline_trim']
    total_code = stats['fence_glue_split'] + stats['lang_added'] + \
        stats['fence_blank_added']
    total_struct = stats['heading_space'] + stats['heading_blank'] + \
        stats['heading_inline_split'] + stats['table_blank'] + stats['list_space']
    print('共扫描: {} 个文件    共修改: {} 个文件'.format(scanned, changed))
    print()
    print('数学公式共修复 {} 处：'.format(total_math))
    print('    \\( \\) → $...$          : {}'.format(stats['paren_to_dollar']))
    print('    \\[ \\] → $$ 独立成行      : {}'.format(stats['bracket_to_block']))
    print('    块级 $$ 规范独立成行      : {}'.format(stats['block_dollar_norm']))
    print('    行内 $ 剥空格/标点外移    : {}'.format(stats['inline_trim']))
    print('代码块共修复 {} 处：'.format(total_code))
    print('    围栏粘连拆分            : {}'.format(stats['fence_glue_split']))
    print('    裸围栏自动补语言         : {}'.format(stats['lang_added']))
    print('    围栏前后补空行           : {}'.format(stats['fence_blank_added']))
    print('表格/标题/列表共修复 {} 处：'.format(total_struct))
    print('    表格上方补空行           : {}'.format(stats['table_blank']))
    print('    标题 # 后补空格          : {}'.format(stats['heading_space']))
    print('    标题上方补空行           : {}'.format(stats['heading_blank']))
    print('    行中标题粘连拆分         : {}'.format(stats['heading_inline_split']))
    print('    列表 "- " 补空格         : {}'.format(stats['list_space']))
    print('冗余空行压缩 {} 处'.format(stats['blank_collapsed']))
    print()
    print('典型修改示例（最多 10 处）：')
    for cat, pat, before, after in samples[:10]:
        print('  [{}] {}'.format(cat, pat))
        print('      - {}'.format(before))
        print('      + {}'.format(after))
    return 0


if __name__ == '__main__':
    sys.exit(main())
