# 贡献指南 (Contributing Guide)

感谢你关注并愿意为本项目贡献力量！本项目是 **\*Flow 生态** 的一部分，致力于提供轻量、本地优先与 Agent 原生的研发与生产力工作流。

## 提交前准备

1. **查阅 Issue**：在提交重大功能或重构前，请先创建 Issue 进行方案讨论。
2. **规范阅读**：请阅读 [AGENTS.md](AGENTS.md) 以及 `docs/` 目录下的相关规范与架构设计文档。

## 研发工作流

1. **Fork & Clone**：Fork 本仓库并 Clone 到本地。
2. **创建特性分支**：
   ```bash
   git checkout -b feature/your-feature-name
   # 或
   git checkout -b fix/your-bug-fix
   ```
3. **环境与依赖**：
   - 请根据项目 README 配置本地依赖与 `.env` 文件。
   - 绝不提交真实的 Secret、API Key 或生产数据。
4. **代码风格与检查**：
   - 提交前请确保运行门禁检查命令通过。
   - Python 代码遵循 Ruff 规则与 PEP 8 标准。
   - TypeScript/前端代码遵循 ESLint 与 Prettier 格式。
5. **提交规范 (Commit Convention)**：
   提交信息推荐遵循 Conventional Commits：
   - `feat: 新增功能`
   - `fix: 修复 Bug`
   - `docs: 更新文档`
   - `style: 格式与代码风格变动`
   - `refactor: 代码重构`
   - `test: 增加或修改测试`
   - `chore: 构建或辅助工具变动`

## Pull Request 流程

1. 确保你的分支与 upstream `main` 保持最新。
2. 填写完整的 PR 模板，并关联相关 Issue。
3. 等待 CI 自动化流水线验证通过。
