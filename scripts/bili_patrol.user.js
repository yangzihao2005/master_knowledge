// ==UserScript==
// @name         B站收藏夹 ➔ Obsidian 巡逻兵
// @namespace    local.bili.obsidian
// @version      1.0.0
// @description  在 B 站收藏夹/视频页一键触发本地 bili_sync.py，完成同步、AI 分类与索引重建
// @author       master
// @match        *://space.bilibili.com/*/favlist*
// @match        *://www.bilibili.com/video/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

(function () {
    'use strict';

    const ENDPOINT = 'http://127.0.0.1:5055/sync';
    const HEALTH = 'http://127.0.0.1:5055/health';
    const BUTTON_ID = 'obsidian-sync-capsule';
    const TOAST_ID = 'obsidian-sync-toast';
    const REQUEST_TIMEOUT = 30 * 60 * 1000; // 首次全量同步可能较久

    let busy = false;

    /* ---------------- 样式 ---------------- */
    const style = document.createElement('style');
    style.textContent = `
      #${BUTTON_ID} {
        position: fixed;
        right: 24px;
        bottom: 96px;
        z-index: 2147483000;
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 10px 18px;
        border: none;
        border-radius: 999px;
        font-size: 14px;
        font-weight: 600;
        letter-spacing: .3px;
        color: #fff;
        cursor: pointer;
        background: linear-gradient(135deg, #fb7299 0%, #7c5cff 100%);
        box-shadow: 0 8px 24px rgba(124, 92, 255, .38), 0 2px 6px rgba(0, 0, 0, .12);
        transition: transform .18s ease, box-shadow .18s ease, opacity .18s ease;
        backdrop-filter: blur(8px);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
        user-select: none;
      }
      #${BUTTON_ID}:hover { transform: translateY(-2px) scale(1.02); box-shadow: 0 12px 28px rgba(124, 92, 255, .48); }
      #${BUTTON_ID}:active { transform: translateY(0) scale(.99); }
      #${BUTTON_ID}[data-busy="true"] { cursor: progress; opacity: .82; }
      #${BUTTON_ID}[data-busy="true"] .capsule-dot {
        animation: obsidian-pulse 1s ease-in-out infinite;
      }
      #${BUTTON_ID} .capsule-dot {
        width: 8px; height: 8px; border-radius: 50%;
        background: #fff; box-shadow: 0 0 8px rgba(255,255,255,.9);
        flex: 0 0 auto;
      }
      @keyframes obsidian-pulse { 0%,100% { opacity: .35; transform: scale(.8);} 50% { opacity: 1; transform: scale(1.15);} }

      #${TOAST_ID} {
        position: fixed;
        right: 24px;
        bottom: 160px;
        z-index: 2147483001;
        max-width: 420px;
        padding: 14px 18px;
        border-radius: 14px;
        background: rgba(24, 26, 34, .96);
        color: #f4f5f7;
        font-size: 13px;
        line-height: 1.65;
        box-shadow: 0 14px 40px rgba(0, 0, 0, .35);
        border: 1px solid rgba(255, 255, 255, .09);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
        opacity: 0;
        transform: translateY(8px);
        transition: opacity .22s ease, transform .22s ease;
        pointer-events: none;
        white-space: pre-wrap;
        word-break: break-word;
      }
      #${TOAST_ID}.show { opacity: 1; transform: translateY(0); }
      #${TOAST_ID}.error { border-color: rgba(255, 107, 107, .55); }
      #${TOAST_ID} .toast-title { font-weight: 700; margin-bottom: 4px; }
    `;
    document.head.appendChild(style);

    /* ---------------- 通用请求（优先 GM_xmlhttpRequest，绕开混合内容限制） ---------------- */
    function request(url, method, data) {
        return new Promise((resolve, reject) => {
            if (typeof GM_xmlhttpRequest === 'function') {
                GM_xmlhttpRequest({
                    method: method,
                    url: url,
                    headers: { 'Content-Type': 'application/json' },
                    data: data ? JSON.stringify(data) : undefined,
                    timeout: REQUEST_TIMEOUT,
                    onload: (res) => {
                        let body = null;
                        try { body = JSON.parse(res.responseText); } catch (e) { body = { error: res.responseText }; }
                        if (res.status >= 200 && res.status < 300) resolve(body);
                        else reject(new Error((body && body.error) || `HTTP ${res.status}`));
                    },
                    onerror: () => reject(new Error('CONNECT_FAILED')),
                    ontimeout: () => reject(new Error('请求超时（同步可能仍在后台运行，请稍后看 Obsidian）')),
                });
            } else {
                fetch(url, {
                    method: method,
                    headers: { 'Content-Type': 'application/json' },
                    body: data ? JSON.stringify(data) : undefined,
                })
                    .then(async (res) => {
                        const body = await res.json().catch(() => ({}));
                        if (res.ok) resolve(body);
                        else reject(new Error(body.error || `HTTP ${res.status}`));
                    })
                    .catch(() => reject(new Error('CONNECT_FAILED')));
            }
        });
    }

    /* ---------------- Toast ---------------- */
    let toastTimer = null;
    function toast(title, message, isError) {
        let el = document.getElementById(TOAST_ID);
        if (!el) {
            el = document.createElement('div');
            el.id = TOAST_ID;
            document.body.appendChild(el);
        }
        el.innerHTML = '';
        const titleEl = document.createElement('div');
        titleEl.className = 'toast-title';
        titleEl.textContent = title;
        const bodyEl = document.createElement('div');
        bodyEl.textContent = message;
        el.appendChild(titleEl);
        el.appendChild(bodyEl);
        el.classList.toggle('error', !!isError);
        requestAnimationFrame(() => el.classList.add('show'));

        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove('show'), isError ? 12000 : 9000);
    }

    /* ---------------- 按钮 ---------------- */
    function setBusy(value, text) {
        busy = value;
        const btn = document.getElementById(BUTTON_ID);
        if (!btn) return;
        btn.dataset.busy = value ? 'true' : 'false';
        btn.disabled = value;
        const label = btn.querySelector('.capsule-label');
        if (label) label.textContent = text;
    }

    function buildButton() {
        if (document.getElementById(BUTTON_ID)) return;
        const btn = document.createElement('button');
        btn.id = BUTTON_ID;
        btn.type = 'button';
        btn.dataset.busy = 'false';
        btn.title = '把 B 站收藏同步到 Obsidian（需要先运行 python scripts/bili_sync.py --serve）';

        const dot = document.createElement('span');
        dot.className = 'capsule-dot';
        const label = document.createElement('span');
        label.className = 'capsule-label';
        label.textContent = '⚡ 同步至 Obsidian';

        btn.appendChild(dot);
        btn.appendChild(label);
        btn.addEventListener('click', onSyncClick);
        document.body.appendChild(btn);
    }

    async function onSyncClick() {
        if (busy) return;

        setBusy(true, '🔄 同步与 AI 分类中...');
        toast('⏳ 正在同步', '正在拉取 B 站收藏夹并调用 DeepSeek 分类，请稍候……\n（首次全量同步可能需要几分钟）');

        try {
            await request(HEALTH, 'GET');
        } catch (e) {
            setBusy(false, '⚡ 同步至 Obsidian');
            toast(
                '❌ 连不上本地同步服务',
                '请先在电脑上运行：\n  python scripts/bili_sync.py --serve\n然后重新点击按钮。',
                true
            );
            return;
        }

        try {
            const result = await request(ENDPOINT, 'POST', {});
            const lines = [result.message || '同步完成'];
            if (typeof result.total_notes === 'number') {
                lines.push(`本地笔记 ${result.total_notes} 篇 · 未看 ${result.unwatched} 篇`);
            }
            if (result.kept_removed) lines.push(`保留手写笔记 ${result.kept_removed} 篇（视频已取消收藏）`);
            if (result.locked) lines.push(`自动锁定你手动调整过分类的笔记 ${result.locked} 篇`);
            if (result.reclassified) lines.push(`🤖 ${result.reclassified} 篇待定笔记已补上 AI 分类`);
            if (result.ai_failed) lines.push(`⚠️ ${result.ai_failed} 篇 AI 分类失败，已放入待定区`);
            if (result.bloated && result.bloated.length) lines.push('🔴 臃肿目录：' + result.bloated.join('、'));
            if (result.errors && result.errors.length) lines.push('⚠️ ' + result.errors.join('\n⚠️ '));

            setBusy(false, '⚡ 同步至 Obsidian');
            toast(result.ok === false ? '⚠️ 同步完成（有告警）' : '✅ 同步完成', lines.join('\n'), result.ok === false);
        } catch (e) {
            setBusy(false, '⚡ 同步至 Obsidian');
            const message = e && e.message === 'CONNECT_FAILED'
                ? '请先在电脑上运行：\n  python scripts/bili_sync.py --serve'
                : (e && e.message) || '未知错误';
            toast('❌ 同步失败', message, true);
        }
    }

    /* ---------------- B 站是 SPA，定时补挂按钮 ---------------- */
    buildButton();
    setInterval(buildButton, 3000);
})();
