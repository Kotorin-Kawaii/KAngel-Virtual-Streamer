# utils/logger.py
import logging
import os
import sys
import threading
from typing import Optional

# 全局锁，防止多线程竞争
_init_lock = threading.Lock()
# 使用集合记录已初始化的进程
_initialized_pids = set()
# 缓存logger实例
_logger_cache = {}

def _get_log_level() -> int:
    """获取日志级别，避免循环导入"""
    try:
        from config.settings import settings
        log_level_str = settings.server.log_level.upper()
    except Exception:
        log_level_str = "INFO"
    
    level_mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL
    }
    return level_mapping.get(log_level_str, logging.INFO)

def initialize_logger(force: bool = False) -> logging.Logger:
    """
    初始化日志系统
    """
    global _initialized_pids
    
    current_pid = os.getpid()
    
    # 检查当前进程是否已初始化
    if not force and current_pid in _initialized_pids:
        return logging.getLogger(__name__)
    
    with _init_lock:
        # 双重检查锁
        if not force and current_pid in _initialized_pids:
            return logging.getLogger(__name__)
        
        # 获取日志级别
        level = _get_log_level()
        
        # 获取根日志记录器
        root_logger = logging.getLogger()
        
        # 只有第一次才清除处理器
        if not root_logger.handlers or force:
            # 清除现有处理器
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
            
            # 配置基础日志
            try:
                logging.basicConfig(
                    level=level,
                    format='[%(levelname)s] | %(asctime)s | %(message)s',
                    datefmt='%H:%M:%S',
                    stream=sys.stderr,
                    force=True
                )
            except Exception as e:
                # 如果 basicConfig 失败，使用 fallback 配置
                print(f"警告: 日志配置失败，使用简单配置: {e}", file=sys.stderr)
                handler = logging.StreamHandler(sys.stderr)
                handler.setFormatter(
                    logging.Formatter('[%(levelname)s] %(message)s')
                )
                root_logger.addHandler(handler)
                root_logger.setLevel(level)
        
        # 标记当前进程已初始化
        _initialized_pids.add(current_pid)
        
        # 创建并返回logger
        logger = logging.getLogger(__name__)
        logger.debug(f"[PID:{current_pid}] 日志系统初始化完成，级别: {logging.getLevelName(level)}")
        
        return logger

def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取logger实例"""
    # 确保日志已初始化
    initialize_logger()
    
    if name:
        return logging.getLogger(name)
    
    # 获取调用者模块名
    frame = sys._getframe(1)  # 跳过当前帧
    module_name = frame.f_globals.get('__name__', __name__)
    return logging.getLogger(module_name)

# 延迟初始化的logger类
class LazyLogger:
    """延迟初始化的logger代理"""
    def __init__(self):
        self._logger = None
    
    def _get_logger(self):
        if self._logger is None:
            self._logger = get_logger()
        return self._logger
    
    def debug(self, msg, *args, **kwargs):
        self._get_logger().debug(msg, *args, **kwargs)
    
    def info(self, msg, *args, **kwargs):
        self._get_logger().info(msg, *args, **kwargs)
    
    def warning(self, msg, *args, **kwargs):
        self._get_logger().warning(msg, *args, **kwargs)
    
    def error(self, msg, *args, **kwargs):
        self._get_logger().error(msg, *args, **kwargs)
    
    def critical(self, msg, *args, **kwargs):
        self._get_logger().critical(msg, *args, **kwargs)
    
    def exception(self, msg, *args, **kwargs):
        self._get_logger().exception(msg, *args, **kwargs)

# 导出延迟logger实例
logger = LazyLogger()
