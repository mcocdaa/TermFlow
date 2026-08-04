# TermFlow 测试套件整理设计

## 目标

在不改变产品行为、不增加业务测试数量的前提下，降低测试维护成本，提高现有测试的复用性和分层可执行性。

本次整理以现有测试作为永久行为契约。判断标准不是测试数量越少越好，而是每项长期约束只有一个明确所有者，公共测试装配只维护一份，快速测试与环境依赖测试可以独立运行。

## 范围

本次包含：

- 合并发布工作流、客户端隐私和仓库入口的重复静态契约。
- 为 Control Plane API/WebSocket 测试提供统一的 Computer、Term 和 Instance 装配工厂；直接测试 enrollment/register 的文件继续保留原始 HTTP 步骤。
- 为现有真实进程统一认证测试补齐 `e2e` 分类，不新增测试。
- 压缩响应式 CSS 和运维文档中的精确源码字符串断言，保留少量长期不变量与已有动态行为验证。
- 保持 `python -m pytest` 入口，避免仓库内 `scripts.*` 导入漂移。

本次不包含：

- 产品代码或 API 行为修改。
- 新测试文件、新业务测试函数或新的参数化案例。
- 数据库初始化实现、CI Job 拆分、Playwright 执行策略调整。
- 为追求测试数量下降而删除安全、认证、tmux、协议或跨进程行为覆盖。

数据库模板与 CI 并行化只有在本次整理后重新计时、证明仍为主要瓶颈时才进入后续工作。

## 所有权收敛

### 发布与部署契约

`tests/release/test_packaging_workflow_contract.py` 是三个基础打包工作流的唯一测试所有者。`test_check_version.py` 只验证版本检查器，`test_compose_contract.py` 只验证 Compose、镜像和本地交付入口。

`test_repository_contract.py` 中已被实际 CI/Compose/发布测试覆盖的文件存在性和脚本文本检查删除，不再保留第二份弱字符串契约。

### 客户端隐私契约

`tests/test_client_workspace_contract.py` 是跨工作区依赖方向、平台 API 和持久化边界的唯一静态扫描所有者。Web Vitest 只保留终端输出不会进入浏览器存储、URL、日志或遥测全局的动态测试。

### Control Plane 装配

公共工厂放在 `apps/control-plane/tests/conftest.py`，返回带标识和凭据的简单数据对象，并显式检查每个装配步骤成功。业务测试通过工厂表达前置状态，不再复制 enrollment/register 请求链。

测试 enrollment、凭据签发或 register 本身时不使用工厂，防止辅助层掩盖被测行为。

## CSS 与文档契约

响应式测试保留跨实现仍然成立的关键规则，例如粗指针入口、安全区、隐藏滚动条、移动 KeyBar 横向滚动和 viewport-lock。Computer 删除、Toast、TOTP 布局和终端交互继续由现有组件测试或 Playwright 行为覆盖，不再锁定完整 CSS 声明文本。

文档测试保留：文档入口可发现、已废弃配置不再出现、关键安装/发布命令存在。描述性措辞和重复的产品能力枚举不作为精确自动化断言。

## 验证标准

- 不新增测试文件、测试函数或参数化案例。
- Python 和 Vitest 收集数量不得增加；删除的数量必须能对应明确重复契约。
- 发布、部署、客户端、Control Plane、Node 非 tmux 测试分别通过。
- tmux/E2E 使用现有 marker 单独收集；受限环境不能启动 tmux 时如实报告，不把环境失败描述为代码回归。
- `ruff`、前端类型检查和 `git diff --check` 通过。
- 输出整理前后测试文件数、用例数、测试代码行数和分组耗时，确认维护成本实际下降。
