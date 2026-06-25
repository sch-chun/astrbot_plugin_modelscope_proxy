const bridge = window.AstrBotPluginPage;
let refreshTimer = null;

async function fetchQuotaStatus() {
  try {
    const data = await bridge.apiGet('quota_status');
    return data;
  } catch (err) {
    console.error('获取额度状态失败:', err);
    return null;
  }
}

function renderUserQuota(userQuota, userLimit, quotaReserve) {
  const fill = document.getElementById('user-quota-fill');
  const reserveFill = document.getElementById('user-quota-reserve-fill'); // 新增元素
  const text = document.getElementById('user-quota-text');

  if (userQuota === undefined || userQuota === null || userLimit === undefined || userLimit === null) {
    text.textContent = '未获取到额度信息';
    fill.style.width = '0%';
    if (reserveFill) reserveFill.style.width = '0%';
    return;
  }

  const reserve = quotaReserve || 0;
  const available = Math.max(0, userQuota - reserve); // 可用部分 = 剩余 - 保留
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

  // 文本： 显示可用次数，如果保留 > 0 才显示保留信息
  let textStr = `可用 ${available} 次`
  if (reserve > 0) {
    textStr += `（保留 ${reserve} 次）`;
  }
  textStr += ` / 总额度 ${total} 次`;
  text.textContent = textStr;
}

function renderVirtualModels(virtualModels) {
  const container = document.getElementById('virtual-models');
  if (!virtualModels || virtualModels.length === 0) {
    container.innerHTML = '<p>暂无虚拟模型配置</p>';
    return;
  }

  let html = '';
  for (const v of virtualModels) {
    const fallbackLabel = v.has_fallback ? '🔁 有兜底' : '无兜底';
    html += `<div class="virtual-model-section">
      <div class="virtual-name">
        <span>${v.name}</span>
        <span class="fallback-badge">${fallbackLabel}</span>
      </div>
      <div class="model-grid">`;

    if (v.models.length === 0) {
      html += `<p style="grid-column:1/-1; color:#888;">该虚拟模型下无配置模型</p>`;
    } else {
      for (const m of v.models) {
        const isExhausted = m.is_disabled && (m.remaining !== undefined && m.remaining !== null && m.remaining <= 0);
        const statusClass = m.is_disabled 
            ? (isExhausted ? 'exhausted' : 'disabled')
            : (m.is_cooldown ? 'cooldown' : 'available');

        let statusText = m.is_disabled 
            ? (isExhausted ? '已耗尽' : '已禁用')
            : (m.is_cooldown ? '冷却中' : '可用');
        const quotaText = m.remaining !== undefined && m.remaining !== null ? `${m.remaining} 次剩余` : '未获取';
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

async function refreshDashboard() {
  const data = await fetchQuotaStatus();
  if (!data) {
    document.getElementById('user-quota-text').textContent = '加载失败，请重试';
    return;
  }

  // 更新用户额度
  renderUserQuota(data.user_quota, data.user_limit, data.quota_reserve);

  // 更新虚拟模型
  renderVirtualModels(data.virtual_models);

  // 更新时间
  const now = new Date();
  document.getElementById('last-update').textContent = `最后更新: ${now.toLocaleTimeString()}`;
}

// 初始化
async function init() {
  await bridge.ready();
  await refreshDashboard();

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
