function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function showPdfRouteSheet(file) {
  _pendingPdfFile = file;
  var el = document.getElementById('pdf-route-filename');
  if (el) el.textContent = '📄 ' + file.name;
  document.getElementById('pdf-route-sheet').classList.add('open');
}

function closePdfRouteSheet() {
  document.getElementById('pdf-route-sheet').classList.remove('open');
  _pendingPdfFile = null;
}

function routePdf(target) {
  var file = _pendingPdfFile;
  closePdfRouteSheet();
  if (!file) return;
  var dt = new DataTransfer();
  dt.items.add(file);
  var inp = document.getElementById('document-file');
  if (inp) {
    inp.files = dt.files;
    showFilename(inp, 'document-filename');
  }
  setDocumentTask(target, document.querySelector('#document-task-group [data-task="' + target + '"]'));
  setDocumentInputMode('file');
  selectHomeTab('document');
}

document.addEventListener('dragover', function(e) { e.preventDefault(); });
document.addEventListener('drop', function(e) {
  e.preventDefault();
  var f = e.dataTransfer.files[0];
  if (!isSupportedDocumentFile(f)) return;
  if (document.getElementById('tab-digest').checked) {
    showPdfRouteSheet(f);
  } else if (document.getElementById('tab-document').checked) {
    var dt = new DataTransfer();
    dt.items.add(f);
    var inp = document.getElementById('document-file');
    if (inp) {
      inp.files = dt.files;
      showFilename(inp, 'document-filename');
    }
    setDocumentInputMode('file');
  }
});

