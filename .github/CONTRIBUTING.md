# 贡献指南

感谢你对 Lumina 的关注！以下是参与贡献的方式。

## 开发环境

```bash
git clone https://github.com/wnma3mz/Lumina.git
cd Lumina
uv sync                    # 安装依赖
uv run lumina server       # 启动开发模式
```

系统要求：macOS 13+，Apple Silicon，Python 3.10–3.12。

## 提交 PR

1. Fork 本仓库，创建功能分支（`git checkout -b feat/your-feature`）
2. 确保改动在本地跑通（`uv run lumina server` 无报错）
3. 如涉及 PDF 翻译/总结，用 `tests/fixtures/2010_Bottou_SGD.pdf` 测试
4. 提交 PR，描述改动目的和测试方法

## 代码风格

- 格式化：`black --line-length 120`
- 类型注解：新增函数建议加，存量代码不强制

## 报告 Bug

使用 [Bug 报告模板](.github/ISSUE_TEMPLATE/bug_report.md) 提 Issue，附上日志（`~/.lumina/qa.log`）。
