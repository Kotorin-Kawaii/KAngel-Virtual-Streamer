# 人格事件流水线

## 数据流

```text
原始弹幕 ──> DanmakuReceivedEvent ──> 聚合速率/情绪，不直接修改人格
                                      │
定时器 ──> AudienceAtmosphereTickEvent┘──> 房间负载与总体气氛 mutation

被选中弹幕 ──> SemanticImpactAnalyzedEvent ──> 单次语义 mutation

礼物/房管/生命周期/静默 ──> 对应领域事件 ──> 确定性 reducer
```

该设计避免同一条被选中弹幕同时通过“普通弹幕内容影响”和“语义分析影响”重复修改人格。`DanmakuReceivedEvent` 只更新 pipeline 的房间聚合信号；内容级影响只由被选中后的语义事件提交。

## 顺序与背压

生产生命周期启动后，事件通过有界 `asyncio.PriorityQueue` 进入单一 worker：

1. 语义影响事件优先。
2. 直播生命周期事件次之。
3. 普通业务事件使用默认优先级。
4. 静默 tick 最低。

队列满时发布方收到明确异常，不会无界占用内存。pipeline 未启动的单元测试场景使用串行锁直接处理，以便领域测试不遗留后台任务。

## 幂等与来源

- 每个事件具有服务端 `event_id`。
- `source_event_id` 关联上游领域事件。
- `platform_message_id` 关联平台弹幕 ID。
- pipeline 保存有界已处理 ID 集，同一进程内重复事件不会重复修改状态。
- WebSocket 弹幕使用 `danmaku:<danmakuID>` 作为原始事件 ID，语义事件引用该来源。

## 回放日志

正式记录写入 SQLite `persona_event_log`，字段包括事件 ID/类型/UTC 时间/来源、脱敏 payload、mutation、前后状态和 pipeline 版本。写入适配器会移除昵称、弹幕正文、房管目标与理由；内存最近 100 条仅用于调试，不作为正式日志。

## 状态所有权

Reducer 是无副作用纯计算；只有 application engine 通过 `PersonaStateRepository` 提交状态。模型、transport、插件和事件本身都不能直接写人格状态。
