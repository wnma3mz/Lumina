async function saveSettings() {
  var btn = document.getElementById('save-btn');
  var msg = document.getElementById('settings-msg');
  btn.disabled = true;
  btn.textContent = '保存中…';
  if (msg) {
    msg.textContent = '';
    msg.className = 'settings-msg';
  }

  var inner = document.getElementById('settings-inner');
  if (!inner) {
    if (msg) {
      msg.textContent = '表单未加载，请稍后重试';
      msg.className = 'settings-msg warn';
    }
    btn.disabled = false;
    btn.textContent = '保存配置';
    return;
  }

  var payload = {};
  var arrayGroups = {};

  function _setNested(obj, keys, val) {
    for (var i = 0; i < keys.length - 1; i++) {
      if (!obj[keys[i]] || typeof obj[keys[i]] !== 'object') obj[keys[i]] = {};
      obj = obj[keys[i]];
    }
    obj[keys[keys.length - 1]] = val;
  }

  inner.querySelectorAll('input[name]:not([type=checkbox])').forEach(function(el) {
    if (!el.name) return;
    var val = el.value;
    if (el.type === 'number' && val !== '') val = el.step && parseFloat(el.step) < 1 ? parseFloat(val) : parseInt(val);
    _setNested(payload, el.name.split('.'), val);
  });
  inner.querySelectorAll('input[name][type=checkbox]').forEach(function(el) {
    if (!el.name) return;
    if (el.dataset.arrayTarget) {
      if (!arrayGroups[el.dataset.arrayTarget]) arrayGroups[el.dataset.arrayTarget] = [];
      if (el.checked) arrayGroups[el.dataset.arrayTarget].push(el.value);
      return;
    }
    _setNested(payload, el.name.split('.'), el.checked);
  });
  Object.keys(arrayGroups).forEach(function(path) {
    _setNested(payload, path.split('.'), arrayGroups[path]);
  });
  inner.querySelectorAll('select[name]').forEach(function(el) {
    if (!el.name) return;
    var val = el.value;
    if (val !== '' && !isNaN(Number(val))) val = Number(val);
    _setNested(payload, el.name.split('.'), val);
  });
  inner.querySelectorAll('textarea[name]').forEach(function(el) {
    if (!el.name) return;
    if (el.name === 'digest.scan_dirs_text') {
      if (!payload.digest) payload.digest = {};
      payload.digest.scan_dirs = el.value.split('\n').map(function(x) { return x.trim(); }).filter(Boolean);
    } else {
      _setNested(payload, el.name.split('.'), el.value);
    }
  });
  if (payload.digest) delete payload.digest.scan_dirs_text;

  try {
    var res = await fetch('/v1/config', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      var errData = await res.json().catch(function() { return { detail: res.statusText }; });
      throw new Error(errData.detail || res.statusText);
    }
    var data = await res.json();
    if (msg) {
      msg.textContent = '保存成功';
      msg.className = 'settings-msg';
    }
    var notice = document.getElementById('restart-notice');
    if (notice) notice.style.display = data.restart_required ? 'flex' : 'none';
    setTimeout(function() {
      if (msg && msg.textContent === '保存成功') msg.textContent = '';
    }, 3000);
  } catch (e) {
    if (msg) {
      msg.textContent = '保存失败：' + e.message;
      msg.className = 'settings-msg warn';
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '保存配置';
    if (typeof updateFloatingUiState === 'function') updateFloatingUiState();
  }
}

function syncLocalHomeUiForm() {
  var homeUi = getHomeUiConfig();
  var box = document.getElementById('local-home-pref-box');
  if (!box) return;
  box.classList.toggle('opacity-60', homeUi.allow_local_override === false);
  box.classList.toggle('pointer-events-none', homeUi.allow_local_override === false);
  var tabs = getEffectiveHomeTabs();
  document.querySelectorAll('input[data-local-home-tab]').forEach(function(el) {
    el.checked = tabs.includes(el.value);
    el.disabled = homeUi.allow_local_override === false;
  });
}

function saveLocalHomePrefs() {
  var tabs = [];
  document.querySelectorAll('input[data-local-home-tab]:checked').forEach(function(el) {
    tabs.push(el.value);
  });
  if (!tabs.length) {
    var msg = document.getElementById('settings-msg');
    if (msg) {
      msg.textContent = '本地首页至少保留一个入口';
      msg.className = 'settings-msg warn';
    }
    return;
  }
  saveLocalHomeTabs(tabs);
  applyHomeTabVisibility();
  syncLocalHomeUiForm();
  var doneMsg = document.getElementById('settings-msg');
  if (doneMsg) {
    doneMsg.textContent = '已保存当前浏览器的首页偏好';
    doneMsg.className = 'settings-msg';
  }
}

function resetLocalHomePrefs() {
  clearLocalHomeTabs();
  applyHomeTabVisibility();
  syncLocalHomeUiForm();
  var msg = document.getElementById('settings-msg');
  if (msg) {
    msg.textContent = '已恢复为全局默认首页';
    msg.className = 'settings-msg';
  }
}

function setProviderType(type, btn) {
  var input = document.getElementById('provider-type-input');
  if (input) input.value = type;

  document.querySelectorAll('#provider-type-group .radio-btn').forEach(function(el) {
    el.classList.remove('bg-white', 'dark:bg-zinc-700', 'shadow-sm', 'text-zinc-900', 'dark:text-zinc-100');
    el.classList.add('text-zinc-500');
  });

  if (btn) {
    btn.classList.remove('text-zinc-500');
    btn.classList.add('bg-white', 'dark:bg-zinc-700', 'shadow-sm', 'text-zinc-900', 'dark:text-zinc-100');
  }

  var localFields = document.getElementById('local-fields');
  var openaiFields = document.getElementById('openai-fields');
  if (!localFields || !openaiFields) return;

  if (type === 'local') {
    localFields.classList.remove('hidden');
    localFields.classList.add('block');
    openaiFields.classList.remove('flex');
    openaiFields.classList.add('hidden');
  } else {
    localFields.classList.remove('block');
    localFields.classList.add('hidden');
    openaiFields.classList.remove('hidden');
    openaiFields.classList.add('flex');
  }
}

function switchSettingsSubTab(key, btn) {
  document.querySelectorAll('#settings-seg-ctrl .seg-btn').forEach(function(el) {
    el.classList.remove('bg-indigo-50', 'dark:bg-indigo-900/20', 'text-indigo-600', 'dark:text-indigo-400');
    el.classList.add('text-zinc-500');
  });

  if (btn) {
    btn.classList.remove('text-zinc-500');
    btn.classList.add('bg-indigo-50', 'dark:bg-indigo-900/20', 'text-indigo-600', 'dark:text-indigo-400');
  }

  document.querySelectorAll('.settings-subtab').forEach(function(el) {
    el.classList.remove('flex');
    el.classList.add('hidden');
  });

  var tab = document.getElementById('stab-' + key);
  if (tab) {
    tab.classList.remove('hidden');
    tab.classList.add('flex');
  }

  if (key === 'ui' && typeof syncLocalHomeUiForm === 'function') {
    syncLocalHomeUiForm();
  }
  if (key === 'server' && typeof checkUpdate === 'function') {
    checkUpdate();
  }
}

document.body.addEventListener('htmx:afterSwap', function(event) {
  if (!event || !event.target || event.target.id !== 'panel-settings') return;
  if (typeof syncLocalHomeUiForm === 'function') {
    syncLocalHomeUiForm();
  }
  if (typeof updateFloatingUiState === 'function') {
    updateFloatingUiState();
  }
  // config_form.html 加载完毕后，若当前活跃子 tab 是 server 则触发更新检查
  var serverTab = document.getElementById('stab-server');
  if (serverTab && !serverTab.classList.contains('hidden')) {
    if (typeof checkUpdate === 'function') checkUpdate();
  }
});

async function toggleFeature(featureName, enabled) {
  try {
    var payload = { ui: { home: {} } };
    payload.ui.home[featureName + '_enabled'] = enabled;
    
    var res = await fetch('/v1/config', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    
    if (res.ok) {
      // 动态更新 DOM：底部导航栏标签
      var navLabel = document.querySelector('label[data-tab="' + featureName + '"]');
      if (navLabel) {
        navLabel.style.display = enabled ? '' : 'none';
      }
      
      // 对于 Digest 面板中展示的其他入口卡片 (Document, Image) 也进行显隐控制
      if (featureName === 'image' || featureName === 'document') {
        var digestCard = document.getElementById('digest-' + featureName + '-card');
        if (digestCard) {
          if (enabled) {
            digestCard.classList.remove('hidden');
          } else {
            digestCard.classList.add('hidden');
          }
        }
      }
      
      // 动态更新 DOM：如果当前正好停留在被关闭的 tab，则切回一个默认的可用的 tab
      var currentTabRadio = document.getElementById('tab-' + featureName);
      if (!enabled && currentTabRadio && currentTabRadio.checked) {
        var defaultTab = document.getElementById('tab-settings'); // 降级到设置页
        if (featureName !== 'digest' && document.getElementById('tab-digest')) {
          defaultTab = document.getElementById('tab-digest');
        }
        if (defaultTab) {
          defaultTab.checked = true;
          defaultTab.dispatchEvent(new Event('change'));
        }
      }
    }
  } catch (e) {
    console.error("Failed to toggle feature " + featureName + ":", e);
  }
}

async function pruneRequestHistory(btn) {
  var msg = document.getElementById('settings-msg');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '清理中…';
  }
  if (msg) {
    msg.textContent = '';
    msg.className = 'settings-msg';
  }
  try {
    var res = await fetch('/v1/config/request_history/prune', { method: 'POST' });
    if (!res.ok) throw new Error((await res.json().catch(function() { return {}; })).detail || res.statusText);
    var d = await res.json();
    var st = d.stats || {};
    var freed = ((st.freed_bytes || 0) / (1024 * 1024)).toFixed(2);
    if (msg) {
      msg.textContent = '清理完成：压缩 ' + (st.compressed || 0) + ' 个，删除 ' + (st.deleted || 0) + ' 个，释放 ' + freed + ' MB';
      msg.className = 'settings-msg';
    }
    if (window.htmx) htmx.trigger(document.body, 'refreshStorage');
  } catch (e) {
    if (msg) {
      msg.textContent = '清理失败：' + e.message;
      msg.className = 'settings-msg warn';
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '立即清理';
    }
  }
}

async function checkUpdate() {
  var el = document.getElementById('update-info');
  var btn = document.getElementById('update-check-btn');
  if (!el) return;
  if (btn) btn.textContent = '检查中…';
  try {
    var res = await fetch('/v1/update');
    var d = await res.json();
    if (d.has_update) {
      el.innerHTML = '<div class="flex items-center gap-2 flex-wrap">'
        + '<span class="text-xs text-zinc-500">当前 <span class="font-mono">' + escHtml(d.current) + '</span></span>'
        + '<span class="text-xs text-emerald-500 font-bold">→ ' + escHtml(d.latest) + ' 可用</span>'
        + '<a href="' + escHtml(d.release_url) + '" target="_blank"'
        + ' class="text-xs text-indigo-500 underline hover:text-indigo-600">查看发布说明</a>'
        + '</div>';
    } else if (d.error) {
      el.innerHTML = '<div class="text-xs text-zinc-500">当前 <span class="font-mono">'
        + escHtml(d.current || '—') + '</span>'
        + '<span class="text-red-400 ml-2">检查失败</span></div>';
    } else {
      el.innerHTML = '<div class="text-xs text-zinc-500">当前 <span class="font-mono">'
        + escHtml(d.current) + '</span>'
        + '<span class="text-zinc-400 ml-2">已是最新</span></div>';
    }
  } catch(e) {
    el.innerHTML = '<div class="text-xs text-red-400">检查失败</div>';
  } finally {
    if (btn) btn.textContent = '检查更新';
  }
}
