from plugins import BasePlugin
from utils.logger import logger


class ExamplePlugin(BasePlugin):
    """示例插件 - 记录弹幕统计"""
    name = "example"
    version = "1.0.0"
    description = "示例插件：记录弹幕统计"
    
    def __init__(self):
        super().__init__()
        self.danmaku_count = 0
    
    async def on_load(self):
        logger.info("📊 示例插件加载完成")
    
    async def on_unload(self):
        logger.info(f"📊 示例插件卸载，共处理 {self.danmaku_count} 条弹幕")
    
    async def on_enable(self):
        logger.info("📊 示例插件已启用")
    
    async def on_disable(self):
        logger.info("📊 示例插件已禁用")
    
    async def on_danmaku_received(self, danmaku: dict) -> dict:
        """收到弹幕时调用"""
        self.danmaku_count += 1
        nickname = danmaku.get("nickname", "匿名")
        message = danmaku.get("message", "")
        
        logger.info(f"📊 弹幕统计: 第{self.danmaku_count}条 - {nickname}: {message[:30]}...")
        
        return danmaku


plugin = ExamplePlugin()
