"""
人工接管实时通知中心 - SSE (Server-Sent Events) 连接管理与事件广播

坐席端通过 HTTP 建立 SSE 长连接订阅通知；当业务触发「转人工」时，
服务端主动向所有已连接的坐席端推送「新工单」事件。

选择 SSE 而非 WebSocket 的原因：
- 需求是单向推送（服务端 → 坐席），SSE 足够且更简单；
- 复用 HTTP，无需额外协议升级，兼容反向代理/鉴权中间件；
- 浏览器原生支持断线自动重连。
"""
import asyncio
import logging
from typing import Dict, Set

logger = logging.getLogger(__name__)


class NotificationHub:
    """SSE 连接管理中心：维护坐席长连接并广播事件。"""

    def __init__(self):
        # agent_id -> 该坐席的所有活跃连接队列（支持一个坐席多端登录）
        self._connections: Dict[str, Set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, agent_id: str) -> asyncio.Queue:
        """坐席建立长连接，返回其专属事件队列。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._connections.setdefault(agent_id, set()).add(queue)
        logger.info(
            "坐席订阅通知: agent=%s, 活跃连接=%d",
            agent_id, len(self._connections.get(agent_id, set())),
        )
        return queue

    async def unsubscribe(self, agent_id: str, queue: asyncio.Queue):
        """坐席断开长连接，移除其事件队列。"""
        async with self._lock:
            conns = self._connections.get(agent_id)
            if conns and queue in conns:
                conns.discard(queue)
                if not conns:
                    self._connections.pop(agent_id, None)
        logger.info("坐席取消订阅: agent=%s", agent_id)

    async def broadcast_all(self, event: dict):
        """向所有已连接的坐席端推送事件（新工单进入共享队列）。"""
        async with self._lock:
            agents = list(self._connections.keys())
        for agent_id in agents:
            await self._push_to_agent(agent_id, event)

    async def _push_to_agent(self, agent_id: str, event: dict):
        """向指定坐席的所有连接投递事件，积压连接会被剔除。"""
        async with self._lock:
            queues = list(self._connections.get(agent_id, set()))
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 客户端消费过慢导致积压，直接断开该连接
                await self.unsubscribe(agent_id, queue)
                logger.warning("坐席连接积压已断开: agent=%s", agent_id)

    @property
    def connection_count(self) -> int:
        return sum(len(qs) for qs in self._connections.values())


notification_hub = NotificationHub()