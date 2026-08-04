# 设计与实施记录

`docs/superpowers/specs/` 和 `docs/superpowers/plans/` 保存 TermFlow 的设计讨论、实现计划
和验收记录。它们按日期命名，属于工程历史，不是部署时应照抄的命令手册；其中的未勾选任务、
旧文件路径和早期方案不能覆盖当前代码。

当前使用方式和执行入口以这些文件为准：

- [根 README](../../README.md)：最短安装、Docker Compose 和源码开发入口；
- [GitHub Actions 构建与发布](../github-actions.md)：手动 workflow、Tag Release、artifact
  名称和平台边界；
- [部署与恢复](../operations.md)：Compose、凭据、TOTP 密钥、回退和发布边界；
- [客户端构建边界](../../apps/clients/README.md)：Web/Tauri 共用层和原生工具链；
- [排障指南](../troubleshooting.md)：安装、Docker、A 连接和运行期故障。

实现事实直接来自仓库中的：

- `.github/workflows/*.yml`；
- `deploy/compose.yaml`、`deploy/Dockerfile.control-plane`；
- `scripts/release/`、`scripts/verify*.sh`；
- `package.json`、workspace manifest、`pyproject.toml` 和 Tauri Cargo/config 文件。

新增设计或计划时，请在文件中明确“设计/计划/已实现/已验证”的状态，并在功能合并后把
当前操作说明同步到上面的用户文档。
