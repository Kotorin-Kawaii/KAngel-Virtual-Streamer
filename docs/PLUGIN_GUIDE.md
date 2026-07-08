# 插件开发指南

## 快速开始

### 创建你的第一个插件

1. 在 `app/plugins/` 目录下创建插件文件夹：

```bash
mkdir -p app/plugins/hello_world
```

2. 创建插件文件 `app/plugins/hello_world/__init__.py`：

```python
from app.plugins import BasePlugin
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

3. 在配置中启用插件：

编辑 `config.json`：
```json
{
  "plugins": {
    "enabled_plugins": ["hello_world"]
  }
}
```

4. 启动服务器，你会看到插件加载日志。

---

## 插件生命周期

```
on_load() → [on_enable()] → 运行中 → [on_disable()] → on_unload()
```

### 生命周期方法说明

| 方法 | 说明 | 必须实现 |
|------|------|----------|
| `on_load()` | 插件加载时调用，用于初始化资源 | 是 |
| `on_unload()` | 插件卸载时调用，用于清理资源 | 是 |
| `on_enable()` | 插件启用时调用 | 否 |
| `on_disable()` | 插件禁用时调用 | 否 |

---

## 事件处理

### 可用事件列表

| 事件名 | 触发时机 | 参数 |
|--------|----------|------|
| `danmaku_received` | 收到弹幕时 | danmaku: dict |
| `danmaku_broadcast` | 弹幕广播前 | danmaku: dict |
| `ai_reply_generated` | AI回复生成后 | reply: dict |

### 事件处理示例

#### 1. 弹幕过滤器插件

```python
from app.plugins import BasePlugin
from utils.logger import logger

class ContentFilterPlugin(BasePlugin):
    name = "content_filter"
    version = "1.0.0"
    description = "弹幕内容过滤器"
    
    # 敏感词列表
    blocked_words = ["敏感词1", "敏感词2"]
    
    async def on_load(self):
        logger.info("内容过滤器插件加载")
    
    async def on_unload(self):
        logger.info("内容过滤器插件卸载")
    
    async def on_danmaku_received(self, danmaku: dict) -> dict:
        """过滤弹幕内容"""
        message = danmaku.get("message", "")
        
        # 替换敏感词
        for word in self.blocked_words:
            message = message.replace(word, "***")
        
        danmaku["message"] = message
        return danmaku

plugin = ContentFilterPlugin()
```

#### 2. AI回复后处理器

```python
from app.plugins import BasePlugin
from utils.logger import logger

class ReplyEnhancerPlugin(BasePlugin):
    name = "reply_enhancer"
    version = "1.0.0"
    description = "AI回复增强器"
    
    async def on_load(self):
        logger.info("回复增强器插件加载")
    
    async def on_unload(self):
        logger.info("回复增强器插件卸载")
    
    async def on_ai_reply_generated(self, reply: dict) -> dict:
        """增强AI回复"""
        # 在每个句子末尾添加表情
        if "sentences" in reply:
            for sentence in reply["sentences"]:
                text = sentence.get("text", "")
                if text and not text.endswith(("♪", "🧬", "†")):
                    sentence["text"] = text + " ♪"
        
        return reply

plugin = ReplyEnhancerPlugin()
```

---

## 插件配置

### 使用插件内置配置

```python
from app.plugins import BasePlugin

class ConfigurablePlugin(BasePlugin):
    name = "configurable"
    version = "1.0.0"
    description = "可配置插件示例"
    
    async def on_load(self):
        # 设置默认配置
        self.set_config("greeting", "Hello!")
        self.set_config("max_length", 100)
    
    async def on_unload(self):
        pass
    
    async def on_danmaku_received(self, danmaku: dict):
        greeting = self.get_config("greeting", "Hi!")
        max_len = self.get_config("max_length", 100)
        
        # 使用配置...
        pass

plugin = ConfigurablePlugin()
```

### 通过全局配置管理插件

在 `config.json` 中：

```json
{
  "plugin_config": {
    "my_plugin": {
      "option1": "value1",
      "option2": "value2"
    }
  }
}
```

---

## 高级用法

### 访问核心服务

插件可以导入和使用核心服务：

```python
from app.plugins import BasePlugin
from app.core import persona_engine, connection_manager
from app.config import settings
from utils.logger import logger

class AdvancedPlugin(BasePlugin):
    name = "advanced_plugin"
    version = "1.0.0"
    description = "高级插件示例"
    
    async def on_load(self):
        logger.info(f"服务器端口: {settings.server.port}")
    
    async def on_unload(self):
        pass
    
    async def on_danmaku_received(self, danmaku):
        # 获取当前人格状态
        mood = persona_engine.state.mood
        logger.info(f"当前心情值: {mood}")
        
        # 获取在线人数
        connections = connection_manager.get_connection_count()
        logger.info(f"当前在线人数: {connections}")
        
        return danmaku

plugin = AdvancedPlugin()
```

### 使用事件总线

插件可以发布和订阅自定义事件：

```python
from app.plugins import BasePlugin
from app.core import event_bus
from utils.logger import logger

class CustomEventPlugin(BasePlugin):
    name = "custom_event"
    version = "1.0.0"
    description = "自定义事件示例"
    
    async def on_load(self):
        # 订阅自定义事件
        await event_bus.subscribe("my_custom_event", self.handle_custom_event)
    
    async def on_unload(self):
        await event_bus.unsubscribe("my_custom_event", self.handle_custom_event)
    
    async def handle_custom_event(self, data):
        logger.info(f"收到自定义事件: {data}")
    
    async def on_danmaku_received(self, danmaku):
        # 发布自定义事件
        await event_bus.emit("my_custom_event", {"danmaku": danmaku})
        return danmaku

plugin = CustomEventPlugin()
```



## 如何调试插件：

管理员接口默认关闭，启用参见 [完整 HTTP/WebSocket 接口契约](docs/FRONTEND_API.md) P2

### 查看插件列表

```bash
GET /plugins
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

### 动态启用/禁用插件

```bash
# 启用
POST /plugins/hello_world/enable

# 禁用
POST /plugins/hello_world/disable
```
