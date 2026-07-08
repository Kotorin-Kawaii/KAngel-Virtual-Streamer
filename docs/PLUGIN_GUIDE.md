# 插件开发指南

插件目录位于仓库根目录的 `plugins/`，当前项目没有 `app/` 包结构。插件会由 `plugins.plugin_manager.PluginManager` 动态加载，模块路径形如 `plugins.hello_world`。

## 创建你的第一个插件

1. 创建插件目录：

```bash
mkdir -p plugins/hello_world
```

2. 创建 `plugins/hello_world/__init__.py`：

```python
from plugins import BasePlugin
from utils.logger import logger


class HelloWorldPlugin(BasePlugin):
    name = "hello_world"
    version = "1.0.0"
    description = "一个简单的示例插件"

    async def on_load(self):
        logger.info("Hello World 插件已加载！")

    async def on_unload(self):
        logger.info("Hello World 插件已卸载！")

    async def on_enable(self):
        logger.info("Hello World 插件已启用！")

    async def on_disable(self):
        logger.info("Hello World 插件已禁用！")


plugin = HelloWorldPlugin()
```

3. 在 `.env` 中启用插件：

```dotenv
PLUGINS__ENABLED_PLUGINS=["hello_world"]
PLUGINS__PLUGIN_DIR=plugins
```

4. 重启服务，日志中会出现插件加载信息。

## 生命周期

```text
on_load() → [on_enable()] → 运行中 → [on_disable()] → on_unload()
```

| 方法 | 说明 | 必须实现 |
| :--- | :--- | :--- |
| `on_load()` | 插件加载时调用，用于初始化资源 | 是 |
| `on_unload()` | 插件卸载时调用，用于清理资源 | 是 |
| `on_enable()` | 插件启用时调用 | 否 |
| `on_disable()` | 插件禁用时调用 | 否 |

## 可用事件

插件可以通过定义特定方法参与主流程。当前常用事件如下：

| 方法 | 触发时机 | 入参 | 返回 |
| :--- | :--- | :--- | :--- |
| `on_danmaku_received(danmaku)` | 收到弹幕后 | `dict` | 修改后的 `dict` |
| `on_danmaku_broadcast(danmaku)` | 弹幕广播前 | `dict` | 修改后的 `dict` |
| `on_ai_reply_generated(reply)` | AI 回复生成后 | `dict` | 修改后的 `dict` |

## 示例：弹幕过滤器

```python
from plugins import BasePlugin
from utils.logger import logger


class ContentFilterPlugin(BasePlugin):
    name = "content_filter"
    version = "1.0.0"
    description = "弹幕内容过滤器"

    blocked_words = ["敏感词1", "敏感词2"]

    async def on_load(self):
        logger.info("内容过滤器插件加载")

    async def on_unload(self):
        logger.info("内容过滤器插件卸载")

    async def on_danmaku_received(self, danmaku: dict) -> dict:
        message = danmaku.get("message", "")
        for word in self.blocked_words:
            message = message.replace(word, "***")
        danmaku["message"] = message
        return danmaku


plugin = ContentFilterPlugin()
```

## 示例：AI 回复后处理

```python
from plugins import BasePlugin
from utils.logger import logger


class ReplyEnhancerPlugin(BasePlugin):
    name = "reply_enhancer"
    version = "1.0.0"
    description = "AI 回复增强器"

    async def on_load(self):
        logger.info("回复增强器插件加载")

    async def on_unload(self):
        logger.info("回复增强器插件卸载")

    async def on_ai_reply_generated(self, reply: dict) -> dict:
        if "sentences" in reply:
            for sentence in reply["sentences"]:
                text = sentence.get("text", "")
                if text and not text.endswith(("♪", "🧬", "†")):
                    sentence["text"] = text + " ♪"
        return reply


plugin = ReplyEnhancerPlugin()
```

## 访问核心服务

当前仓库是扁平包结构，核心模块直接从 `core`、`config`、`utils` 导入：

```python
from plugins import BasePlugin
from core import persona_engine, connection_manager
from config import settings
from utils.logger import logger


class AdvancedPlugin(BasePlugin):
    name = "advanced_plugin"
    version = "1.0.0"
    description = "高级插件示例"

    async def on_load(self):
        logger.info(f"服务器端口: {settings.server.port}")

    async def on_unload(self):
        pass

    async def on_danmaku_received(self, danmaku: dict) -> dict:
        logger.info(f"当前心情值: {persona_engine.state.mood}")
        logger.info(f"当前在线人数: {connection_manager.get_connection_count()}")
        return danmaku


plugin = AdvancedPlugin()
```

## 自定义事件总线

```python
from plugins import BasePlugin
from core import event_bus
from utils.logger import logger


class CustomEventPlugin(BasePlugin):
    name = "custom_event"
    version = "1.0.0"
    description = "自定义事件示例"

    async def on_load(self):
        await event_bus.subscribe("my_custom_event", self.handle_custom_event)

    async def on_unload(self):
        await event_bus.unsubscribe("my_custom_event", self.handle_custom_event)

    async def handle_custom_event(self, data):
        logger.info(f"收到自定义事件: {data}")

    async def on_danmaku_received(self, danmaku: dict) -> dict:
        await event_bus.emit("my_custom_event", {"danmaku": danmaku})
        return danmaku


plugin = CustomEventPlugin()
```

## 调试插件

插件管理接口属于管理/调试能力，默认建议只在本地或可信后台启用。接口契约见 [FRONTEND_API.md](FRONTEND_API.md)。

```http
GET /plugins
POST /plugins/{plugin_name}/enable
POST /plugins/{plugin_name}/disable
```

响应示例：

```json
{
  "plugins": [
    {
      "name": "hello_world",
      "version": "1.0.0",
      "description": "一个简单的示例插件",
      "enabled": true
    }
  ]
}
```

## 注意事项

- 插件代码运行在主 Python 进程内，请避免阻塞事件循环。
- 不要在插件中提交真实 API Key、Cookie、数据库或用户数据。
- 会改变前端协议字段的插件，应同时更新 `docs/FRONTEND_API.md`。
- 会影响人格、记忆、SC 优先级或弹幕筛选的插件，应提供清晰的回归测试方式。
