import asyncio
import json
import uuid
from collections import deque
from typing import Set, Dict, Optional
from datetime import datetime
from fastapi import WebSocket, WebSocketDisconnect
from kangel.danmaku.domain.entities import DanmakuResponse
from kangel.audience.domain.identity import ViewerIdentity
from kangel.audience.application.identity_service import (
    VerifiedAccountPrincipal, viewer_identity_resolver,
)
from config import settings
from kangel.shared.logging import logger
from kangel.infrastructure.security_metrics import security_metrics
from .protocol import WebSocketEventType


class WebSocketConnection:
    """WebSocket连接包装类，跟踪连接信息"""
    
    def __init__(
        self,
        websocket: WebSocket,
        verified_principal: Optional[VerifiedAccountPrincipal] = None,
        resolved_client_ip: Optional[str] = None,
    ):
        self.id: str = str(uuid.uuid4())
        self.websocket: WebSocket = websocket
        self.created_at: datetime = datetime.now()
        self.client_ip: Optional[str] = resolved_client_ip
        self.user_agent: Optional[str] = None
        self.retry_count: int = 0
        self.send_queue: asyncio.Queue = asyncio.Queue(
            maxsize=settings.rate_limit.ws_send_queue_size
        )
        self.sender_task: Optional[asyncio.Task] = None
        self.identity: ViewerIdentity = viewer_identity_resolver.resolve_for_connection(
            connection_id=self.id,
            nickname=(verified_principal.nickname if verified_principal
                      else f"用户_{self.id[:8]}"),
            principal=verified_principal,
        )
        
        # 提取客户端信息
        try:
            if hasattr(websocket, "client") and websocket.client:
                self.client_ip = resolved_client_ip or websocket.client.host
            if hasattr(websocket, "headers"):
                self.user_agent = websocket.headers.get("user-agent", "unknown")
        except Exception as e:
            logger.debug(f"获取客户端信息失败: {e}")
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "client_ip": self.client_ip,
            "user_agent": self.user_agent[:50] if self.user_agent else None,
            "created_at": self.created_at.isoformat(),
            "retry_count": self.retry_count,
            "identity_type": self.identity.identity_type.value,
            "current_nickname": self.identity.current_nickname,
        }

    def update_nickname(self, nickname: str) -> ViewerIdentity:
        self.identity = self.identity.with_nickname(nickname)
        return self.identity

    def start_sender(self) -> None:
        if self.sender_task is None:
            self.sender_task = asyncio.create_task(self._sender_loop())

    async def send_text(self, payload: str) -> None:
        loop = asyncio.get_running_loop()
        completed = loop.create_future()
        try:
            self.send_queue.put_nowait((payload, completed))
        except asyncio.QueueFull as exc:
            security_metrics.record("broadcast", "queue_full")
            raise RuntimeError("websocket_send_queue_full") from exc
        await completed

    async def _sender_loop(self) -> None:
        while True:
            payload, completed = await self.send_queue.get()
            try:
                await asyncio.wait_for(
                    self.websocket.send_text(payload),
                    timeout=settings.rate_limit.ws_send_timeout_seconds,
                )
                if not completed.done():
                    completed.set_result(None)
            except asyncio.CancelledError:
                if not completed.done():
                    completed.cancel()
                raise
            except Exception as exc:
                if not completed.done():
                    completed.set_exception(exc)
            finally:
                self.send_queue.task_done()

    def stop_sender(self) -> None:
        if self.sender_task and not self.sender_task.done():
            self.sender_task.cancel()
        while not self.send_queue.empty():
            try:
                _, completed = self.send_queue.get_nowait()
                if not completed.done():
                    completed.cancel()
                self.send_queue.task_done()
            except asyncio.QueueEmpty:
                break


class ConnectionManager:
    """WebSocket连接管理器"""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocketConnection] = {}
        self.message_history: deque = deque(maxlen=settings.danmaku.max_history)
        self._lock = asyncio.Lock()
        self._ip_connection_map: Dict[str, Set[str]] = {}
    
    async def connect(
        self,
        websocket: WebSocket,
        verified_principal: Optional[VerifiedAccountPrincipal] = None,
        resolved_client_ip: Optional[str] = None,
    ) -> str:
        """建立新的WebSocket连接，返回连接ID"""
        await websocket.accept()
        connection = WebSocketConnection(
            websocket,
            verified_principal=verified_principal,
            resolved_client_ip=resolved_client_ip,
        )
        
        async with self._lock:
            self.active_connections[connection.id] = connection
            connection.start_sender()
            
            # 记录IP到连接ID的映射
            if connection.client_ip:
                if connection.client_ip not in self._ip_connection_map:
                    self._ip_connection_map[connection.client_ip] = set()
                self._ip_connection_map[connection.client_ip].add(connection.id)
        
        # 检查是否有多个连接来自同一IP
        if connection.client_ip:
            ip_connections = self._ip_connection_map.get(connection.client_ip, set())
            if len(ip_connections) > 1:
                logger.warning(
                    f"IP {connection.client_ip} 有 {len(ip_connections)} 个活跃连接: "
                    f"{list(ip_connections)}"
                )
        
        logger.info(
            f"新连接建立 [ID: {connection.id[:8]}] "
            f"[IP: {connection.client_ip or 'unknown'}] "
            f"[当前连接数: {len(self.active_connections)}]"
        )
        security_metrics.record("connection", "connected")
        await self.send_history_to_client(connection)
        return connection.id

    def get_connection(self, websocket: WebSocket) -> Optional[WebSocketConnection]:
        """按 WebSocket 获取连接上下文。"""
        return next(
            (connection for connection in self.active_connections.values()
             if connection.websocket == websocket),
            None,
        )

    def update_connection_nickname(
        self, websocket: WebSocket, nickname: str
    ) -> Optional[ViewerIdentity]:
        connection = self.get_connection(websocket)
        return connection.update_nickname(nickname) if connection else None

    async def send_json_to(self, websocket: WebSocket, data: dict) -> None:
        """连接建立后统一进入该连接的串行发送队列。"""
        payload = json.dumps(data, ensure_ascii=False)
        connection = self.get_connection(websocket)
        if connection and hasattr(connection, "send_text"):
            await connection.send_text(payload)
            return
        await asyncio.wait_for(
            websocket.send_text(payload),
            timeout=settings.rate_limit.ws_send_timeout_seconds,
        )

    def update_account_nickname(
        self, account_id: str, nickname: str, nickname_version: int
    ) -> int:
        """让账号改名立即作用于该账号当前所有 WebSocket 连接。"""
        updated = 0
        for connection in self.active_connections.values():
            identity = connection.identity
            if identity.is_authenticated and identity.account_id == account_id:
                connection.identity = identity.model_copy(update={
                    "current_nickname": nickname,
                    "nickname_version": nickname_version,
                })
                updated += 1
        return updated
    
    async def disconnect(self, websocket: WebSocket) -> Optional[str]:
        """断开WebSocket连接，返回连接ID"""
        connection_id = None
        
        async with self._lock:
            # 查找对应的连接
            for cid, conn in list(self.active_connections.items()):
                if conn.websocket == websocket:
                    connection_id = cid
                    client_ip = conn.client_ip
                    
                    # 从活跃连接中移除
                    del self.active_connections[cid]
                    conn.stop_sender()
                    
                    # 从IP映射中移除
                    if client_ip and client_ip in self._ip_connection_map:
                        if cid in self._ip_connection_map[client_ip]:
                            self._ip_connection_map[client_ip].discard(cid)
                        if not self._ip_connection_map[client_ip]:
                            del self._ip_connection_map[client_ip]
                    
                    break
        
        if connection_id:
            security_metrics.record("connection", "closed")
            logger.info(
                f"连接断开 [ID: {connection_id[:8]}] "
                f"[IP: {client_ip or 'unknown'}] "
                f"[当前连接数: {len(self.active_connections)}]"
            )
        else:
            logger.warning(
                f"尝试断开未找到的WebSocket连接，当前连接数: {len(self.active_connections)}"
            )
        
        return connection_id
    
    async def send_history_to_client(self, connection: WebSocketConnection):
        """向指定客户端发送历史弹幕"""
        if not self.message_history:
            return
        
        history_messages = list(self.message_history)
        history_data = {
            "type": WebSocketEventType.HISTORY_BATCH,
            "messages": history_messages,
            "count": len(history_messages)
        }
        
        try:
            await connection.send_text(json.dumps(history_data, ensure_ascii=False))
            logger.debug(f"已向新用户 [ID: {connection.id[:8]}] 发送 {len(history_messages)} 条历史弹幕")
        except Exception as e:
            logger.error(f"发送历史弹幕失败 [ID: {connection.id[:8]}]: {e}")
    
    async def broadcast_message(self, message: DanmakuResponse):
        """记录并广播直播间展示消息；普通弹幕与 SC 共用展示历史。"""
        async with self._lock:
            self.message_history.append(message.model_dump())

        # 房间暂时无人时仍保留历史，供稍后建立的连接初始化。
        if not self.active_connections:
            return
        
        broadcast_data = {
            "type": WebSocketEventType.DANMAKU_REALTIME,
            "data": message.model_dump()
        }
        
        disconnected_ids = set()
        
        async def send_to_client(connection: WebSocketConnection):
            try:
                await connection.send_text(json.dumps(broadcast_data, ensure_ascii=False))
            except Exception as e:
                logger.error(f"发送消息失败 [ID: {connection.id[:8]}]: {e}")
                security_metrics.record("broadcast", "send_failed")
                disconnected_ids.add(connection.id)
        
        tasks = [send_to_client(conn) for conn in self.active_connections.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        if disconnected_ids:
            async with self._lock:
                for cid in disconnected_ids:
                    if cid in self.active_connections:
                        client_ip = self.active_connections[cid].client_ip
                        self.active_connections[cid].stop_sender()
                        del self.active_connections[cid]
                        
                        # 从IP映射中移除
                        if client_ip and client_ip in self._ip_connection_map:
                            self._ip_connection_map[client_ip].discard(cid)
                            if not self._ip_connection_map[client_ip]:
                                del self._ip_connection_map[client_ip]
    
    def get_connection_count(self) -> int:
        """获取当前连接数"""
        return len(self.active_connections)

    def get_ip_connection_count(self, client_ip: str) -> int:
        return len(self._ip_connection_map.get(client_ip, set()))

    def get_account_connection_count(self, account_id: str) -> int:
        return sum(
            1 for connection in self.active_connections.values()
            if connection.identity.is_authenticated
            and connection.identity.account_id == account_id
        )
    
    def get_history_count(self) -> int:
        """获取历史弹幕数量"""
        return len(self.message_history)
    
    def get_connection_info(self) -> dict:
        """获取所有连接信息"""
        return {
            "total_connections": len(self.active_connections),
            "connections": [conn.to_dict() for conn in self.active_connections.values()],
            "ip_distribution": {
                ip: len(conn_ids) 
                for ip, conn_ids in self._ip_connection_map.items()
            }
        }
    
    async def broadcast_json(self, data: dict):
        """广播JSON数据给所有连接的客户端"""
        if not self.active_connections:
            return 0
        
        broadcast_str = json.dumps(data, ensure_ascii=False)
        disconnected_ids = set()
        
        async def send_to_client(connection: WebSocketConnection):
            try:
                await connection.send_text(broadcast_str)
            except Exception as e:
                logger.error(f"广播数据失败 [ID: {connection.id[:8]}]: {e}")
                security_metrics.record("broadcast", "send_failed")
                disconnected_ids.add(connection.id)
        
        tasks = [send_to_client(conn) for conn in self.active_connections.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        if disconnected_ids:
            async with self._lock:
                for cid in disconnected_ids:
                    if cid in self.active_connections:
                        client_ip = self.active_connections[cid].client_ip
                        self.active_connections[cid].stop_sender()
                        del self.active_connections[cid]
                        
                        # 从IP映射中移除
                        if client_ip and client_ip in self._ip_connection_map:
                            self._ip_connection_map[client_ip].discard(cid)
                            if not self._ip_connection_map[client_ip]:
                                del self._ip_connection_map[client_ip]
        return len(disconnected_ids)


connection_manager = ConnectionManager()
