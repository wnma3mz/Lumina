var _homeTabDefs = [];
var _allHomeTabs = [];
var _legacyHomeTabMap = {};
var _imageTaskDefs = [];
var _homeTabsVersionKey = 'lumina.homeTabs.version';
var _homeTabsCurrentVersion = '5'; // 迁移版本号，与 getLocalHomeTabs 迁移逻辑保持一致
var _homeUi = {};
var _imagePrompts = {};

var _cachedAppData = null;
function getAppDataJson(attrName, fallback) {
  if (!_cachedAppData) {
    var script = document.getElementById('lumina-app-data');
    if (script) {
      try {
        _cachedAppData = JSON.parse(script.textContent);
      } catch (e) {
        console.error('Lumina: Critical error parsing app data block:', e);
      }
    }
  }
  if (_cachedAppData && _cachedAppData[attrName] !== undefined) {
    return _cachedAppData[attrName];
  }
  return fallback;
}

function getHomeTabDefs() {
  if (_homeTabDefs.length) return _homeTabDefs;
  _homeTabDefs = getAppDataJson('homeTabs', []);
  _allHomeTabs = _homeTabDefs.map(function(item) { return item.key; });
  
  // CRITICAL FALLBACK: If data-attributes failed, scrape from DOM radio buttons
  if (!_allHomeTabs.length) {
    var radios = document.querySelectorAll('input[name="tab"]');
    radios.forEach(function(r) {
      var key = r.id.replace('tab-', '');
      if (key && !_allHomeTabs.includes(key)) _allHomeTabs.push(key);
    });
  }
  return _homeTabDefs;
}

function getLegacyHomeTabMap() {
  if (Object.keys(_legacyHomeTabMap).length) return _legacyHomeTabMap;
  _legacyHomeTabMap = getAppDataJson('legacyHomeTabMap', {});
  return _legacyHomeTabMap;
}

function getImageTaskDefs() {
  if (_imageTaskDefs.length) return _imageTaskDefs;
  _imageTaskDefs = getAppDataJson('imageTaskDefs', []);
  return _imageTaskDefs;
}

function getHomeUiConfig() {
  if (Object.keys(_homeUi).length) return _homeUi;
  _homeUi = getAppDataJson('homeUi', {});
  return _homeUi;
}

function getImagePrompts() {
  if (Object.keys(_imagePrompts).length) return _imagePrompts;
  _imagePrompts = getAppDataJson('imagePrompts', {});
  return _imagePrompts;
}

function normalizeHomeTab(tab) {
  var legacyMap = getLegacyHomeTabMap();
  return legacyMap[tab] || tab;
}

function normalizeHomeTabs(tabs) {
  if (!_allHomeTabs.length) getHomeTabDefs();
  var out = [];
  (Array.isArray(tabs) ? tabs : []).forEach(function(tab) {
    tab = normalizeHomeTab(tab);
    if (_allHomeTabs.includes(tab) && !out.includes(tab)) out.push(tab);
  });
  return out;
}

function getAudioTaskDefs() {
  return getAppDataJson('audioTaskDefs', []);
}

function getServerHomeTabs() {
  if (!_allHomeTabs.length) getHomeTabDefs();
  var homeUi = getHomeUiConfig();
  var tabs = normalizeHomeTabs(homeUi.enabled_tabs);
  if (homeUi.image_enabled === false) {
    tabs = tabs.filter(function(tab) { return tab !== 'image'; });
  }
  if (homeUi.audio_enabled === false) {
    tabs = tabs.filter(function(tab) { return tab !== 'audio'; });
  } else if (homeUi.audio_enabled === true && !tabs.includes('audio')) {
    var idx = tabs.indexOf('settings');
    if (idx >= 0) tabs.splice(idx, 0, 'audio');
    else tabs.push('audio');
  }
  if (homeUi.game_enabled === false) {
    tabs = tabs.filter(function(tab) { return tab !== 'game'; });
  } else if (homeUi.game_enabled === true && !tabs.includes('game')) {
    var gameIdx = tabs.indexOf('settings');
    if (gameIdx >= 0) tabs.splice(gameIdx, 0, 'game');
    else tabs.push('game');
  }
  if (homeUi.digest_enabled === false) {
    tabs = tabs.filter(function(tab) { return tab !== 'digest'; });
  }
  if (homeUi.document_enabled === false) {
    tabs = tabs.filter(function(tab) { return tab !== 'document'; });
  }
  return tabs.length ? tabs : _allHomeTabs.slice();
}

function getLocalHomeTabs() {
  var homeUi = getHomeUiConfig();
  if (homeUi.allow_local_override === false) return null;
  try {
    var raw = localStorage.getItem('lumina.homeTabs');
    if (!raw) return null;
    var parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    
    // Version 5 Migration: Remove game from core auto-insert (now opt-in via game.enabled)
    var version = localStorage.getItem(_homeTabsVersionKey);
    if (version !== _homeTabsCurrentVersion) {
      var allDefs = _allHomeTabs.length ? _allHomeTabs : ['digest', 'document', 'image', 'audio', 'settings'];
      ['image', 'audio'].forEach(function(tab) {
        if (!parsed.includes(tab) && allDefs.includes(tab)) {
          var settingsIdx = parsed.indexOf('settings');
          if (settingsIdx >= 0) parsed.splice(settingsIdx, 0, tab);
          else parsed.push(tab);
        }
      });
      localStorage.setItem('lumina.homeTabs', JSON.stringify(parsed));
      localStorage.setItem(_homeTabsVersionKey, _homeTabsCurrentVersion);
    }

    // Check missing enabled tabs dynamically:
    if (homeUi.audio_enabled === true && !parsed.includes('audio')) {
      var audioIdx = parsed.indexOf('settings');
      if (audioIdx >= 0) parsed.splice(audioIdx, 0, 'audio');
      else parsed.push('audio');
    }
    if (homeUi.game_enabled === true && !parsed.includes('game')) {
      var gameLocalIdx = parsed.indexOf('settings');
      if (gameLocalIdx >= 0) parsed.splice(gameLocalIdx, 0, 'game');
      else parsed.push('game');
    }
    
    return normalizeHomeTabs(parsed);
  } catch (e) {
    return null;
  }
}

function getEnabledLabTasks() {
  var labTasks = _labTasks;
  var homeUi = getHomeUiConfig();
  var modules = Array.isArray(homeUi.image_modules) ? homeUi.image_modules.filter(function(key) {
    return !!labTasks[key];
  }) : [];
  return modules.length ? modules : Object.keys(labTasks);
}

function getEffectiveHomeTabs() {
  var local = getLocalHomeTabs();
  var server = getServerHomeTabs();
  // Ensure we always have at least one tab
  var out = local || server;
  return (out && out.length) ? out : ['digest'];
}

function applyHomeTabVisibility() {
  getHomeTabDefs(); 
  var visibleTabs = getEffectiveHomeTabs();
  var active = normalizeHomeTab(location.hash.slice(1));
  
  // Mandatory: If user is on a valid tab via URL, it MUST be visible
  if (active && _allHomeTabs.includes(active) && !visibleTabs.includes(active)) {
    visibleTabs.push(active);
  }

  _allHomeTabs.forEach(function(tab) {
    var label = document.querySelector('#home-nav label[data-tab="' + tab + '"]');
    if (label) label.style.display = visibleTabs.includes(tab) ? '' : 'none';
  });

  // Re-check active tab visibility
  if (!visibleTabs.includes(active)) {
    var checked = document.querySelector('input[name="tab"]:checked');
    active = checked ? checked.id.replace('tab-', '') : '';
  }
  if (!visibleTabs.includes(active)) active = visibleTabs[0] || 'digest';
  
  selectHomeTab(active, false);
  return visibleTabs;
}

function selectHomeTab(tab, updateHash) {
  tab = normalizeHomeTab(tab);
  if (!_allHomeTabs.includes(tab)) return;
  var radio = document.getElementById('tab-' + tab);
  if (radio) radio.checked = true;
  if (updateHash !== false) {
    history.replaceState(null, '', tab === 'digest' ? location.pathname : '#' + tab);
  }
}

function saveLocalHomeTabs(tabs) {
  var homeUi = getHomeUiConfig();
  if (homeUi.allow_local_override === false) return;
  try {
    localStorage.setItem('lumina.homeTabs', JSON.stringify(normalizeHomeTabs(tabs)));
    localStorage.setItem(_homeTabsVersionKey, _homeTabsCurrentVersion);
  } catch (_) {}
}

function clearLocalHomeTabs() {
  try {
    localStorage.removeItem('lumina.homeTabs');
    localStorage.removeItem(_homeTabsVersionKey);
  } catch (_) {}
}
