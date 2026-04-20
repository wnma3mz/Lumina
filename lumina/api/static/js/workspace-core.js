var _translateLang = 'zh';

// --- HTMX Events ---
document.addEventListener('htmx:afterSettle', function(evt) {
  // If Data Source Pulse was updated, notify the buddy to evolve
  if (evt.detail.target.id === 'digest-sources') {
    var sourceMap = {};
    evt.detail.target.querySelectorAll('.grid > div').forEach(function(el) {
      var name = el.querySelector('span:last-child')?.textContent.trim();
      var isActive = !el.classList.contains('grayscale');
      if (name) sourceMap[name.toLowerCase()] = isActive ? 100 : 0;
    });
    if (window.luminaBuddy) window.luminaBuddy.evolve(sourceMap);
  }
});
var _translateJobId = '';
var _comparePairs = [];
var _compareSync = false;
var _compareFullscreen = false;
var _pendingPdfFile = null;
var _documentBatchJobId = null;
var _documentBatchPollTimer = null;
var _labBatchJobId = null;
var _labBatchPollTimer = null;
var _documentTask = 'translate';
var _documentInputMode = 'text';
var _labInputMode = 'url';
var _currentTaskController = null;

function cancelCurrentTask() {
  if (_currentTaskController) {
    _currentTaskController.abort();
    _currentTaskController = null;
  }
}
var _documentTasks = {
  translate: {
    label: '翻译',
    description: '适合短文、长文和文档文件的中英互译，文本与 txt/md 会直接返回结果，PDF 或目录批量会保留任务追踪。',
    button: '开始翻译',
    outputTitle: '翻译结果',
    textLabel: '输入原文',
    textPlaceholder: '粘贴需要翻译的正文...',
    textHint: '适合网页正文、笔记、文章片段等直接翻译。',
    urlLabel: '输入 PDF 链接',
    urlHint: '适合线上论文、白皮书和可公开访问的 PDF 文档。',
    fileLabel: '上传 / 粘贴文件'
  },
  summarize: {
    label: '总结',
    description: '适合长文本和文档文件的快速提炼，txt/md 会直接总结，PDF 和目录批量会走文档解析链路。',
    button: '生成总结',
    outputTitle: '总结结果',
    textLabel: '粘贴长文本',
    textPlaceholder: '粘贴文章、会议纪要、论文正文或任意长文本...',
    textHint: '适合网页正文、笔记、聊天记录、会议纪要等非 PDF 内容。',
    urlLabel: '输入 PDF 链接',
    urlHint: '适合线上论文、研究报告和长篇 PDF 文档。',
    fileLabel: '上传 / 粘贴文件'
  }
};
var _audioTask = 'audio_live';
var _audioInputMode = 'live';
var _audioTasks = {
  audio_live: {
    label: '实时同传',
    shortLabel: '实时同传',
    description: '捕获系统音频并实时转写翻译。',
    modes: ['live'],
    button: '开启同传'
  }
};

function setAudioTask(task, btn) {
  _audioTask = task;
  var spec = _audioTasks[task];
  if (!spec) return;
  
  document.querySelectorAll('.audio-task-btn').forEach(function(b) {
    var isActive = b === btn;
    b.classList.toggle('bg-indigo-500', isActive);
    b.classList.toggle('text-white', isActive);
    b.classList.toggle('shadow-lg', isActive);
    b.classList.toggle('bg-zinc-100', !isActive);
    b.classList.toggle('dark:bg-zinc-800/50', !isActive);
    b.classList.toggle('text-zinc-500', !isActive);
  });
  
  document.getElementById('audio-task-description').textContent = spec.description;
  document.getElementById('audio-run-btn').textContent = spec.button;
  setAudioInputMode(spec.modes[0]);
}

function setAudioInputMode(mode) {
  _audioInputMode = mode;
  ['live', 'file'].forEach(function(key) {
    var btn = document.getElementById('audio-mode-' + key);
    var block = document.getElementById('audio-input-' + key);
    if (!btn || !block) return;
    var active = key === mode;
    btn.classList.toggle('bg-white', active);
    btn.classList.toggle('dark:bg-zinc-700', active);
    btn.classList.toggle('shadow-sm', active);
    btn.classList.toggle('text-zinc-900', active);
    btn.classList.toggle('dark:text-zinc-100', active);
    btn.classList.toggle('text-zinc-500', !active);
    block.classList.toggle('hidden', !active);
  });
}

function handleAudioDrop(e, el) {
  e.preventDefault();
  el.classList.remove('border-indigo-500', 'bg-indigo-50', 'dark:bg-indigo-900/20');
  var file = e.dataTransfer.files[0];
  if (file) {
    var input = document.getElementById('audio-file-input');
    input.files = e.dataTransfer.files;
    showAudioFilename(input);
  }
}

function showAudioFilename(input) {
  var nameEl = document.getElementById('audio-filename');
  var placeholder = document.getElementById('audio-file-placeholder');
  if (input.files && input.files[0]) {
    nameEl.textContent = input.files[0].name;
    placeholder.classList.add('hidden');
  } else {
    nameEl.textContent = '';
    placeholder.classList.remove('hidden');
  }
}

async function runAudioTask() {
  var result = document.getElementById('audio-result');
  var btn = document.getElementById('audio-run-btn');
  if (!result || !btn) return;

  if (_audioInputMode === 'file') {
    var fileInput = document.getElementById('audio-file-input');
    if (!fileInput.files[0]) {
      result.innerHTML = '<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-6 w-full text-red-600 dark:text-red-400 font-bold flex items-start gap-2"><svg class="w-5 h-5 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg><span class="text-sm">请先选择音频文件</span></div>';
      return;
    }
    setActionButtonBusyState(btn, '转写中…');
    result.innerHTML = '<div class="bg-zinc-50 dark:bg-zinc-800/50 rounded-2xl p-12 w-full flex flex-col items-center justify-center border border-zinc-100 dark:border-zinc-800"><svg class="w-8 h-8 animate-spin text-indigo-500 mb-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg><div class="text-sm font-bold text-zinc-500">正在转写音频，请稍候…</div></div>';
    
    try {
        var fd = new FormData();
        fd.append('file', fileInput.files[0]);
        var res = await fetch('/v1/audio/transcriptions', { method: 'POST', body: fd });
        if (!res.ok) throw new Error(((await res.json().catch(function() { return {}; })).detail) || res.statusText);
        var data = await res.json();
        if (window.luminaBuddy) window.luminaBuddy.setState('success');
        await renderRichTextResult('audio-result', data.text || '', fileInput.files[0].name + ' · 语音转写');
    } catch (e) {
        if (window.luminaBuddy) window.luminaBuddy.setState('error');
        result.innerHTML = '<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-6 w-full text-red-600 dark:text-red-400 font-bold flex items-start gap-2"><svg class="w-5 h-5 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg><span class="text-sm">错误：' + escapeHtml(e.message) + '</span></div>';
    } finally {
        btn.disabled = false;
        btn.textContent = '开始转写';
    }
    return;
  }
  
  if (_audioTask === 'audio_live') {
    if (btn.textContent === '停止同传') {
        if (_currentTaskController) _currentTaskController.abort();
        return;
    }
    
    setActionButtonBusyState(btn, '环境检查中…');
    try {
      var checkRes = await fetch('/v1/audio/check_env');
      if (!checkRes.ok) {
        var errDetail = await checkRes.json().catch(function() { return {}; });
        var msg = errDetail.detail || checkRes.statusText;
        result.innerHTML = '<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-6 w-full text-red-600 dark:text-red-400 font-bold flex flex-col gap-3"><div class="flex items-start gap-2"><svg class="w-5 h-5 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg><span class="text-sm">环境依赖缺失：' + escapeHtml(msg) + '</span></div><div class="text-xs bg-white/50 dark:bg-black/10 p-4 rounded-xl font-normal leading-relaxed text-zinc-800 dark:text-zinc-200 border border-red-100 dark:border-red-900/30"><strong>如何修复？</strong><br>由于系统限制，捕获系统声音需要第三方虚拟声卡作为桥梁。<br><br>1. 下载安装 <strong>BlackHole 2ch</strong>: <a href="https://existential.audio/blackhole/" target="_blank" class="underline text-blue-500 hover:text-blue-600">官网链接</a> 或终端执行 <code class="bg-black/5 dark:bg-white/10 px-1.5 py-0.5 rounded">brew install blackhole-2ch</code><br>2. 在 macOS「音频 MIDI 设置」中创建一个「多输出设备」，勾选你当前的耳机/扬声器和 BlackHole。<br>3. 将该「多输出设备」设为系统的默认声音输出。<br>4. 重试开启同传。</div></div>';
        restoreActionButtonState(btn, '开启同传');
        return;
      }
    } catch(e) {
      result.innerHTML = '<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-6 w-full text-red-600 dark:text-red-400 font-bold flex items-start gap-2"><svg class="w-5 h-5 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg><span class="text-sm">环境检查失败：' + escapeHtml(e.message) + '</span></div>';
      restoreActionButtonState(btn, '开启同传');
      return;
    }
    
    result.innerHTML = '<div class="flex flex-col gap-4 w-full" id="live-subtitles-container"></div>';
    var container = document.getElementById('live-subtitles-container');
    var source = new EventSource('/v1/audio/live?lang_out=zh');
    
    _currentTaskController = { abort: function() { source.close(); btn.textContent = '开启同传'; if (window.luminaBuddy) window.luminaBuddy.setState('idle'); } };
    
    source.onmessage = function(event) {
      var data = JSON.parse(event.data);
      var item = document.createElement('div');
      item.className = 'bg-white dark:bg-zinc-800 p-4 rounded-2xl shadow-sm border border-zinc-100 dark:border-zinc-700 animate-in fade-in slide-in-from-bottom-2 duration-500';
      item.innerHTML = '<div class="text-[10px] font-bold text-indigo-500 mb-1 uppercase tracking-widest">Transcription</div>' +
                      '<div class="text-zinc-500 text-xs mb-2 italic">"' + escapeHtml(data.raw) + '"</div>' +
                      '<div class="text-sm font-medium text-zinc-900 dark:text-zinc-100 leading-relaxed">' + escapeHtml(data.translated) + '</div>';
      container.appendChild(item);
      scrollResultIntoView(item);
      if (window.luminaBuddy) window.luminaBuddy.setState('working');
    };
    
    source.onerror = function() {
      source.close();
      if (window.luminaBuddy) window.luminaBuddy.setState('error');
      btn.textContent = '开启同传';
    };

    btn.textContent = '停止同传';
    if (window.luminaBuddy) window.luminaBuddy.setState('working');
    return;
  }
}

var _labTasks = {};
getImageTaskDefs().forEach(function(item) {
  var modes = Array.isArray(item.modes) ? item.modes.slice() : [];
  if (!modes.includes('directory')) modes.push('directory');
  _labTasks[item.key] = {
    label: item.label,
    shortLabel: item.short_label,
    description: item.description,
    modes: modes,
    fileAccept: item.file_accept,
    fileLabel: item.file_label,
    button: item.button,
    promptText: item.prompt_text
  };
});
var _labTask = Object.keys(_labTasks)[0] || 'image_ocr';

function normalizeUrl(url) {
  return url.replace(/arxiv\.org\/abs\/([0-9]+\.[0-9]+)/g, 'arxiv.org/pdf/$1');
}

function fileExtension(name) {
  var parts = String(name || '').toLowerCase().split('.');
  return parts.length > 1 ? parts.pop() : '';
}

function isPdfFile(file) {
  return !!file && (fileExtension(file.name) === 'pdf' || (file.type || '') === 'application/pdf');
}

function isTextDocumentFile(file) {
  var ext = fileExtension(file && file.name);
  return !!file && (['txt', 'md', 'markdown'].includes(ext) || (file.type || '').startsWith('text/'));
}

function isSupportedDocumentFile(file) {
  return isPdfFile(file) || isTextDocumentFile(file);
}

async function readDocumentTextFile(file) {
  var text = await file.text();
  return (text || '').trim();
}

function readImageAsDataUrl(file) {
  return new Promise(function(resolve, reject) {
    var reader = new FileReader();
    reader.onload = function() { resolve(String(reader.result || '')); };
    reader.onerror = function() { reject(new Error('图片读取失败')); };
    reader.readAsDataURL(file);
  });
}

function sleep(ms) {
  return new Promise(function(r) { setTimeout(r, ms); });
}

function escapeHtml(text) {
  return String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function scrollResultIntoView(targetEl) {
  if (!targetEl || window.innerWidth >= 768) return;
  targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function setActionButtonBusyState(btn, busyText) {
  if (!btn) return;
  if (!btn.dataset.idleText) btn.dataset.idleText = btn.textContent;
  btn.disabled = true;
  btn.setAttribute('aria-busy', 'true');
  btn.textContent = busyText;
}

function restoreActionButtonState(btn, fallbackText) {
  if (!btn) return;
  btn.disabled = false;
  btn.setAttribute('aria-busy', 'false');
  btn.textContent = btn.dataset.idleText || fallbackText || btn.textContent;
}

async function renderRichTextResult(targetId, text, meta, signal) {
  var el = document.getElementById(targetId);
  if (!el) return;
  var contentHtml = '<div class="whitespace-pre-wrap text-sm leading-7 text-zinc-700 dark:text-zinc-200">' + escapeHtml(text) + '</div>';
  try {
    var res = await fetch('/v1/render_markdown', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: signal,
      body: JSON.stringify({ text: text || '' })
    });
    if (res.ok) {
      var data = await res.json();
      contentHtml = '<div class="prose prose-sm max-w-none dark:prose-invert prose-pre:rounded-xl prose-pre:bg-zinc-900 prose-code:before:content-none prose-code:after:content-none">' + (data.html || '') + '</div>';
    }
  } catch (e) {
    if (e && e.name === 'AbortError') throw e;
  }
  var copyBtnHtml = '<button onclick="navigator.clipboard.writeText(decodeURIComponent(\'' + encodeURIComponent(text) + '\')); this.textContent=\'已复制\'; setTimeout(()=>this.textContent=\'📋 复制\', 2000)" class="px-2 py-1 text-[10px] font-bold uppercase tracking-widest text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200 bg-white dark:bg-zinc-800 rounded shadow-sm border border-zinc-200 dark:border-zinc-700 transition-all flex items-center gap-1 shrink-0">📋 复制</button>';
  var headerHtml = '<div class="flex items-start justify-between gap-4 mb-4">' + 
    (meta ? '<div class="text-[11px] font-bold uppercase tracking-widest text-zinc-400 mt-1">' + escapeHtml(meta) + '</div>' : '<div></div>') + 
    copyBtnHtml + 
    '</div>';
  el.innerHTML = '<div class="w-full bg-zinc-50 dark:bg-zinc-800/50 border border-zinc-100 dark:border-zinc-800 rounded-2xl p-5">' + headerHtml + contentHtml + '</div>';
}

function clearBatchPoll(kind) {
  if (kind === 'document' && _documentBatchPollTimer) {
    clearTimeout(_documentBatchPollTimer);
    _documentBatchPollTimer = null;
  }
  if (kind === 'image' && _labBatchPollTimer) {
    clearTimeout(_labBatchPollTimer);
    _labBatchPollTimer = null;
  }
}

function formatBatchTask(task, targetLanguage) {
  if (task === 'translate') return targetLanguage === 'en' ? '批量翻译 · 中 -> 英' : '批量翻译 · 英 -> 中';
  if (task === 'summarize') return '批量总结';
  if (task === 'image_ocr') return '批量 OCR';
  if (task === 'image_caption') return '批量 Caption';
  return task;
}

function renderBatchJob(targetId, job) {
  var el = document.getElementById(targetId);
  if (!el || !job) return;
  var percent = job.total ? Math.min(100, Math.round((job.completed / job.total) * 100)) : 0;
  var tone = job.status === 'error' ? 'from-red-500 to-rose-500' : (job.status === 'done' ? 'from-emerald-500 to-teal-500' : 'from-indigo-500 to-violet-500');
  var current = job.current_item ? '<div class="text-xs text-zinc-500 dark:text-zinc-400 mt-2">当前文件：' + escapeHtml(job.current_item) + '</div>' : '';
  var error = job.error ? '<div class="mt-4 text-sm font-bold text-red-600 dark:text-red-400">' + escapeHtml(job.error) + '</div>' : '';
  var items = (job.items || []).map(function(item) {
    var badgeCls = item.status === 'done'
      ? 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/15 dark:text-emerald-300'
      : item.status === 'error'
        ? 'bg-red-50 text-red-600 dark:bg-red-500/15 dark:text-red-300'
        : item.status === 'running'
          ? 'bg-indigo-50 text-indigo-600 dark:bg-indigo-500/15 dark:text-indigo-300'
          : 'bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-300';
    var outputs = (item.output_paths || []).map(function(path) {
      return '<div class="text-xs text-zinc-500 dark:text-zinc-400 break-all">' + escapeHtml(path) + '</div>';
    }).join('');
    var preview = item.preview ? '<div class="mt-3 text-sm leading-6 text-zinc-700 dark:text-zinc-200">' + escapeHtml(item.preview) + '</div>' : '';
    var itemError = item.error ? '<div class="mt-3 text-sm font-bold text-red-600 dark:text-red-400">' + escapeHtml(item.error) + '</div>' : '';
    return '<div class="rounded-2xl border border-zinc-100 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-800/40 p-4">' +
      '<div class="flex items-start justify-between gap-3">' +
        '<div class="min-w-0">' +
          '<div class="text-sm font-bold text-zinc-900 dark:text-zinc-100 break-all">' + escapeHtml(item.rel_path) + '</div>' +
          '<div class="text-xs text-zinc-400 mt-1 break-all">' + escapeHtml(item.path) + '</div>' +
        '</div>' +
        '<span class="shrink-0 px-2.5 py-1 rounded-full text-[10px] font-bold uppercase tracking-widest ' + badgeCls + '">' + escapeHtml(item.status) + '</span>' +
      '</div>' +
      preview + itemError +
      (outputs ? '<div class="mt-3 pt-3 border-t border-zinc-200/70 dark:border-zinc-700/70 space-y-1">' + outputs + '</div>' : '') +
    '</div>';
  }).join('');

  el.innerHTML = '<div class="w-full bg-zinc-50 dark:bg-zinc-800/50 border border-zinc-100 dark:border-zinc-800 rounded-2xl p-5">' +
    '<div class="flex items-start justify-between gap-4">' +
      '<div>' +
        '<div class="text-[11px] font-bold uppercase tracking-widest text-zinc-400">' + escapeHtml(formatBatchTask(job.task, job.target_language)) + '</div>' +
        '<div class="text-xl font-black text-zinc-900 dark:text-zinc-100 mt-2">' + job.completed + ' / ' + job.total + '</div>' +
        '<div class="text-sm text-zinc-500 dark:text-zinc-400 mt-2">成功 ' + job.succeeded + '，失败 ' + job.failed + '，状态 ' + escapeHtml(job.status) + '</div>' +
        current +
      '</div>' +
      '<div class="text-right">' +
        '<div class="text-[10px] font-bold uppercase tracking-widest text-zinc-400">Output</div>' +
        '<div class="text-xs text-zinc-500 dark:text-zinc-400 mt-2 break-all max-w-[220px]">' + escapeHtml(job.output_dir) + '</div>' +
      '</div>' +
    '</div>' +
    '<div class="mt-5 h-3 rounded-full bg-zinc-200 dark:bg-zinc-700 overflow-hidden"><div class="h-full rounded-full bg-gradient-to-r ' + tone + '" style="width:' + percent + '%"></div></div>' +
    error +
    '<div class="mt-6 space-y-3 max-h-[520px] overflow-y-auto pr-1">' + items + '</div>' +
  '</div>';
}

async function pollBatchJob(kind, targetId, jobId) {
  try {
    var res = await fetch('/v1/batch/' + encodeURIComponent(jobId));
    if (!res.ok) throw new Error((await res.json().catch(function() { return {}; })).detail || res.statusText);
    var job = await res.json();
    renderBatchJob(targetId, job);
    if (job.status === 'queued' || job.status === 'running') {
      clearBatchPoll(kind);
      var timer = setTimeout(function() { pollBatchJob(kind, targetId, jobId); }, 2000);
      if (kind === 'document') _documentBatchPollTimer = timer;
      else _labBatchPollTimer = timer;
    } else if (job.status === 'done') {
      if (window.luminaBuddy) window.luminaBuddy.setState('success');
    } else if (job.status === 'error') {
      if (window.luminaBuddy) window.luminaBuddy.setState('error');
    }
  } catch (e) {
    if (window.luminaBuddy) window.luminaBuddy.setState('error');
    var el = document.getElementById(targetId);
    if (el) {
      el.innerHTML = '<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-6 w-full text-red-600 dark:text-red-400 font-bold flex items-start gap-2"><svg class="w-5 h-5 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg><span class="text-sm">批处理状态获取失败：' + escapeHtml(e.message) + '</span></div>';
    }
  }
}

function clearFileSelection(inputId, filenameId, previewId) {
  var input = document.getElementById(inputId);
  if (input) input.value = '';
  showFilename({files: []}, filenameId, previewId);
}

function showFilename(input, targetId, previewId) {
  var el = document.getElementById(targetId);
  var file = input.files[0];
  if (el) el.textContent = file ? file.name : '';

  if (previewId) {
    var preview = document.getElementById(previewId);
    var placeholder = document.getElementById(previewId.replace('preview', 'placeholder'));
    var clearBtn = document.getElementById(previewId.replace('preview', 'clear'));
    if (preview) {
      if (file && file.type.startsWith('image/')) {
        var reader = new FileReader();
        reader.onload = function(e) {
          preview.src = e.target.result;
          preview.classList.remove('hidden');
          if (placeholder) placeholder.classList.add('hidden');
          if (clearBtn) { clearBtn.classList.remove('hidden'); clearBtn.classList.add('flex'); }
        };
        reader.readAsDataURL(file);
      } else {
        preview.src = '';
        preview.classList.add('hidden');
        if (placeholder) placeholder.classList.remove('hidden');
        if (clearBtn) { clearBtn.classList.add('hidden'); clearBtn.classList.remove('flex'); }
      }
    }
  } else if (file) {
    var clearBtnDoc = document.getElementById(targetId.replace('filename', 'clear'));
    if (clearBtnDoc) { clearBtnDoc.classList.remove('hidden'); clearBtnDoc.classList.add('flex'); }
  } else {
    var clearBtnDoc = document.getElementById(targetId.replace('filename', 'clear'));
    if (clearBtnDoc) { clearBtnDoc.classList.add('hidden'); clearBtnDoc.classList.remove('flex'); }
  }
}

function showUrlPreview(url, previewId) {
  var container = document.getElementById(previewId + '-container');
  var preview = document.getElementById(previewId);
  if (!container || !preview) return;
  url = (url || '').trim();
  if (url && (url.startsWith('http://') || url.startsWith('https://') || url.startsWith('data:image/'))) {
    preview.src = url;
    container.classList.remove('hidden');
  } else {
    preview.src = '';
    container.classList.add('hidden');
  }
}

function handleDropZone(event, fileInputId, filenameId) {
  var f = event.dataTransfer.files[0];
  var input = document.getElementById(fileInputId);
  if (!f || !input) return;
  var allowDocumentFiles = fileInputId === 'document-file';
  if ((allowDocumentFiles && !isSupportedDocumentFile(f)) || (!allowDocumentFiles && !isPdfFile(f))) return;
  var dt = new DataTransfer();
  dt.items.add(f);
  input.files = dt.files;
  showFilename(input, filenameId);
}

function handleLabDrop(event, target) {
  event.preventDefault();
  target.classList.remove('border-indigo-500', 'bg-indigo-50', 'dark:bg-indigo-900/20');
  var file = event.dataTransfer.files[0];
  var input = document.getElementById('lab-file-input');
  if (!file || !input) return;
  if (!(file.type || '').startsWith('image/')) return;
  var dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  showFilename(input, 'lab-filename', 'lab-file-preview');
}

function setDigestSeg(seg) {
  var btns = document.querySelectorAll('#digest-seg-ctrl button');
  btns.forEach(function(b) {
    b.classList.remove('bg-white', 'dark:bg-zinc-700', 'shadow-sm', 'text-zinc-900', 'dark:text-zinc-100');
    b.classList.add('text-zinc-500');
  });
  var active = document.getElementById('seg-' + seg);
  if (active) {
    active.classList.remove('text-zinc-500');
    active.classList.add('bg-white', 'dark:bg-zinc-700', 'shadow-sm', 'text-zinc-900', 'dark:text-zinc-100');
    var url = active.getAttribute('hx-get');
    if (url) {
      document.getElementById('digest-content').setAttribute('hx-get', url);
    }
  }
}

function setTranslateLang(lang) {
  _translateLang = lang;
  document.getElementById('lang-zh').classList.toggle('selected', lang === 'zh');
  document.getElementById('lang-en').classList.toggle('selected', lang === 'en');
}

function setDocumentTask(task, btn) {
  if (!_documentTasks[task]) return;
  _documentTask = task;
  document.querySelectorAll('#document-task-group .document-task-btn').forEach(function(el) {
    el.classList.remove('bg-indigo-500', 'text-white', 'shadow-lg', 'shadow-indigo-500/20');
    el.classList.add('bg-zinc-100', 'dark:bg-zinc-800/50', 'text-zinc-500');
  });
  if (btn) {
    btn.classList.remove('bg-zinc-100', 'dark:bg-zinc-800/50', 'text-zinc-500');
    btn.classList.add('bg-indigo-500', 'text-white', 'shadow-lg', 'shadow-indigo-500/20');
  }
  var spec = _documentTasks[task];
  var description = document.getElementById('document-task-description');
  var outputTitle = document.getElementById('document-output-title');
  var textLabel = document.getElementById('document-text-label');
  var textInput = document.getElementById('document-text');
  var textHint = document.getElementById('document-text-hint');
  var urlLabel = document.getElementById('document-url-label');
  var urlHint = document.getElementById('document-url-hint');
  var fileLabel = document.getElementById('document-file-label');
  var runBtn = document.getElementById('document-run-btn');
  var translateOptions = document.getElementById('document-translate-options');
  if (description) description.textContent = spec.description;
  if (outputTitle) outputTitle.textContent = spec.outputTitle;
  if (textLabel) textLabel.textContent = spec.textLabel;
  if (textInput) textInput.placeholder = spec.textPlaceholder;
  if (textHint) textHint.textContent = spec.textHint;
  if (urlLabel) urlLabel.textContent = spec.urlLabel;
  if (urlHint) urlHint.textContent = spec.urlHint;
  if (fileLabel) fileLabel.textContent = spec.fileLabel;
  if (runBtn) runBtn.textContent = spec.button;
  if (translateOptions) translateOptions.classList.toggle('hidden', task !== 'translate');
}

function setDocumentInputMode(mode) {
  _documentInputMode = mode;
  ['text', 'url', 'file', 'directory'].forEach(function(key) {
    var btn = document.getElementById('document-mode-' + key);
    var block = document.getElementById('document-input-' + key);
    var active = key === mode;
    if (btn) {
      btn.classList.toggle('bg-white', active);
      btn.classList.toggle('dark:bg-zinc-700', active);
      btn.classList.toggle('shadow-sm', active);
      btn.classList.toggle('text-zinc-900', active);
      btn.classList.toggle('dark:text-zinc-100', active);
      btn.classList.toggle('text-zinc-500', !active);
    }
    if (block) block.classList.toggle('hidden', !active);
  });
}

function setLabTask(task, btn) {
  if (!_labTasks[task] || !getEnabledLabTasks().includes(task)) return;
  _labTask = task;
  document.querySelectorAll('#lab-task-group .lab-task-btn').forEach(function(el) {
    el.classList.remove('bg-indigo-500', 'text-white', 'shadow-lg', 'shadow-indigo-500/20');
    el.classList.add('bg-zinc-100', 'dark:bg-zinc-800/50', 'text-zinc-500');
  });
  if (btn) {
    btn.classList.remove('bg-zinc-100', 'dark:bg-zinc-800/50', 'text-zinc-500');
    btn.classList.add('bg-indigo-500', 'text-white', 'shadow-lg', 'shadow-indigo-500/20');
  }
  var spec = _labTasks[task];
  var desc = document.getElementById('lab-task-description');
  if (desc) desc.textContent = spec.description;
  var runBtn = document.getElementById('lab-run-btn');
  if (runBtn) runBtn.textContent = spec.button;
  var fileInput = document.getElementById('lab-file-input');
  if (fileInput) fileInput.accept = spec.fileAccept || '';
  var fileLabel = document.getElementById('lab-file-label');
  if (fileLabel) fileLabel.textContent = spec.fileLabel || '点击选择或拖入文件';
  var nextMode = spec.modes.includes(_labInputMode) ? _labInputMode : spec.modes[0];
  setLabInputMode(nextMode);
}

function applyLabTaskAvailability() {
  var enabled = getEnabledLabTasks();
  document.querySelectorAll('#lab-task-group .lab-task-btn').forEach(function(btn) {
    var task = btn.dataset.task;
    btn.classList.toggle('hidden', !enabled.includes(task));
  });
  if (!enabled.includes(_labTask)) {
    var firstTask = enabled[0] || 'image_ocr';
    setLabTask(firstTask, document.querySelector('#lab-task-group [data-task="' + firstTask + '"]'));
  }
}

function setLabInputMode(mode) {
  var spec = _labTasks[_labTask] || _labTasks.image_ocr;
  if (!spec.modes.includes(mode)) mode = spec.modes[0];
  _labInputMode = mode;
  ['text', 'url', 'file', 'directory', 'live'].forEach(function(key) {
    var btn = document.getElementById('lab-mode-' + key);
    var block = document.getElementById('lab-input-' + key);
    var enabled = spec.modes.includes(key);
    var active = enabled && key === mode;
    if (btn) {
      btn.disabled = !enabled;
      btn.classList.toggle('hidden', !enabled);
      btn.classList.toggle('bg-white', active);
      btn.classList.toggle('dark:bg-zinc-700', active);
      btn.classList.toggle('shadow-sm', active);
      btn.classList.toggle('text-zinc-900', active);
      btn.classList.toggle('dark:text-zinc-100', active);
      btn.classList.toggle('text-zinc-500', !active);
    }
    if (block) block.classList.toggle('hidden', !active);
  });
}

async function runLabTask() {
  var spec = _labTasks[_labTask] || _labTasks.image_ocr;
  var result = document.getElementById('lab-result');
  var btn = document.getElementById('lab-run-btn');
  if (!result || !btn) return;

  if (window.luminaBuddy) window.luminaBuddy.setState('working');
  if (_labInputMode === 'directory') {
    var inputDir = (document.getElementById('lab-directory-input').value || '').trim();
    var outputDir = (document.getElementById('lab-output-dir').value || '').trim();
    if (!inputDir) {
      result.innerHTML = '<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-6 w-full text-red-600 dark:text-red-400 font-bold flex items-start gap-2"><svg class="w-5 h-5 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg><span class="text-sm">请先输入图片目录路径</span></div>';
      return;
    }
    btn.disabled = true;
    btn.textContent = '提交中…';
    result.innerHTML = '<div class="bg-zinc-50 dark:bg-zinc-800/50 rounded-2xl p-12 w-full flex flex-col items-center justify-center border border-zinc-100 dark:border-zinc-800"><svg class="w-8 h-8 animate-spin text-indigo-500 mb-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg><div class="text-sm font-bold text-zinc-500">正在提交批处理任务…</div></div>';
    try {
      clearBatchPoll('image');
      var batchRes = await fetch('/v1/batch/image', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          input_dir: inputDir,
          output_dir: outputDir || null,
          task: _labTask
        })
      });
      if (!batchRes.ok) throw new Error(((await batchRes.json().catch(function() { return {}; })).detail) || batchRes.statusText);
      var batchData = await batchRes.json();
      _labBatchJobId = batchData.job_id;
      renderBatchJob('lab-result', batchData);
      pollBatchJob('image', 'lab-result', batchData.job_id);
      return;
    } catch (e) {
      result.innerHTML = '<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-6 w-full text-red-600 dark:text-red-400 font-bold flex items-start gap-2"><svg class="w-5 h-5 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg><span class="text-sm">错误：' + escapeHtml(e.message) + '</span></div>';
      return;
    } finally {
      btn.disabled = false;
      btn.textContent = spec.button;
    }
  }

  setActionButtonBusyState(btn, '处理中…');
  result.innerHTML = '<div class="bg-zinc-50 dark:bg-zinc-800/50 rounded-2xl p-12 w-full flex flex-col items-center justify-center border border-zinc-100 dark:border-zinc-800 relative"><button onclick="cancelCurrentTask()" class="absolute top-4 right-4 px-3 py-1.5 text-xs font-bold text-red-500 hover:text-red-600 bg-red-50 hover:bg-red-100 dark:bg-red-500/10 dark:hover:bg-red-500/20 rounded-lg transition-all">取消</button><svg class="w-8 h-8 animate-spin text-indigo-500 mb-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg><div class="text-sm font-bold text-zinc-500">处理中，请稍候…</div></div>';
  scrollResultIntoView(result);

  cancelCurrentTask();
  _currentTaskController = new AbortController();

  try {
    if (_labTask === 'image_ocr' || _labTask === 'image_caption') {
      var prompts = getImagePrompts();
      var systemPrompt = prompts[_labTask] || '';
      var imageRef;
      if (_labInputMode === 'file') {
        var mediaFile = document.getElementById('lab-file-input');
        if (!mediaFile.files[0]) throw new Error('请先选择图片文件');
        imageRef = await readImageAsDataUrl(mediaFile.files[0]);
      } else {
        var imageUrl = (document.getElementById('lab-url-input').value || '').trim();
        if (!imageUrl) throw new Error('请先输入图片链接');
        imageRef = imageUrl;
      }
      var mediaRes = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: _currentTaskController.signal,
        body: JSON.stringify({
          model: 'lumina',
          messages: [
            { role: 'system', content: systemPrompt },
            {
              role: 'user',
              content: [
                { type: 'text', text: spec.promptText || '请描述这张图片。' },
                { type: 'image_url', image_url: { url: imageRef } }
              ]
            }
          ]
        })
      });
      if (!mediaRes.ok) throw new Error(((await mediaRes.json().catch(function() { return {}; })).detail) || mediaRes.statusText);
      var mediaData = await mediaRes.json();
      var mediaText = (((mediaData || {}).choices || [])[0] || {}).message;
      if (window.luminaBuddy) window.luminaBuddy.setState('success');
      await renderRichTextResult('lab-result', (mediaText && mediaText.content) || '', spec.label + ' · Chat', _currentTaskController.signal);
      return;
    }

    throw new Error('暂不支持的图片任务');
  } catch (e) {
    if (window.luminaBuddy) window.luminaBuddy.setState('error');
    if (e.name === 'AbortError') {
      result.innerHTML = '<div class="bg-zinc-50 dark:bg-zinc-800/50 border border-zinc-200 dark:border-zinc-800 rounded-2xl p-6 w-full text-zinc-500 dark:text-zinc-400 font-bold flex items-start gap-2"><span class="text-sm">已取消处理。</span></div>';
    } else {
      result.innerHTML = '<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-6 w-full text-red-600 dark:text-red-400 font-bold flex items-start gap-2"><svg class="w-5 h-5 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg><span class="text-sm">错误：' + escapeHtml(e.message) + '</span></div>';
    }
  } finally {
    _currentTaskController = null;
    restoreActionButtonState(btn, spec.button);
  }
}

async function startDocumentTask() {
  var fileInput = document.getElementById('document-file');
  if (window.luminaBuddy) window.luminaBuddy.setState('working');
  var urlInput = document.getElementById('document-url');
  var textInput = document.getElementById('document-text');
  var dirInput = document.getElementById('document-directory');
  var outputDirInput = document.getElementById('document-output-dir');
  var resultDiv = document.getElementById('document-result');
  var btn = document.getElementById('document-run-btn');
  var spec = _documentTasks[_documentTask];
  var selectedFile = fileInput && fileInput.files ? fileInput.files[0] : null;

  var text = textInput ? (textInput.value || '').trim() : '';
  var url = urlInput ? normalizeUrl((urlInput.value || '').trim()) : '';
  var inputDir = dirInput ? (dirInput.value || '').trim() : '';
  var outputDir = outputDirInput ? (outputDirInput.value || '').trim() : '';

  if ((_documentInputMode === 'text' && !text) || (_documentInputMode === 'url' && !url) || (_documentInputMode === 'file' && !selectedFile) || (_documentInputMode === 'directory' && !inputDir)) {
    var hint = _documentInputMode === 'text'
      ? '请先粘贴文本'
      : (_documentInputMode === 'url'
        ? '请先输入 PDF 链接'
        : (_documentInputMode === 'file' ? '请先上传文件' : '请先输入目录路径'));
    resultDiv.innerHTML = '<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-6 w-full text-red-600 dark:text-red-400 font-bold flex items-start gap-2"><svg class="w-5 h-5 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg><span class="text-sm">' + hint + '</span></div>';
    return;
  }

  if (_documentInputMode === 'directory') {
    btn.disabled = true;
    btn.textContent = '提交中…';
    if (window.luminaBuddy) window.luminaBuddy.setState('working');
    resultDiv.innerHTML = '<div class="bg-zinc-50 dark:bg-zinc-800/50 rounded-2xl p-12 w-full flex flex-col items-center justify-center border border-zinc-100 dark:border-zinc-800"><svg class="w-8 h-8 animate-spin text-indigo-500 mb-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg><div class="text-sm font-bold text-zinc-500">正在提交批处理任务…</div></div>';
    try {
      clearBatchPoll('document');
      var batchRes = await fetch('/v1/batch/document', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          input_dir: inputDir,
          output_dir: outputDir || null,
          task: _documentTask,
          target_language: _translateLang
        })
      });
      if (!batchRes.ok) {
        var batchErr = await batchRes.json().catch(function() { return { detail: batchRes.statusText }; });
        throw new Error(batchErr.detail || batchRes.statusText);
      }
      var batchData = await batchRes.json();
      _documentBatchJobId = batchData.job_id;
      renderBatchJob('document-result', batchData);
      pollBatchJob('document', 'document-result', batchData.job_id);
      return;
    } catch (e) {
      resultDiv.innerHTML = '<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-6 w-full text-red-600 dark:text-red-400 font-bold flex items-start gap-2"><svg class="w-5 h-5 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg><span class="text-sm">错误：' + escapeHtml(e.message) + '</span></div>';
      return;
    } finally {
      btn.disabled = false;
      btn.textContent = spec.button;
    }
  }

  setActionButtonBusyState(btn, _documentTask === 'translate' ? (_documentInputMode === 'text' ? '翻译中…' : '处理中…') : '总结中…');
  resultDiv.innerHTML = '<div class="bg-zinc-50 dark:bg-zinc-800/50 rounded-2xl p-12 w-full flex flex-col items-center justify-center border border-zinc-100 dark:border-zinc-800 relative"><button onclick="cancelCurrentTask()" class="absolute top-4 right-4 px-3 py-1.5 text-xs font-bold text-red-500 hover:text-red-600 bg-red-50 hover:bg-red-100 dark:bg-red-500/10 dark:hover:bg-red-500/20 rounded-lg transition-all">取消</button><svg class="w-8 h-8 animate-spin text-indigo-500 mb-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg><div class="text-sm font-bold text-zinc-500">' + (_documentTask === 'translate' ? '处理中，请稍候…' : '生成中，请稍候…') + '</div></div>';
  scrollResultIntoView(resultDiv);

  cancelCurrentTask();
  _currentTaskController = new AbortController();

  try {
    var res;
    if (_documentTask === 'translate') {
      if (_documentInputMode === 'text') {
        res = await fetch('/v1/translate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: _currentTaskController.signal,
          body: JSON.stringify({ text: text, target_language: _translateLang })
        });
        if (!res.ok) {
          var translateTextErr = await res.json().catch(function() { return { detail: res.statusText }; });
          throw new Error(translateTextErr.detail || res.statusText);
        }
        var translateTextData = await res.json();
        if (window.luminaBuddy) window.luminaBuddy.setState('success');
        await renderRichTextResult('document-result', translateTextData.text || '', _translateLang === 'zh' ? '文本翻译 · 英 -> 中' : '文本翻译 · 中 -> 英', _currentTaskController.signal);
        return;
      }

      if (_documentInputMode === 'file') {
        if (isTextDocumentFile(selectedFile)) {
          var translateFileText = await readDocumentTextFile(selectedFile);
          if (!translateFileText) throw new Error('文件内容为空');
          res = await fetch('/v1/translate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            signal: _currentTaskController.signal,
            body: JSON.stringify({ text: translateFileText, target_language: _translateLang })
          });
          if (!res.ok) {
            var translateFileErr = await res.json().catch(function() { return { detail: res.statusText }; });
            throw new Error(translateFileErr.detail || res.statusText);
          }
          var translateFileData = await res.json();
          if (window.luminaBuddy) window.luminaBuddy.setState('success');
          await renderRichTextResult('document-result', translateFileData.text || '', (selectedFile.name || '文本文件') + ' · 文件翻译', _currentTaskController.signal);
          return;
        }
        if (!isPdfFile(selectedFile)) throw new Error('文件模式目前支持 PDF / TXT / MD');
        var translateFd = new FormData();
        translateFd.append('file', selectedFile);
        translateFd.append('lang_out', _translateLang);
        res = await fetch('/v1/pdf/upload', { method: 'POST', signal: _currentTaskController.signal, body: translateFd });
      } else {
        res = await fetch('/v1/pdf/url', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: _currentTaskController.signal,
          body: JSON.stringify({ url: url, action: 'translate', lang_out: _translateLang })
        });
      }
      if (!res.ok) {
        var translateErr = await res.json().catch(function() { return { detail: res.statusText }; });
        throw new Error(translateErr.detail || res.statusText);
      }
      var translateData = await res.json();
      _translateJobId = translateData.job_id;
      _comparePairs = [];
      resultDiv.innerHTML = '<div hx-get="/fragments/pdf/status/' + translateData.job_id + '" hx-trigger="every 2s" hx-swap="outerHTML" class="w-full"><div class="bg-zinc-50 dark:bg-zinc-800/50 rounded-2xl p-6 w-full border border-zinc-100 dark:border-zinc-800"><div class="text-sm font-bold text-zinc-500 mb-4 flex justify-between items-center"><span>正在翻译，可能需要几分钟…</span><span>30%</span></div><div class="w-full bg-zinc-200 dark:bg-zinc-700 rounded-full h-2.5 overflow-hidden"><div class="bg-indigo-600 h-2.5 rounded-full transition-all duration-500 ease-out" style="width: 30%"></div></div></div></div>';
      if (window.htmx) htmx.process(resultDiv);
      return;
    }

    if (_documentInputMode === 'text') {
      res = await fetch('/v1/summarize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: _currentTaskController.signal,
        body: JSON.stringify({ text: text })
      });
      if (!res.ok) {
        var summarizeTextErr = await res.json().catch(function() { return { detail: res.statusText }; });
        throw new Error(summarizeTextErr.detail || res.statusText);
      }
      var summarizeTextData = await res.json();
      if (window.luminaBuddy) window.luminaBuddy.setState('success');
      await renderRichTextResult('document-result', summarizeTextData.text || '', '文本摘要', _currentTaskController.signal);
      return;
    }

    var endpoint;
    var body;
    var headers = {};
    if (_documentInputMode === 'file') {
      if (isTextDocumentFile(selectedFile)) {
        var summarizeFileText = await readDocumentTextFile(selectedFile);
        if (!summarizeFileText) throw new Error('文件内容为空');
        res = await fetch('/v1/summarize', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: _currentTaskController.signal,
          body: JSON.stringify({ text: summarizeFileText })
        });
        if (!res.ok) {
          var summarizeFileErr = await res.json().catch(function() { return { detail: res.statusText }; });
          throw new Error(summarizeFileErr.detail || res.statusText);
        }
        var summarizeFileData = await res.json();
        if (window.luminaBuddy) window.luminaBuddy.setState('success');
        await renderRichTextResult('document-result', summarizeFileData.text || '', (selectedFile.name || '文本文件') + ' · 文件总结', _currentTaskController.signal);
        return;
      }
      if (!isPdfFile(selectedFile)) throw new Error('文件模式目前支持 PDF / TXT / MD');
      var summarizeFd = new FormData();
      summarizeFd.append('file', selectedFile);
      endpoint = '/v1/pdf/summarize_sync';
      body = summarizeFd;
    } else {
      endpoint = '/v1/pdf/url_summarize_sync';
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify({ url: url });
    }
    res = await fetch(endpoint, { method: 'POST', headers: headers, signal: _currentTaskController.signal, body: body });
    if (!res.ok) {
      var summarizeErr = await res.json().catch(function() { return { detail: res.statusText }; });
      throw new Error(summarizeErr.detail || res.statusText);
    }
    if (window.luminaBuddy) window.luminaBuddy.setState('success');
    resultDiv.innerHTML = await res.text();
  } catch (e) {
    if (window.luminaBuddy) window.luminaBuddy.setState('error');
    if (e.name === 'AbortError') {
      resultDiv.innerHTML = '<div class="bg-zinc-50 dark:bg-zinc-800/50 border border-zinc-200 dark:border-zinc-800 rounded-2xl p-6 w-full text-zinc-500 dark:text-zinc-400 font-bold flex items-start gap-2"><span class="text-sm">已取消处理。</span></div>';
    } else {
      resultDiv.innerHTML = '<div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-6 w-full text-red-600 dark:text-red-400 font-bold flex items-start gap-2"><svg class="w-5 h-5 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg><span class="text-sm">错误：' + escapeHtml(e.message) + '</span></div>';
    }
  } finally {
    _currentTaskController = null;
    restoreActionButtonState(btn, spec.button);
  }
}

document.addEventListener('paste', function(e) {
  var items = (e.clipboardData || e.originalEvent.clipboardData).items;
  if (!items) return;
  var targetFile = null;
  var fileType = '';
  var hasText = false;
  
  for (var i = 0; i < items.length; i++) {
    if (items[i].type.indexOf('text/plain') === 0) hasText = true;
  }
  
  if (hasText && e.target && (e.target.tagName === 'TEXTAREA' || (e.target.tagName === 'INPUT' && (e.target.type === 'text' || e.target.type === 'url' || e.target.type === 'search')))) {
    return;
  }

  for (var i = 0; i < items.length; i++) {
    if (items[i].kind === 'file') {
      var file = items[i].getAsFile();
      if (file) {
        if (file.type.indexOf('image/') === 0) {
          targetFile = file;
          fileType = 'image';
          break;
        } else if (file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')) {
          targetFile = file;
          fileType = 'pdf';
          break;
        } else if (file.name.toLowerCase().endsWith('.txt') || file.name.toLowerCase().endsWith('.md')) {
          targetFile = file;
          fileType = 'document';
          break;
        }
      }
    }
  }

  if (targetFile) {
    e.preventDefault();
    if (fileType === 'image') {
      var tabImage = document.getElementById('tab-image');
      if (tabImage && !tabImage.checked && typeof selectHomeTab === 'function') {
        selectHomeTab('image');
      }
      setLabInputMode('file');
      var dt = new DataTransfer();
      dt.items.add(targetFile);
      var input = document.getElementById('lab-file-input');
      if (input) {
        input.files = dt.files;
        showFilename(input, 'lab-filename', 'lab-file-preview');
      }
      var dropZone = document.querySelector('#lab-input-file > div');
      if (dropZone) {
        dropZone.classList.add('border-indigo-500', 'bg-indigo-50', 'dark:bg-indigo-900/20');
        setTimeout(function() { dropZone.classList.remove('border-indigo-500', 'bg-indigo-50', 'dark:bg-indigo-900/20'); }, 300);
      }
    } else if (fileType === 'pdf' || fileType === 'document') {
      var tabDocument = document.getElementById('tab-document');
      if (tabDocument && !tabDocument.checked && typeof selectHomeTab === 'function') {
        selectHomeTab('document');
      }
      setDocumentInputMode('file');
      var dt = new DataTransfer();
      dt.items.add(targetFile);
      var input = document.getElementById('document-file');
      if (input) {
        input.files = dt.files;
        showFilename(input, 'document-filename');
      }
      var dropZone = document.querySelector('#document-input-file > div');
      if (dropZone) {
        dropZone.classList.add('border-indigo-500', 'bg-indigo-50', 'dark:bg-indigo-900/20');
        setTimeout(function() { dropZone.classList.remove('border-indigo-500', 'bg-indigo-50', 'dark:bg-indigo-900/20'); }, 300);
      }
    }
  } else if (hasText) {
    e.preventDefault();
    var textData = (e.clipboardData || e.originalEvent.clipboardData).getData('text/plain');
    if (!textData) return;
    textData = textData.trim();
    
    var isUrl = textData.startsWith('http://') || textData.startsWith('https://');
    var lowerText = textData.toLowerCase();
    var isImageUrl = isUrl && (lowerText.endsWith('.jpg') || lowerText.endsWith('.png') || lowerText.endsWith('.jpeg') || lowerText.endsWith('.gif') || lowerText.endsWith('.webp') || lowerText.endsWith('.bmp'));
    var isPdfUrl = isUrl && lowerText.endsWith('.pdf');
    
    if (isImageUrl) {
      var tabImage = document.getElementById('tab-image');
      if (tabImage && !tabImage.checked && typeof selectHomeTab === 'function') {
        selectHomeTab('image');
      }
      setLabInputMode('url');
      var urlInput = document.getElementById('lab-url-input');
      if (urlInput) {
        urlInput.value = textData;
        if (typeof showUrlPreview === 'function') showUrlPreview(textData, 'lab-url-preview');
        urlInput.classList.add('ring-2', 'ring-indigo-500');
        setTimeout(function() { urlInput.classList.remove('ring-2', 'ring-indigo-500'); }, 300);
      }
    } else if (isPdfUrl) {
      var tabDocument = document.getElementById('tab-document');
      if (tabDocument && !tabDocument.checked && typeof selectHomeTab === 'function') {
        selectHomeTab('document');
      }
      setDocumentInputMode('url');
      var docUrlInput = document.getElementById('document-url');
      if (docUrlInput) {
        docUrlInput.value = textData;
        docUrlInput.classList.add('ring-2', 'ring-indigo-500');
        setTimeout(function() { docUrlInput.classList.remove('ring-2', 'ring-indigo-500'); }, 300);
      }
    } else {
      var tabDocument = document.getElementById('tab-document');
      if (tabDocument && !tabDocument.checked && typeof selectHomeTab === 'function') {
        selectHomeTab('document');
      }
      setDocumentInputMode('text');
      var textInput = document.getElementById('document-text');
      if (textInput) {
        textInput.value = textData;
        textInput.classList.add('ring-2', 'ring-indigo-500');
        setTimeout(function() { textInput.classList.remove('ring-2', 'ring-indigo-500'); }, 300);
      }
    }
  }
});
