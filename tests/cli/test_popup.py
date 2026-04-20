from lumina.platform_support import popup


def test_build_popup_html_uses_pywebview_bridge():
    html = popup._build_popup_html(
        {"original": "hello", "action": "polish", "lang": "en", "base_url": "http://127.0.0.1:31821", "label": "润色"},
        bridge="pywebview",
    )
    assert "pywebview.api.close()" in html
    assert "pywebview.api.copy(s.result)" in html
    assert "window.webkit.messageHandlers.close.postMessage" not in html


def test_run_popup_dispatches_to_platform_backend(monkeypatch):
    called = []

    monkeypatch.setattr(popup, "IS_MACOS", False)
    monkeypatch.setattr(popup, "_run_popup_pywebview", lambda params: called.append(("pywebview", params)))
    popup._run_popup({"label": "test"})

    assert called == [("pywebview", {"label": "test"})]
