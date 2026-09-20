---
title: API规范
description: 通用RESTful API架构规范
keywords: [api, rest, version-control, router-loader]
version: "2.0"
---

# API 规范

本规范适用于任何软件项目的 RESTful API 架构设计。

### 特性

- 路由自动加载：扫描目录自动挂载
- 版本控制：动态加载指定版本
- 统一前缀：`/api`

---

## 目录结构

```
backend/
├── api/
│   ├── __init__.py          # 路由总入口
│   └── v1/                  # API 版本
│       ├── __init__.py      # 版本路由汇总
│       ├── A.py             # 资源模块
│       └── C/               # 子命名空间
│           ├── __init__.py
│           └── C1.py
├── core/
│   └── router_loader.py     # 路由自动加载器
```

---

## 核心代码

### 路由总入口 `api/__init__.py`

```python
# @file backend/api/__init__.py
# @brief API 路由总入口 - 支持版本控制

import importlib
from fastapi import FastAPI
from XXXX.settings import API_VERSION

def register_routers(app: FastAPI):
    version_package_name = f"{__name__}.{API_VERSION}"

    try:
        version_package = importlib.import_module(version_package_name)
    except ModuleNotFoundError:
        raise RuntimeError(f"API 版本模块不存在: {version_package_name}")

    if hasattr(version_package, "router"):
        app.include_router(
            version_package.router,
            prefix="/api",
            tags=[API_VERSION.upper()]
        )
```

### 模块路由汇总 `api/v1/__init__.py` 或 `api/v1/C/__init__.py`

```python
# @file backend/api/v1/__init__.py
# @brief API v1 模块路由汇总

from fastapi import APIRouter
from pathlib import Path
from core.router_loader import include_routers_from_directory

router = APIRouter()

include_routers_from_directory(
    parent_router=router,
    package_name=__package__,
    directory_path=Path(__file__).parent,
    auto_tag=False,
    auto_prefix=False,
    skip_modules=[],
)
```

### 路由自动加载器 `core/router_loader.py`

```python
# @file backend/core/router_loader.py
# @brief 自动路由加载器

from fastapi import APIRouter
import importlib
from pathlib import Path
from typing import List, Optional

def include_routers_from_directory(
    parent_router: APIRouter,
    package_name: str,
    directory_path: Path,
    *,
    skip_modules: Optional[List[str]] = None,
    auto_tag: bool = False,
    auto_prefix: bool = False
) -> None:
    if skip_modules is None:
        skip_modules = []

    base_name = package_name.split(".")[-1]

    for path in directory_path.iterdir():
        if path.name == "__init__.py":
            continue

        module_name = None

        if path.is_file() and path.suffix == ".py":
            module_name = path.stem
        elif path.is_dir() and (path / "__init__.py").exists():
            module_name = path.name

        if not module_name or module_name in skip_modules:
            continue

        try:
            module = importlib.import_module(f".{module_name}", package=package_name)

            if hasattr(module, "router"):
                sub_router = getattr(module, "router")

                kwargs = {"prefix": f"/{base_name}"}
                if auto_tag:
                    kwargs["tags"] = [module_name]
                if auto_prefix:
                    kwargs["prefix"] += f"/{module_name}"

                parent_router.include_router(sub_router, **kwargs)

        except Exception as e:
            print(f"[RouterLoader] 挂载失败 {module_name}: {str(e)}")
```

---

## 加载流程

```
app = FastAPI()
    │
    ▼
register_routers(app)
    │
    ▼
动态导入 api.v1
    │
    ▼
include_routers_from_directory() 扫描目录
    │
    ├── A.py    → /v1/A
    └── C/      → /v1/C
           └── C1.py → /v1/C/C1
```

---

## 子路由模块规范

每个路由模块需定义 `router` 对象：

```python
# @file backend/api/v1/A.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/A")
async def get_list():
    return []
```

---

## 参数说明

| 参数 | 说明 |
|------|------|
| skip_modules | 跳过加载的模块名列表 |
| auto_tag | 为路由添加模块名作为标签 |
| auto_prefix | 自动追加模块名前缀 |

| auto_tag | auto_prefix | 路由前缀 |
|----------|-------------|---------|
| False | False | /v1 |
| True | False | /v1 (带标签) |
| True | True | /v1/C (带标签) |

---

## 复用

1. 复制核心文件：
   - `api/__init__.py`
   - `api/v1/__init__.py`
   - `core/router_loader.py`

2. 目录结构保持一致

3. 配置 `API_VERSION`

4. 应用入口调用 `register_routers(app)`
