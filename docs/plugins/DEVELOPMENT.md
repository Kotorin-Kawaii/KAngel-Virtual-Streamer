# 插件开发指南

插件放在根目录 `plugins/<plugin_name>/__init__.py`，但只能导入稳定公共 API：

```python
from kangel.plugins import BasePlugin


class HelloWorldPlugin(BasePlugin):
    name = "hello_world"
    version = "1.0.0"
    description = "最小示例插件"

    async def on_load(self):
        self.context.log("info", "Hello World 插件已加载")

    async def on_unload(self):
        self.context.log("info", "Hello World 插件已卸载")

    async def on_danmaku_received(self, danmaku: dict):
        return danmaku


plugin = HelloWorldPlugin()
```

生命周期为 `on_load → on_enable → on_disable → on_unload`。可选事件处理器包括
`on_danmaku_received`、`on_danmaku_broadcast` 和 `on_ai_reply_generated`。

## PluginContext

`self.context` 仅提供以下受控能力：

- `log(level, message, *args)`：记录日志。
- `publish_event(name, payload)`：发布公开事件。
- `persona_snapshot()`：读取只读人格快照。
- `stream_snapshot()`：读取只读直播快照。

插件不得导入 `kangel.*.application`、`kangel.infrastructure` 或数据库实现，也不得
修改服务全局单例。需要新的能力时，应先扩展并评审 `PluginContext`，而不是绕过边界。

## 启用插件

在配置中把目录名加入 `PLUGINS__ENABLED_PLUGINS`，例如：

```dotenv
PLUGINS__ENABLED_PLUGINS=["hello_world"]
```

插件必须保持轻量、处理异常，并在 `on_unload` 中释放自己创建的资源。
