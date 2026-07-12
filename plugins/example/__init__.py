from kangel.plugins import BasePlugin


class ExamplePlugin(BasePlugin):
    """示例插件 - 记录弹幕统计"""
    name = "example"
    version = "1.0.0"
    description = "示例插件：记录弹幕统计"
    
    def __init__(self):
        super().__init__()
        self.danmaku_count = 0
    
    async def on_load(self):
        self.context.log("info", "📊 示例插件加载完成")
    
    async def on_unload(self):
        self.context.log("info", "📊 示例插件卸载，共处理 %s 条弹幕", self.danmaku_count)
    
    async def on_enable(self):
        self.context.log("info", "📊 示例插件已启用")
    
    async def on_disable(self):
        self.context.log("info", "📊 示例插件已禁用")
    
    async def on_danmaku_received(self, danmaku: dict) -> dict:
        """收到弹幕时调用"""
        self.danmaku_count += 1
        nickname = danmaku.get("nickname", "匿名")
        message = danmaku.get("message", "")
        
        self.context.log("info", "📊 弹幕统计: 第%s条 - %s: %s...", self.danmaku_count, nickname, message[:30])
        
        return danmaku


plugin = ExamplePlugin()
