# 依赖方向规则

## 允许的方向

```text
HTTP / WebSocket / Plugin
            ↓
        Application
            ↓
          Domain

Infrastructure ──实现──> Domain/Application 定义的接口
```

依赖箭头表示“上层可以导入下层公开 API”。领域层定义业务事实和端口，基础设施层实现端口；领域层不知道实现来自 SQLite、WebSocket 或外部 AI。

## 强制规则

1. `kangel.<domain>.domain` 不得导入 FastAPI、WebSocket、数据库驱动、AI SDK、transport 或 infrastructure。
2. application 负责编排用例、事务边界和端口调用，不保存具体数据库或协议实现。
3. transport 只解析/校验请求并转换响应，不直接实现人格、记忆、关系或活动规则。
4. infrastructure 不得反向成为业务规则的唯一存放位置。
5. 插件只能从 `kangel.plugins` 及获准的领域包级公共 API 导入对象，不得访问深层模块、数据库管理器或全局单例。
6. 每个领域的稳定对象只通过包级 `__init__.py` 暴露；跨领域代码不得依赖私有模块路径。
7. 根目录 `core/models/api/services/utils` 已退役，任何代码或文档示例均不得重新引入这些包。

## 时间与标识

- 内部事件时间使用带时区 UTC。
- 对外展示时区由 stream 配置决定。
- 事件和实体标识由服务端生成，客户端提供的 ID 只能作为不可信输入或幂等键。

## 自动检查

`tests/test_architecture_layout.py` 检查包骨架、公共导入和领域层禁止依赖。随着领域迁移，检查范围必须同步扩大；不得通过排除新文件绕过规则。
