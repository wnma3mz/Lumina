var _homeTabDefs = [];
var _allHomeTabs = [];
var _legacyHomeTabMap = {};
var _imageTaskDefs = [];
var _homeTabsVersionKey = 'lumina.homeTabs.version';
var _homeUi = {};
var _imagePrompts = {};

function getAppDataJson(attrName, fallback) {
  var app = document.getElementById('app');
  if (!app) return fallback;
  try {
    var raw = app.dataset[attrName];
    return raw ? JSON.parse(raw) : fallback;
  } catch (_) {
    return fallback;
  }
}

function getHomeTabDefs() {
  if (_homeTabDefs.length) return _homeTabDefs;
  _homeTabDefs = getAppDataJson('homeTabs', []);
  _allHomeTabs = _homeTabDefs.map(function(item) { return item.key; });
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
  var out = [];
  (Array.isArray(tabs) ? tabs : []).forEach(function(tab) {
    tab = normalizeHomeTab(tab);
    if (_allHomeTabs.includes(tab) && !out.includes(tab)) out.push(tab);
  });
  return out;
}

function getServerHomeTabs() {
  if (!_allHomeTabs.length) getHomeTabDefs();
  var homeUi = getHomeUiConfig();
  var tabs = normalizeHomeTabs(homeUi.enabled_tabs);
  if (homeUi.image_enabled === false) {
    tabs = tabs.filter(function(tab) { return tab !== 'image'; });
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
    parsed = normalizeHomeTabs(parsed);
    var version = localStorage.getItem(_homeTabsVersionKey);
    if (version !== '2' && parsed.includes('document') && !parsed.includes('image')) {
      var settingsIdx = parsed.indexOf('settings');
      if (settingsIdx >= 0) parsed.splice(settingsIdx, 0, 'image');
      else parsed.push('image');
    }
    localStorage.setItem('lumina.homeTabs', JSON.stringify(parsed));
    localStorage.setItem(_homeTabsVersionKey, '2');
    return parsed.length ? parsed : null;
  } catch (_) {
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
  return getServerHomeTabs();
}

function applyHomeTabVisibility() {
  var visibleTabs = getEffectiveHomeTabs();
  _allHomeTabs.forEach(function(tab) {
    var label = document.querySelector('#home-nav label[data-tab="' + tab + '"]');
    if (label) label.style.display = visibleTabs.includes(tab) ? '' : 'none';
  });

  var active = normalizeHomeTab(location.hash.slice(1));
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
    localStorage.setItem(_homeTabsVersionKey, '2');
  } catch (_) {}
}

function clearLocalHomeTabs() {
  try {
    localStorage.removeItem('lumina.homeTabs');
    localStorage.removeItem(_homeTabsVersionKey);
  } catch (_) {}
}
