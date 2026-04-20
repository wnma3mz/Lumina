(function() {
  getHomeTabDefs(); 
  applyHomeTabVisibility();
  
  var hash = (location.hash || '').slice(1);
  var initialDocumentTask = (hash === 'translate' || hash === 'summarize') ? hash : 'translate';
  setDocumentTask(initialDocumentTask, document.querySelector('#document-task-group [data-task="' + initialDocumentTask + '"]'));
  setDocumentInputMode('text');
  
  var initialLabTask = Object.keys(_labTasks)[0] || 'image_ocr';
  setLabTask(initialLabTask, document.querySelector('#lab-task-group [data-task="' + initialLabTask + '"]'));
  applyLabTaskAvailability();

  // Final check to prevent initial flash or race condition with browser default state
  var runRecovery = function() {
    var currentHash = (location.hash || '').slice(1);
    if (currentHash) {
      var targetTab = normalizeHomeTab(currentHash);
      if (_allHomeTabs.includes(targetTab)) {
        selectHomeTab(targetTab, false);
      }
    }
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', runRecovery);
  else setTimeout(runRecovery, 50);
})();
