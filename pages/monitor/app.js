const bridge = window.AstrBotPluginPage;
let refreshTimer = null;

// i18n 辅助函数：替换模板字符串中的占位符 {n}, {time} 等
function t(key, fallback, params) {
  let text = bridge.t(key, fallback);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      text = text.replace(new RegExp(`\\{${k}\\}`, 'g'), v);
    }
  }
  return text;
}

async function fetchQuotaStatus() {
  try {
    const data = await bridge.apiGet('quota_status');
    return data;
  } catch (err) {
    console.error('Failed to fetch quota status:', err);
    return null;
  }
}

function renderUserQuota(userQuota, userLimit, quotaReserve) {
  const fill = document.getElementById('user-quota-fill');
  const reserveFill = document.getElementById('user-quota-reserve-fill');
  const text = document.getElementById('user-quota-text');

  if (userQuota === undefined || userQuota === null || userLimit === undefined || userLimit === null) {
    text.textContent = t('pages.monitor.no_quota_info', '未获取到额度信息');
    fill.style.width = '0%';
    if (reserveFill) reserveFill.style.width = '0%';
    return;
  }

  const reserve = quotaReserve || 0;
  const available = Math.max(0, userQuota - reserve);
  const total = userLimit;

  const availablePercent = total > 0 ? Math.min(100, (available / total) * 100) : 0;
  const reservePercent = total > 0 ? Math.min(100, (reserve / total) * 100) : 0;

  fill.style.width = availablePercent + '%';
  fill.className = 'bar-fill';
  if (availablePercent < 20) fill.classList.add('low');
  else if (availablePercent < 50) fill.classList.add('medium');

  if (reserveFill) {
    reserveFill.style.width = reservePercent + '%';
  }

  let textStr = t('pages.monitor.available_count', '可用 {n} 次', { n: available });
  if (reserve > 0) {
    textStr += t('pages.monitor.reserved_count', '（保留 {n} 次）', { n: reserve });
  }
  textStr += t('pages.monitor.total_count', ' / 总额度 {n} 次', { n: total });
  text.textContent = textStr;
}

function renderVirtualModels(virtualModels) {
  const container = document.getElementById('virtual-models');
  if (!virtualModels || virtualModels.length === 0) {
    container.innerHTML = `<p>${t('pages.monitor.no_virtual_models', '暂无虚拟模型配置')}</p>`;
    return;
  }

  let html = '';
  for (const v of virtualModels) {
    const fallbackLabel = v.has_fallback
      ? t('pages.monitor.has_fallback', '🔁 有兜底')
      : t('pages.monitor.no_fallback', '无兜底');
    html += `<div class="virtual-model-section">
      <div class="virtual-name">
        <span>${v.name}</span>
        <span class="fallback-badge">${fallbackLabel}</span>
      </div>
      <div class="model-grid">`;

    if (v.models.length === 0) {
      html += `<p style="grid-column:1/-1; color:#888;">${t('pages.monitor.no_models', '该虚拟模型下无配置模型')}</p>`;
    } else {
      for (const m of v.models) {
        const isExhausted = m.is_disabled && (m.remaining !== undefined && m.remaining !== null && m.remaining <= 0);
        const statusClass = m.is_disabled
            ? (isExhausted ? 'exhausted' : 'disabled')
            : (m.is_cooldown ? 'cooldown' : 'available');

        let statusText;
        if (m.is_disabled) {
          statusText = isExhausted
            ? t('pages.monitor.exhausted', '已耗尽')
            : t('pages.monitor.disabled', '已禁用');
        } else {
          statusText = m.is_cooldown
            ? t('pages.monitor.cooldown', '冷却中')
            : t('pages.monitor.available', '可用');
        }

        const quotaText = m.remaining !== undefined && m.remaining !== null
          ? t('pages.monitor.count_remaining', '{n} 次剩余', { n: m.remaining })
          : t('pages.monitor.not_obtained', '未获取');

        html += `
          <div class="model-card">
            <div class="name">${m.id}</div>
            <div class="status">
              <span class="dot ${statusClass}"></span>
              <span>${statusText}</span>
            </div>
            <div class="quota">${quotaText}</div>
          </div>
        `;
      }
    }
    html += `</div></div>`;
  }
  container.innerHTML = html;
}

function renderUI() {
  // 静态 HTML 文案
  const heading = document.querySelector('header h1');
  if (heading) heading.textContent = t('pages.monitor.heading', '📊 ModelScope 额度监控');

  const userQuotaTitle = document.querySelector('#user-quota h2');
  if (userQuotaTitle) userQuotaTitle.textContent = t('pages.monitor.user_quota_title', '用户全局额度');

  const refreshBtn = document.getElementById('refresh-btn');
  if (refreshBtn) refreshBtn.textContent = t('pages.monitor.refresh', '🔄 刷新');

  const loadingSpan = document.getElementById('user-quota-text');
  if (loadingSpan && loadingSpan.textContent === '加载中...' || loadingSpan.textContent === 'Loading...') {
    loadingSpan.textContent = t('pages.monitor.loading', '加载中...');
  }
}

async function refreshDashboard() {
  const data = await fetchQuotaStatus();
  if (!data) {
    document.getElementById('user-quota-text').textContent = t('pages.monitor.load_failed', '加载失败，请重试');
    return;
  }

  renderUserQuota(data.user_quota, data.user_limit, data.quota_reserve);
  renderVirtualModels(data.virtual_models);

  const now = new Date();
  document.getElementById('last-update').textContent = t('pages.monitor.last_update', '最后更新: {time}', { time: now.toLocaleTimeString() });
}

// 初始化
async function init() {
  await bridge.ready();
  renderUI();
  await refreshDashboard();

  // 监听语言切换
  bridge.onContext(() => {
    renderUI();
    refreshDashboard();
  });

  // 刷新按钮
  document.getElementById('refresh-btn').addEventListener('click', refreshDashboard);

  // 自动刷新（每30秒）
  refreshTimer = setInterval(refreshDashboard, 30000);

  // 页面卸载时清理
  window.addEventListener('beforeunload', () => {
    if (refreshTimer) clearInterval(refreshTimer);
  });
}

init();