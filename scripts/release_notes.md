## 安装方法

1. 下载下方的 **Lumina.zip**
2. 解压，把 `Lumina.app` 拖到「应用程序」文件夹
3. 双击启动

首次启动会自动下载模型（约 622MB），右上角有进度通知，完成后弹出「Lumina 已就绪」即可使用。

> **提示"无法验证开发者"？** 右键点击 `Lumina.app` → 选「打开」→ 再点「打开」，之后可直接双击。

---

## 功能

- **翻译 PDF**：右键 → 快速操作 → 用 Lumina 翻译 PDF（生成中文版 + 双语版）
- **总结 PDF**：右键 → 快速操作 → 用 Lumina 总结 PDF
- **浏览器插件**：API 地址填 `http://127.0.0.1:31821/v1`，模型名填 `lumina`
- **手机 PWA**：Safari 访问 `http://Mac局域网IP:31821`，添加到主屏幕
- **语音转文字**：配合 Raycast / Alfred 使用

右键菜单安装：打开终端运行
```
bash /Applications/Lumina.app/Contents/MacOS/scripts/install_quick_action.sh
```

---

## 系统要求

- macOS 13+（Apple Silicon）
- 首次启动需要网络下载模型（约 622MB）
