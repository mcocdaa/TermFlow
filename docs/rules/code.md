---
title: 代码规范
description: 通用代码编写规范与最佳实践
keywords: [代码规范, code, 最佳实践]
version: "2.0"
---

# 代码规范

本规范适用于任何软件项目的代码编写。

## 目录
- [1. 导入规范](#1-导入规范)
- [2. 命名规范](#2-命名规范)
- [3. 类型注解](#3-类型注解)
- [4. 文档字符串](#4-文档字符串)
- [5. 异常处理](#5-异常处理)
- [6. 常量定义](#6-常量定义)
- [7. 日志规范](#7-日志规范)
- [8. 文件组织](#8-文件组织)
- [9. 代码审查清单](#9-代码审查清单)

---

## 1. 导入规范

### 1.1 移除未使用的导入
**✗ 错误示例:**
```python
import os
import sys
import json  # 未使用
import asyncio  # 未使用
```

**✓ 正确示例:**
```python
import os
import sys
```

### 1.2 导入顺序
按以下顺序分组导入：
1. 标准库
2. 第三方库
3. 本地模块

每组之间空一行。

**✓ 正确示例:**
```python
import os
import sys
from typing import Dict, List

import yaml
from fastapi import FastAPI

from core import setting_manager
from managers import session_manager
```

---

## 2. 命名规范

### 2.1 常量命名
使用全大写下划线分隔。

**✗ 错误示例:**
```python
secret_bytes = 24
max_wait = 50
```

**✓ 正确示例:**
```python
SECRET_TOKEN_BYTES = 24
REFRESH_WAIT_MAX_ITERATIONS = 50
DEFAULT_LOG_LEVEL = "INFO"
```

### 2.2 枚举类型
对于状态值、类型等，使用 `Enum`。

**✗ 错误示例:**
```python
if status == "raw":
    ...
elif status == "approved":
    ...
```

**✓ 正确示例:**
```python
from enum import Enum

class SessionStatus(Enum):
    RAW = "raw"
    CURATED = "curated"
    APPROVED = "approved"

if status == SessionStatus.RAW:
    ...
```

---

## 3. 类型注解

### 3.1 公共方法必须有类型注解
所有公共方法的参数和返回值都必须添加类型注解。

**✗ 错误示例:**
```python
def get_session(self, session_id):
    ...
```

**✓ 正确示例:**
```python
def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
    ...
```

---

## 4. 文档字符串

### 4.1 Google 风格文档字符串
所有公共方法必须有文档字符串，使用 Google 风格。

**✓ 正确示例:**
```python
def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
    """获取单个会话

    Args:
        session_id: 会话ID

    Returns:
        会话字典，如果不存在返回 None
    """
    ...
```

---

## 5. 异常处理

### 5.1 避免捕获所有异常
尽可能捕获具体的异常类型。

**✗ 错误示例:**
```python
try:
    data = json.load(f)
except Exception:
    logger.error("加载失败")
```

**✓ 正确示例:**
```python
try:
    data = json.load(f)
except json.JSONDecodeError as e:
    logger.error(f"JSON 解析失败: {e}")
except IOError as e:
    logger.error(f"文件读取失败: {e}")
```

### 5.2 记录异常堆栈
使用 `exc_info=True` 记录完整堆栈。

**✓ 正确示例:**
```python
try:
    ...
except Exception as e:
    logger.error(f"操作失败: {e}", exc_info=True)
```

---

## 6. 常量定义

### 6.1 避免魔法数字
所有字面量数值都应该定义为常量。

**✗ 错误示例:**
```python
token = secrets.token_bytes(24)
for _ in range(50):
    ...
```

**✓ 正确示例:**
```python
SECRET_TOKEN_BYTES = 24
REFRESH_WAIT_MAX_ITERATIONS = 50

token = secrets.token_bytes(SECRET_TOKEN_BYTES)
for _ in range(REFRESH_WAIT_MAX_ITERATIONS):
    ...
```

---

## 7. 日志规范

### 7.1 统一使用 logging
禁止使用 `print`，统一使用 `logging` 模块。

**✗ 错误示例:**
```python
print("[Route] 已注册 API")
```

**✓ 正确示例:**
```python
logger.info("[Route] 已注册 API")
```

### 7.2 日志级别
- `DEBUG`: 调试信息
- `INFO`: 一般信息
- `WARNING`: 警告信息
- `ERROR`: 错误信息

---

## 8. 文件组织

### 8.1 文件大小
单个文件建议不超过 400 行。超过时考虑拆分为多个模块。

| 建议 | 操作 |
|------|------|
| < 400 行 | 保持 |
| 400-500 行 | 考虑拆分 |
| > 500 行 | 必须拆分 |

### 8.2 删除死代码
删除未使用的方法、变量和类。

**✗ 错误示例:**
```python
def _unused_method(self):
    """从未被调用的方法"""
    ...
```

---

## 9. 代码审查清单

提交代码前，请检查以下项目：

- [ ] 移除所有未使用的导入
- [ ] 移除所有未使用的变量和方法
- [ ] 所有魔法数字已定义为常量
- [ ] 使用 logging 而不是 print
- [ ] 公共方法有类型注解
- [ ] 公共方法有文档字符串
- [ ] 异常处理尽可能具体
- [ ] 文件不超过 500 行
- [ ] 运行测试通过
