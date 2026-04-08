## 安装方法

1. 下载下方的 **Lumina.zip**
2. 解压，双击 **`install.command`**
3. 安装完成后，双击「应用程序」中的 Lumina 启动

首次启动会自动下载模型（约 622MB），右上角有进度通知，完成后弹出「Lumina 已就绪」即可使用。

---

## v0.2.0 更新内容

- **修复右键翻译/总结报错**：修复 `multiprocessing` spawn 子进程重走 CLI 导致的「服务输入出现问题」
- **命令行模式支持定时日报**：`uv run lumina server` 现在也会每小时自动生成日报，与 .app 行为一致
- **日报自动刷新**：Web UI 每 5 分钟静默拉取最新日报，无需手动点击
- **Tab 顺序调整**：默认显示「日报」，顺序改为 日报 → 翻译 → 总结
- **更快的并发响应**：本地推理引擎改用 Continuous Batching，多请求并发时首 token 延迟降低 5-8x
- **图标修复**：解决图标透明通道丢失问题

---

## 功能

- **翻译 PDF**：右键 → 快速操作 → 用 Lumina 翻译 PDF（生成中文版 + 双语版）
- **总结 PDF**：右键 → 快速操作 → 用 Lumina 总结 PDF
- **浏览器插件**：API 地址填 `http://127.0.0.1:31821/v1`，模型名填 `lumina`
- **手机 PWA**：Safari 访问 `http://Mac局域网IP:31821`，添加到主屏幕
- **语音转文字**：配合 Raycast / Alfred 使用
- **每日日报**：自动汇总 Git 提交、Shell 历史、剪贴板等活动

右键菜单安装：打开终端运行
```
bash /Applications/Lumina.app/Contents/MacOS/scripts/install_quick_action.sh
```

---

## 系统要求

- macOS 13+（Apple Silicon）
- 首次启动需要网络下载模型（约 622MB）
