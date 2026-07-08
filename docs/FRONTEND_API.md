# KAngel Server 前端接口完整契约

本文档面向前端开发者，覆盖当前后端全部 HTTP 路由、WebSocket 收发事件、认证方式、字段类型、状态码和接入时序。


## 1. 基础约定

### 1.1 地址

```text
HTTP 开发地址：http://localhost:8000
WebSocket 开发地址：ws://localhost:8000/danmaku
OpenAPI JSON：http://localhost:8000/openapi.json
Swagger UI：http://localhost:8000/docs
```

当前所有业务 HTTP 路由都直接挂在根路径，`settings.api_v1_str` 尚未作为 `/api/v1` 前缀启用。

生产环境应使用 HTTPS/WSS，并配置：

```env
AUTH__COOKIE_SECURE=true
```

跨域部署时，后端 `CORS__ALLOWED_ORIGINS` 必须包含前端的精确 Origin（协议、域名和端口），例如 `https://kangel.kotorin.cn`。前端请求认证接口时设置 `credentials: "include"`；服务端允许凭据，因此不会接受 `*` 来源。

当前 GitHub Pages 前端是跨站部署，登录 Cookie 使用 `HttpOnly; Secure; SameSite=None; Partitioned`（CHIPS）。`AUTH__COOKIE_DOMAIN` 默认留空，Cookie 只发送回 API 域名且按顶级站点分区；不要由前端读取或复制令牌。

### 1.2 内容与时间

- HTTP 请求及响应：`application/json; charset=utf-8`。
- WebSocket 消息：UTF-8 JSON 文本帧，不使用二进制帧。
- 时间字段均为 ISO 8601 字符串；部分旧字段不带时区偏移，前端解析时不要假定它一定是 UTC。
- 浮点人格值、情绪值和权重通常位于 `0..1`。
- 未特别标注的 HTTP 接口当前不要求认证。

### 1.3 通用错误

高成本 HTTP 请求在服务整体过载时可能返回：

```ts
type ServerOverloadedError = {
  code: "server_overloaded";
  message: string;
  retry_after_seconds: number;
  scope: "server_overload";
};
```

状态码为 `503`，同时包含 `Retry-After`。该响应不表示令牌失效，前端不得清除登录态或自动循环重试。低成本的状态与直播元数据接口会尽量保持可用。

```ts
type ISODateTime = string;

type HttpError = {
  detail: string | Array<{
    type: string;
    loc: Array<string | number>;
    msg: string;
    input?: unknown;
  }>;
};
```

- `400`：业务参数错误。
- `401`：访问令牌缺失、无效或过期。
- `404`：资源不存在。
- `409`：资源冲突，例如用户名重复或删除当前昵称。
- `422`：FastAPI/Pydantic 请求校验失败。
- `500`：后端处理失败。

## 2. 接口分层

| 层级 | 接口 | 前端用途 |
|---|---|---|
| 核心 | `/auth/**`、`/stream/metadata`、`/persona/state`、`/emotion/list`、`/danmaku` WebSocket | 登录、账号设置、直播页和弹幕互动 |
| 可选 | `/status`、`/stream/activities`、`/memory/hot-topics` | 状态页、直播间辅助信息 |
| 内部管理/调试 | `/config`、`/plugins/**`、`/connections`、`/database/**`、`/persona/impact/**`、大部分 `/memory/**` 和 `/emotion/**` 写接口 | 仅后台工具，不应进入公开用户前端 |

内部管理/调试接口默认关闭并返回 `404`。仅当服务端显式设置 `ADMIN__ENABLED=true` 且配置独立 `ADMIN__API_KEY` 后，才接受 `Authorization: Bearer <admin-key>` 或 `X-Admin-Key`；普通用户访问令牌和 Cookie 不能调用。`/config` 中密钥使用掩码序列化，前端仍不得保存或记录管理响应。

## 3. 认证与登录状态

### 3.1 登录结果

注册和登录成功后，服务端同时：

1. 在 JSON 中返回不透明 `access_token`；
2. 设置同值的 `HttpOnly` Cookie，默认名为 `kangel_access_token`。

```ts
type Account = {
  account_id: string;       // 不可变 UUID，人物记忆唯一归属键
  username: string;         // 登录名
  nickname: string;         // 可变展示昵称
  nickname_version: number; // 从 1 开始递增
  created_at: ISODateTime;
};

type AuthTokenResponse = {
  account: Account;
  access_token: string;
  token_type: "bearer";
  expires_at: ISODateTime;
};
```

推荐同源浏览器只使用 HttpOnly Cookie，不把 Token 写入 `localStorage`。若使用 Cookie，HTTP 请求需要：

```ts
fetch(url, {
  credentials: "include",
  headers: { "Content-Type": "application/json" },
});
```

非浏览器客户端可以发送：

```http
Authorization: Bearer <access_token>
```

HTTP 同时存在 Bearer 和 Cookie 时，Bearer 优先。

### 3.2 WebSocket 认证优先级

WebSocket 按以下顺序取令牌：

1. `ws://host/danmaku?access_token=<token>`；
2. `kangel_access_token` Cookie；
3. `Authorization: Bearer <token>` 握手头。

浏览器原生 `WebSocket` 不能设置自定义 Authorization Header，因此同源页面推荐 Cookie：

```ts
const ws = new WebSocket("ws://localhost:8000/danmaku");
```

没有令牌时作为游客连接；携带无效或过期令牌时不会降级为游客，而是以 WebSocket 关闭码 `1008` 拒绝。

## 4. 核心 HTTP 接口

认证接口触发应用层限流时返回：

```ts
type RateLimitError = {
  code: "rate_limited";
  message: string;
  retry_after_seconds: number;
  scope: "auth_register" | "auth_login" |
    "auth_register_capacity" | "auth_login_capacity" | string;
  request_id: string;
};
```

HTTP 状态为 `429 Too Many Requests`，响应头包含整数秒 `Retry-After` 和用于排查的
`X-Request-ID`。该错误不是登录态失效，前端不得因此清除 Cookie 或跳转登录页。

普通 HTTP 请求还可能在进入业务路由前返回：

```ts
type RequestBoundaryError = {
  code: "request_too_large" | "request_timeout";
  message: string;
};
```

- `413 request_too_large`：请求头、查询参数、声明长度或分块传输后的实际请求体超过限制。
- `408 request_timeout`：请求体上传超时。
- `504 request_timeout`：服务端处理超时。

这些错误均不表示访问令牌失效，不得据此清除登录态；非幂等写请求不要无条件自动重放。

### 4.1 创建账号

`POST /auth/register`

认证：无。成功后自动登录。

```ts
type RegisterRequest = {
  username: string; // 3..64；禁止空白和控制字符；NFKC + casefold 判重
  password: string; // 8..128
  nickname: string; // 1..100
};
```

请求：

```json
{
  "username": "alice_01",
  "password": "a-strong-password",
  "nickname": "小爱"
}
```

成功：`201 AuthTokenResponse`。

错误：`409` 用户名已存在；`422` 字段格式错误；`429 RateLimitError` 注册过于频繁。

### 4.2 登录

`POST /auth/login`

认证：无。

```ts
type LoginRequest = {
  username: string; // 3..64
  password: string; // 1..128
};
```

成功：`200 AuthTokenResponse`。每次登录签发新令牌，旧令牌在到期前仍有效。

错误：`401` 用户名不存在或密码错误，两者统一返回 `用户名或密码错误`；`422` 格式错误；`429 RateLimitError` 登录尝试过于频繁。

当前没有登出、刷新令牌、修改密码或找回密码接口。

### 4.3 修改昵称

`PATCH /auth/profile/nickname`

认证：登录账号。

```ts
type NicknameUpdateRequest = {
  nickname: string; // 1..100，禁止控制字符
};
```

成功：`200 Account`。

作用：原子结束旧昵称版本并创建新版本；`account_id`、关系和长期记忆不变。该账号已有 WebSocket 会立即使用新昵称。

### 4.4 查询昵称历史

`GET /auth/profile/nickname-history`

认证：登录账号。

```ts
type NicknameHistoryEntry = {
  version: number;
  nickname: string;
  started_at: ISODateTime;
  ended_at: ISODateTime | null;
  is_current: boolean;
};

type NicknameHistoryResponse = {
  account_id: string;
  history: NicknameHistoryEntry[]; // 版本倒序
};
```

### 4.5 删除旧昵称版本

`DELETE /auth/profile/nickname-history/{version}`

认证：登录账号。路径参数 `version: integer >= 1`。

成功：`204 No Content`。

错误：`404` 版本不存在；`409` 当前昵称版本不可删除。

作用：物理删除旧昵称版本。当前昵称不能通过此接口删除。

### 4.6 人物记忆类型

```ts
type RelationshipMemory = {
  account_id: string;
  viewer_key: string; // 形如 account:<account_id>
  nickname: string;
  familiarity: number;
  affinity: number;
  trust: number;
  boundary_strikes: number;
  interaction_count: number;
  reply_count: number;
  recent_topics: string[];
  last_message: string;
  first_seen_at: ISODateTime;
  last_seen_at: ISODateTime;
  updated_at?: ISODateTime;
};

type AIReplySentence = {
  emotion: string;
  text: string;
};

type AIReply = {
  emotions: string[];
  sentences: AIReplySentence[];
};

type ConversationTransition =
  | "new"
  | "continuation"
  | "contrast"
  | "supplement"
  | "switch";

type ConversationFragment = {
  id: number;
  session_scope_id: string;
  danmaku_id: string;
  nickname: string;              // 对话发生时的昵称
  nickname_version: number;
  viewer_message: string;        // 已按隐私策略脱敏
  streamer_reply: string;
  reply_payload: AIReply | null;
  topic_label: string;
  transition: ConversationTransition;
  resolved_reference: string | null;
  sentiment: number;             // -1..1
  importance: number;            // 0..1
  created_at: ISODateTime;
  last_accessed_at: ISODateTime | null;
  access_count: number;
  archived: boolean;
  expires_at: ISODateTime;
};

type TopicSummary = {
  id: number;
  topic_label: string;
  summary: string;
  source_count: number;
  importance: number;
  first_seen_at: ISODateTime;
  last_seen_at: ISODateTime;
  last_accessed_at: ISODateTime | null;
  access_count: number;
  expires_at: ISODateTime;
};

type AccountMemoryResponse = {
  account_id: string;
  long_term_memory_enabled: boolean;
  retention_days: number;
  relationship: RelationshipMemory | null;
  recent_conversations: ConversationFragment[];
  topic_summaries: TopicSummary[];
};
```

### 4.7 查询人物记忆

`GET /auth/profile/memory`

认证：登录账号。

成功：`200 AccountMemoryResponse`。最多返回近期活跃对话和话题摘要；没有记忆时关系为 `null`、数组为空。

### 4.8 导出人物记忆

`GET /auth/profile/memory/export`

认证：登录账号。

```ts
type AccountMemoryExportResponse = AccountMemoryResponse & {
  nickname_history: NicknameHistoryEntry[];
  exported_at: ISODateTime;
};
```

与普通查询不同，导出包含允许保留的活跃及归档对话。响应是 JSON，不是文件流；前端如需下载，应自行创建 `Blob`。

### 4.9 开启或退出长期记忆

`PUT /auth/profile/memory/preferences`

认证：登录账号。

```ts
type MemoryPreferenceUpdateRequest = {
  long_term_memory_enabled: boolean;
};

type MemoryPreferenceResponse = {
  account_id: string;
  long_term_memory_enabled: boolean;
  updated_at: ISODateTime | null;
};
```

设置为 `false` 会立即清除已有关系、对话片段和话题摘要，并阻止后续持久化；重新开启不会恢复已删除内容。

### 4.10 清除已有记忆

`DELETE /auth/profile/memory`

认证：登录账号。

成功：`204 No Content`。

只清除已有记忆，记忆开关保持不变；若希望清除后不再写入，应调用偏好接口关闭长期记忆。

### 4.11 服务状态

`GET /`

```ts
type RootResponse = {
  status: string;       // "danmaku server running"
  connections: number;
  history_count: number;
};
```

`GET /status`

```ts
type ServerStatus = {
  status: string; // "running"
  active_connections: number;
  message_history_count: number;
  server_time: ISODateTime;
};
```

### 4.12 当前人格状态

`GET /persona/state`

```ts
type PersonaStateResponse = {
  mood: number;
  darkness: number;
  stress: number;
  behavior: {
    reply_aggressiveness: number;
    ignore_probability: number;
  };
};
```

### 4.13 当前直播元数据

`GET /stream/metadata`

```ts
type StreamMetadata = {
  stream_id: string;
  streamer_name: string;
  viewer_count: number;
  total_joined: number;
  total_left: number;
  current_time: ISODateTime;
  stream_start_time: ISODateTime | null;
  stream_duration_seconds: number;
  is_live: boolean;
  stream_status: "streaming" | "offline" | string;
  schedule_timezone: string; // 后端实际采用的 IANA 时区
  schedule_config_valid: boolean;
  schedule_errors: string[];
  current_stream_start_time: ISODateTime | null;
  current_stream_end_time: ISODateTime | null;
  next_stream_start_time: ISODateTime | null;
  next_stream_end_time: ISODateTime | null;
  daily_theme_id: string;
  daily_theme_name: string;
  daily_theme_date: string; // schedule_timezone 下的 YYYY-MM-DD
  theme_config_valid: boolean;
  theme_errors: string[];
  current_activity: StreamerActivity | null;
  activity_config_valid: boolean;
  activity_errors: string[];
  extra: Record<string, unknown>;
};

type StreamerActivity = {
  activity_id: string;
  category: string;
  display_name: string;
  object_name: string;
  started_at: ISODateTime;
  version: number;
};
```

`is_live` 和 `stream_status` 由后端按排期计算，不代表服务器进程是否运行。每日主题及当前具体活动均由后端选择；前端必须直接消费这些字段，不自行按浏览器时区、本地随机数或主题名称推算。下播时 `current_activity=null`。

### 4.14 最近进出活动

`GET /stream/activities?limit=20`

查询参数：`limit: integer`，默认 `20`。

```ts
type UserActivity = {
  user_id: string; // 当前通常是连接 UUID，不是公开 account_id
  nickname: string; // 登录用户为账号直播间昵称；游客为“用户_<连接ID前8位>”
  action: "join" | "leave" | string;
  timestamp: ISODateTime;
  extra: Record<string, unknown>;
};

type StreamActivitiesResponse = {
  activities: UserActivity[];
  total: number; // 本次返回数量，不是历史总数
};
```

`extra` 当前可能包含客户端 IP，公开前端不得展示、持久化或上报该字段。

### 4.15 可用情绪动作

`GET /emotion/list`

```ts
type EmotionListResponse = {
  available_emotions: string[];
};
```

当前共 39 个值，按表现语义分组如下：

- 正向：`开心`、`喜欢`、`得意`、`卖萌`、`兴奋`、`温柔`、`亢奋`、`大笑`
- 亲密/表现：`害羞`、`撒娇`、`自恋`、`做作`、`帅气`、`打招呼`、`笑着挥手`
- 负向：`生气`、`委屈`、`无语`、`尴尬`、`伤心`、`焦虑`、`困倦`、`疲惫`、`厌恶`、`害怕`
- 强烈/阴暗：`阴暗`、`暴怒`、`毒舌`、`嘲讽`、`崩溃`、`冷笑`、`震惊`
- 中性/动作：`眼神飘忽`、`祷告`、`认真`、`思考`、`惊讶`、`搞怪`、`宅系`

前端应以接口返回值为准，并为未知值提供通用动作或静态立绘兜底。

### 4.16 提交与查询 SC

`POST /sc`，认证：登录 Cookie 或 Bearer Token。游客不能提交。

`GET /sc/config` 无需登录，返回前端可见规则：

```ts
type SCConfigResponse = {
  cooldown_seconds: number;  // 同一账号两次成功接受 SC 的最短间隔
  max_content_chars: number;
  max_content_bytes: number;
};
```

页面初始化时读取该接口显示发送间隔和输入上限，不要把默认秒数写死在前端。

```ts
type SCSubmitRequest = {
  sc_id: string;  // 客户端生成，8..128，仅字母、数字、_、-
  content: string;
};

type SCStatus = "accepted" | "pending" | "processing" |
  "replied" | "rejected" | "failed";

type SCStatusResponse = {
  sc_id: string;
  status: SCStatus;
  nickname: string;
  content: string;
  accepted_at: ISODateTime;
  queue_position: number | null;
  retry_after_seconds?: number | null;
  next_submit_at: ISODateTime | null;
  failure_code?: string | null;
  processing_started_at: ISODateTime | null;
  completed_at: ISODateTime | null;
  estimated_wait_seconds: number | null;
  reply: AIReply | null;
};
```

首次成功返回 HTTP `202`，`status="accepted"`。相同账号重放相同 `sc_id` 时仍返回 `202`，但 `status` 是数据库当前状态（例如 `pending/replied`），不重复排队或重新开始冷却。账号昵称由服务端令牌取得，客户端不能指定。

- `GET /sc/{sc_id}` 查询当前账号自己的状态；其他账号的同一 ID 统一按不存在处理。
- `GET /sc?limit=50` 返回当前账号最近的 SC（最多 100 条），用于页面刷新、跨标签页或 WebSocket 断线后的状态恢复。
- `DELETE /sc/history` 删除当前账号的 `replied/failed/rejected` 历史并返回 `204`；`pending/processing` 不会删除，以保证已接受 SC 继续履约。
- `GET /auth/profile/memory/export` 的导出结果新增 `sc_history`。关闭或清除长期人物记忆不会删除 SC 业务记录。

排队时 `queue_position` 从 1 开始，`estimated_wait_seconds` 是服务端粗略估计，不是承诺时间；以状态事件和后续查询为准。`replied` 状态会携带 `reply`，因此错过 WebSocket 广播后仍可恢复完整回复。

`retry_after_seconds` 和 `next_submit_at` 始终按该账号最近一次成功接受的 SC 由服务端计算。成功提交后立即开始倒计时；倒计时归零只代表业务冷却结束，仍可能受到提交速率或队列容量保护。前端应优先用响应中的剩余秒数校准，再用 `next_submit_at` 处理刷新和多标签页同步。

错误：

- `401`：未登录或令牌失效。
- `409`：该 `sc_id` 已属于其他账号；生成新的 ID，不得查询他人状态。
- `422`：格式/大小错误，或 `{ code: "sc_content_rejected", message, scope: "sc_submit" }` 表示敏感词或明确提示注入未通过入队前审核。不要展示内部命中规则。
- `429 sc_cooldown`：账号业务冷却，响应含 `retry_after_seconds`、`scope="sc_submit"` 和 `Retry-After`。
- `429 rate_limited` 且 `scope="sc_submit_rate"`：IP、账号或直播间提交防滥用额度耗尽；按返回时间禁用提交，不代表既有 SC 失败。
- `503`：`{ code: "sc_queue_full", message, retry_after_seconds }`，表示服务端尚未接受，不得自动重试风暴。

`202 accepted` 表示已持久化进入专用 FIFO 队列。开播期间服务端会绕过普通弹幕选择器直接处理；进程重启后 `pending` 保留，超时的 `processing` 会由租约自动恢复。

当前 SC v1 不代表真实支付，也不接受 `amount`、订单号或客户端优先级。未来支付版必须由服务端验证支付平台签名、订单状态、金额、账号归属和重复 webhook 后才能入队；前端不得提前发送或信任这些字段。

### 4.17 观众表情配置

`GET /emotes/config`，无需登录。后端只维护稳定 ID，不返回图片 URL 或文件路径。

```ts
type EmoteConfigResponse = {
  allowed_ids: string[];
  cooldown_seconds: number;
};
```

前端 JavaScript 按 `emote_id` 映射本地静态资源。未知 ID 使用通用占位或忽略，禁止将 ID 直接拼接成未经校验的 HTML/URL。

## 5. WebSocket `/danmaku`

### 5.1 客户端发送弹幕

```ts
type SendDanmaku = {
  nickname: string;
  message: string;
  danmakuID: string; // 客户端生成并在当前业务范围内保持唯一
  type?: string;     // 默认 "normal"
  sender_level?: number; // 默认 1
};
```

示例：

```json
{
  "nickname": "小爱",
  "message": "晚上好！",
  "danmakuID": "msg_01J2ABCXYZ",
  "type": "normal",
  "sender_level": 1
}
```

注意：

- 登录连接以账号数据库中的昵称为准，负载中的 `nickname` 会被覆盖。
- 游客连接使用负载中的昵称，并仅保持连接级身份。
- 负载中的 `account_id`、`user_id` 或 Token 字段不会用于身份认证。
- `nickname` 为 1-100 字符，`message` 为 1-500 字符，`danmakuID` 为 1-128 字符，`sender_level` 为整数 `1..10`。
- 单个 UTF-8 WebSocket 文本帧默认不得超过 4096 字节；超限会先发送 `rate_limited`，随后以 `1009` 关闭。
- 同一连接身份在默认 300 秒内重复提交相同 `danmakuID` 会收到 `duplicate_danmaku`，且不会进入弹幕池、数据库、关系或 AI 链路。

### 5.1.1 客户端发送观众表情

沿用 `/danmaku` WebSocket，但使用独立消息类型：

```ts
type SendViewerEmote = {
  type: "viewer_emote";
  emote_id: string;       // 必须来自 GET /emotes/config
  client_event_id: string; // 8..128，字母、数字、_、-
};
```

不发送 `nickname`、`message` 或 `danmakuID`。服务端从连接身份取得昵称；表情不会进入弹幕历史、选择、人格、关系、记忆、活动、数据库或 AI。

### 5.2 服务端事件联合类型

```ts
type ServerWebSocketEvent =
  | HistoryBatchEvent
  | DanmakuRealtimeEvent
  | ConfirmationEvent
  | DanmakuSelectedEvent
  | AIReplyEvent
  | SCStatusEvent
  | MoodUpdateEvent
  | StreamMetadataEvent
  | ViewerCountUpdateEvent
  | UserActivityEvent
  | StreamStatusEvent
  | StreamerActivityEvent
  | ViewerEmoteEvent
  | RateLimitedEvent
  | WebSocketErrorEvent;
```

前端必须按顶层 `type` 分发；未知类型应安全忽略并保留日志，方便插件或后续版本扩展。

### 5.3 `history_batch`

新连接建立后，如果服务内存在历史弹幕，会发送一次；历史为空时不发送。

```ts
type Danmaku = {
  nickname: string;
  message: string;
  type: string;
  timestamp: ISODateTime;
  danmakuID: string;
};

type HistoryBatchEvent = {
  type: "history_batch";
  messages: Danmaku[];
  count: number;
};
```

此事件与多数事件不同，没有 `data` 包装层。

### 5.4 `danmaku_realtime`

每条合法弹幕会广播给所有连接，包括发送者。

```ts
type DanmakuRealtimeEvent = {
  type: "danmaku_realtime";
  data: Danmaku;
};
```

### 5.4.1 `viewer_emote`

合法表情会广播给所有连接（包括发送者），新连接不补发历史：

```ts
type ViewerEmoteEvent = {
  type: "viewer_emote";
  data: {
    emote_id: string;
    nickname: string;
    viewer_id: string; // 临时连接 ID，不是 account_id
    client_event_id: string;
    timestamp: ISODateTime;
  };
};
```

按 `(viewer_id, client_event_id)` 去重。建议动画队列有上限；过载时丢弃最旧的纯展示动画，不得阻塞弹幕、SC 或主播活动状态渲染。

前端可使用 `danmakuID` 去重，不要在收到 `confirmation` 时再重复插入一次。

### 5.5 `confirmation`

仅发送给提交该弹幕的连接。

```ts
type ConfirmationEvent = {
  type: "confirmation";
  message: "弹幕发送成功" | string;
  timestamp: ISODateTime;
  danmaku_rate: number; // 最近一分钟服务收到的弹幕数
};
```

当前确认事件不包含 `danmakuID`；发送状态应通过本地发送队列和同 ID 的 `danmaku_realtime` 关联。

### 5.6 `danmaku_selected`

服务器选择某条弹幕准备生成 AI 回复时发送。该通知只发给触发当前选择循环的连接，但被选弹幕可能来自池中的其他连接，因此不要把它视作“我的弹幕已被选中”。

```ts
type DanmakuSelectedEvent = {
  type: "danmaku_selected";
  data: {
    danmaku_id: string;
    nickname: string;
    message: string; // 超过 50 字符时截断并加 "..."
    confidence: number;
    processing_time_ms: number;
  };
};
```

### 5.7 `ai_reply`

AI 回复完成后广播给所有连接。

```ts
type NormalAIReplyEvent = {
  type: "ai_reply";
  data: {
    danmaku_id: string;
    nickname: string;
    original_message: string;
    reply: AIReply;
    timestamp: ISODateTime;
  };
};
```

SC 回复完全复用现有结构，只增加可选来源字段：

```ts
type SCAIReplyEvent = {
  type: "ai_reply";
  data: {
    danmaku_id: string; // 等于 sc_id
    sc_id: string;
    source: "sc";
    nickname: string;
    original_message: string;
    reply: AIReply;
    timestamp: ISODateTime;
  };
};

type AIReplyEvent = NormalAIReplyEvent | SCAIReplyEvent;
```

渲染规则：`reply.emotions[i]` 与 `reply.sentences[i].emotion` 设计上应一一对应；前端应以 `sentences` 为主要文本来源，并对空数组做容错。

### 5.7.1 `sc_status`

SC 被领取、回复完成或最终失败时公共广播，不包含账号 ID、令牌或内部异常。

```ts
type SCStatusEvent = {
  type: "sc_status";
  data: {
    sc_id: string;
    status: "processing" | "replied" | "failed";
    nickname: string;
    content: string;
    failure_code: "empty_ai_reply" | "invalid_ai_reply" |
      "reply_generation_failed" | null;
    reply: AIReply | null; // replied 时必有；其他状态为 null
  };
};
```

`processing` 可用于显示“主播正在读取”；`replied` 必须携带可展示 `reply`，前端即使漏掉独立 `ai_reply` 也要用它补回正文并按 `sc_id` 去重；`failed` 是模型空响应、无效结构或调用异常在有限重试耗尽后的终态，不得显示成已回复。

### 5.8 `mood_update`

连接订阅后立即发送一次，之后默认每 1000ms 推送。

```ts
type MoodAxis = {
  value: number;       // 0..1
  label: string;
  description: string;
};

type MoodData = {
  mood: MoodAxis;
  darkness: MoodAxis;
  stress: MoodAxis;
  behavior: {
    reply_aggressiveness: number;
    ignore_probability: number;
  };
  streamer_name: string;
};

type MoodUpdateEvent = {
  type: "mood_update";
  data: MoodData;
  timestamp: ISODateTime;
};
```

### 5.9 `stream_metadata`

连接订阅后立即发送一次，之后默认每 5000ms 推送。

```ts
type StreamMetadataEvent = {
  type: "stream_metadata";
  data: StreamMetadata;
  timestamp: ISODateTime;
};
```

### 5.10 `viewer_count_update`

当前在线人数一次变化达到 5 人时立即推送；小幅变化仍会在下次完整 `stream_metadata` 中体现。

```ts
type ViewerCountUpdateEvent = {
  type: "viewer_count_update";
  data: {
    viewer_count: number;
    total_joined: number;
    total_left: number;
  };
  timestamp: ISODateTime;
};
```

### 5.11 `user_activity`

用户建立或断开连接时推送。

```ts
type UserActivityEvent = {
  type: "user_activity";
  data: UserActivity;
  timestamp: ISODateTime;
};
```

`data.extra` 当前可能包含 IP。前端只能忽略，不得展示或发送到第三方分析服务。
登录用户的 `data.nickname` 直接使用账号当前直播间昵称，改名后的新连接和离房事件会采用新昵称；游客仍使用由 WebSocket 连接 ID 派生的临时名称。前端不得从 `user_id` 或游客临时名称推断账号身份。

### 5.12 `stream_status`

后端在排期开播/下播边界主动推送；当前没有对应公开 HTTP 写接口。

```ts
type StreamStatusEvent = {
  type: "stream_status";
  data: {
    is_live: boolean;
    stream_status: "streaming" | "offline" | string;
    stream_duration_seconds: number;
    schedule_timezone: string;
    schedule_config_valid: boolean;
    schedule_errors: string[];
    current_stream_start_time: ISODateTime | null;
    current_stream_end_time: ISODateTime | null;
    next_stream_start_time: ISODateTime | null;
    next_stream_end_time: ISODateTime | null;
    daily_theme_id: string;
    daily_theme_name: string;
    daily_theme_date: string;
    theme_config_valid: boolean;
    theme_errors: string[];
    current_activity: StreamerActivity | null;
    activity_config_valid: boolean;
    activity_errors: string[];
  };
  timestamp: ISODateTime;
};
```

### 5.12.1 `streamer_activity`

明显活动切换的独立演出事件，不属于弹幕回复，没有 `danmaku_id`。静默切换不会发送此事件，前端仍可从后续元数据快照看到更高版本。

```ts
type StreamerActivityEvent = {
  type: "streamer_activity";
  data: {
    stream_session_id: string;
    version: number;
    previous: StreamerActivity;
    current: StreamerActivity;
    trigger_type: "time_driven" | "audience_influenced";
    changed_at: ISODateTime;
    emotions: string[];
    sentences: Array<{ emotion: string; text: string }>;
  };
  timestamp: ISODateTime;
};
```

以 `(stream_session_id, version)` 去重。先把 `current` 写入活动展示，再播放 `sentences`；事件不包含促成切换的账号、昵称、关系分或原始弹幕。断线期间漏掉事件时，以 `stream_metadata.current_activity` 快照恢复事实，不补播旧演出。

### 5.13 `error`

```ts
type WebSocketErrorEvent = {
  type: "error";
  code?: "invalid_fields" | "duplicate_danmaku" | "invalid_emote" |
    "invalid_emote_event" | "duplicate_emote" | "internal_error" | string;
  message: string;
  request_id?: string;
};
```

`invalid_emote` 表示 ID 不在后端目录；重新拉取 `/emotes/config`。`duplicate_emote` 表示该事件已经广播或处理，前端不得生成新 ID 自动补发。

### 5.14 `rate_limited`

```ts
type RateLimitedEvent = {
  type: "rate_limited";
  data: {
    code: "rate_limited" | "payload_too_large" | "server_overloaded" | string;
    message: string;
    retry_after_seconds: number;
    scope: "danmaku_send" | "danmaku_frame" | "ai_reply" |
      "viewer_emote" | string;
    action: "drop" | "cooldown" | "disconnect";
    request_id?: string;
  };
};
```

- `cooldown`：按 `scope` 禁用对应操作并倒计时。`danmaku_send` 表示本条未写入；`ai_reply` 可能表示弹幕已经成功广播但本轮未进入回复链，前端不得重发弹幕。
- `viewer_emote`：本次表情未广播；按 `retry_after_seconds` 禁用表情按钮，不影响弹幕输入或登录状态。
- `drop`：当前操作未进入后续 AI 处理；不要自动重发，避免重复弹幕风暴。
- `disconnect`：展示信息后等待服务端关闭；按关闭码决定是否重连。

### 5.15 推荐连接时序

```ts
const ws = new WebSocket(`${WS_BASE}/danmaku`);

ws.onmessage = (event) => {
  let payload: ServerWebSocketEvent;
  try {
    payload = JSON.parse(event.data);
  } catch {
    return;
  }

  switch (payload.type) {
    case "history_batch":
      // 初始化弹幕历史
      break;
    case "danmaku_realtime":
      // 按 danmakuID 去重后插入
      break;
    case "ai_reply":
      // 关联 data.danmaku_id 并播放 sentences
      break;
    case "viewer_emote":
      // 按 emote_id 渲染前端本地静态资源
      break;
    case "sc_status":
      // 按 data.sc_id 更新 SC 排队/读取状态
      break;
    case "mood_update":
      // 更新主播状态 UI
      break;
    case "stream_metadata":
    case "viewer_count_update":
      // 更新直播元信息
      break;
  }
};
```

建议前端实现带抖动的指数退避重连：

- `1008`：令牌无效，停止自动重连并要求重新登录。
- `1009`：发送帧过大，修正负载后由用户重新连接，不要原样重发。
- `1013`：握手频率、连接数或服务容量受限，至少等待上次已知 `retry_after_seconds`，未知时从 2 秒开始退避。
- `1001`：服务端优雅关闭。默认配置不会再因 120 秒没有客户端入站消息而关闭连接；只有运维显式启用应用层空闲超时/最大生命周期时才可能用于连接回收。
- 普通网络断开：使用带抖动的指数退避，恢复后重新取得元数据快照。

## 6. 可选展示 HTTP 接口

### 6.1 热门话题

`GET /memory/hot-topics?limit=5`

```ts
type HotTopicsResponse = {
  hot_topics: Array<{
    topic: string;
    heat: number;
  }>;
};
```

这是当前进程内的直播间短期话题，不是账号长期人物记忆。

### 6.2 直播间短期上下文

`GET /memory/context?limit=10`

```ts
type RoomMemoryContext = {
  recent_danmaku: Array<{
    nickname: string;
    content: string;
    timestamp: ISODateTime;
    sentiment: number;
    topics: string[];
  }>;
  hot_topics: Array<{ topic: string; heat: number }>;
  active_users: number;
  total_users: number;
  total_danmaku: number;
};
```

### 6.3 直播间话题讨论情况

`GET /memory/group-discussion?topic=<string>`

```ts
type GroupDiscussionResponse = {
  topic: string;
  exists: boolean;
  danmaku_count: number;
  user_count: number;
  heat: number;
  is_hot: boolean;
};
```

### 6.4 直播间对人格的聚合影响

`GET /memory/persona-impact`

```ts
type PersonaImpactAggregate = {
  mood: number;
  stress: number;
  darkness: number;
};
```

## 7. 内部管理与调试 HTTP 接口

这些接口全部使用独立管理员密钥，默认关闭。管理前端只能部署在受信任网络，不得把管理员密钥打包进公开网页；推荐由服务端后台或运维代理注入凭据。

### 7.1 配置

`GET /config`

```ts
type ConfigResponse = {
  success: boolean;
  config?: {
    server: {
      host: string;
      port: number;
      reload: boolean;
      log_level: string;
    };
    ai: {
      type: string;
      base_url: string;
      api_key: string; // 后端返回掩码；仍不得进入公开前端状态、日志或构建产物
      default_model: string;
      qa_selector_model: string | null;
      qa_selector_timeout: number;
      temperature: number;
      streaming: boolean;
      timeout: number;
    };
    danmaku: {
      max_history: number;
      message_rate_limit: number;
      enable_filter: boolean;
      time_window_minutes: number;
      frequency_threshold: number;
      max_unread_pool_size: number;
      selector_weights: {
        content_relevance: number;
        sender_level: number;
        emotional_match: number;
        timeliness: number;
        persona_consistency: number;
        [key: string]: number;
      };
      memory_max_user_danmaku: number;
      memory_topic_decay_time: number;
      memory_user_inactive_time: number;
      memory_time_window_size: number;
      memory_max_topic_keywords: number;
      memory_max_topic_memories: number;
    };
    persona: {
      streamer_name: string;
      theme: string;
      initial_mood: number;
      initial_darkness: number;
      initial_stress: number;
      reply_aggressiveness: number;
      ignore_probability: number;
      mood_push_interval_ms: number;
      enable_mood_push: boolean;
    };
    plugins: {
      enabled_plugins: string[];
      plugin_dir: string;
    };
    memory: {
      enabled_by_default: boolean;
      retention_days: number;
      max_text_length: number;
      recent_fragment_limit: number;
      retrieval_limit: number;
      compact_after_fragments: number;
      summary_max_chars: number;
      importance_half_life_days: number;
      max_archived_fragments: number;
    };
    custom: Record<string, unknown>;
  };
  message?: string;
};
```

`PUT /config`

```ts
type ConfigUpdateRequest = {
  key: string; // 顶层配置节，例如 "memory"
  value: Record<string, unknown>;
};
```

返回 `ConfigResponse`。该接口会写入 `config.json`；不是普通用户偏好接口。

### 7.2 插件

`GET /plugins`

```ts
type PluginInfo = {
  name: string;
  version: string;
  description: string;
  enabled: boolean;
};

type PluginListResponse = { plugins: PluginInfo[] };
```

`POST /plugins/{plugin_name}/enable`

`POST /plugins/{plugin_name}/disable`

```ts
type PluginMutationResponse = {
  success: boolean;
  plugin: string;
};
```

### 7.3 人格重置和调试

`POST /persona/reset` → `{ "success": boolean }`。会修改并持久化主播人格状态。

`GET /persona/impact/debug`

```ts
type PersonaImpactDebugResponse = {
  impact_analyzer: {
    debug_mode: boolean;
    analysis_count: number;
    max_history: number;
    max_single_change: number;
    boundaries: Record<"mood" | "stress" | "darkness", {
      min: number;
      max: number;
    }>;
  };
  dynamics: {
    baseline: { mood: number; stress: number; darkness: number };
    max_step: Record<string, number>;
    recovery_rate: Record<string, number>;
    last_update_at: ISODateTime | null;
    last_snapshot: Record<string, unknown> | null;
  };
};
```

`GET /persona/impact/history?limit=10`

```ts
type ImpactAnalysis = {
  danmaku_content: string;
  current_mood: number;
  current_stress: number;
  current_darkness: number;
  emotional_tone: "positive" | "negative" | "neutral" | "mixed" | string;
  content_intensity: number;
  context_relevance: number;
  mood_impact: number;
  stress_impact: number;
  darkness_impact: number;
  reasoning: string;
  key_factors: string[];
  clamped_mood: number;
  clamped_stress: number;
  clamped_darkness: number;
  timestamp: ISODateTime;
};

type ImpactHistoryResponse = {
  history: ImpactAnalysis[];
  total_count: number;
};
```

`POST /persona/impact/analyze`

请求体：`{ "content": string }`。成功返回 `ImpactAnalysis`；缺少内容返回 `400`；AI 与回退均失败返回 `500`。该调用可能访问外部模型，延迟较高。

`POST /persona/impact/debug-mode?enabled=true`

查询参数 `enabled: boolean`；返回 `{ "debug_mode": boolean }`。

`GET /persona/events/debug`

```ts
type PersonaEventDebugResponse = {
  running: boolean;
  tick_seconds: number;
  current_danmaku_rate: number;
  last_activity_at: ISODateTime;
  processed_events: number;
  recent_events: Array<Record<string, unknown>>;
  reserved_event_types: string[];
};
```

### 7.4 连接、弹幕池和推送器

`GET /connections`

```ts
type ConnectionsResponse = {
  total_connections: number;
  connections: Array<{
    id: string;
    client_ip: string | null;
    user_agent: string | null;
    created_at: ISODateTime;
    retry_count: number;
    identity_type: "authenticated" | "guest";
    current_nickname: string;
  }>;
  ip_distribution: Record<string, number>;
};
```

包含隐私字段，不得进入公开前端。

`GET /danmaku/pool`

```ts
type PoolItem = {
  id: string;
  nickname: string;
  message: string;
  timestamp: ISODateTime;
  status: "unread" | "read" | "selected" | "replied" | "expired";
  sender_level: number;
  priority: number;
  reply_count: number;
  content_score: number;
  emotional_match_score: number;
  reply_content: string; // JSON 字符串或空串
};

type PoolStats = {
  unread_count: number;
  read_count: number;
  replied_count: number;
  total_received: number;
  total_replied: number;
  total_expired: number;
};

type DanmakuPoolResponse = {
  stats: PoolStats;
  unread_items: PoolItem[];
  read_items: PoolItem[];
  config: {
    time_window_minutes: number;
    max_pool_size: number;
    frequency_threshold: number;
  };
};
```

`GET /danmaku/selector/stats`

```ts
type SelectorStats = {
  total_selections: number;
  last_selection_time: ISODateTime | null;
  weights: Record<string, number>;
};
```

`GET /mood/pusher/stats`

```ts
type MoodPusherStats = {
  total_pushes: number;
  start_time: ISODateTime | null;
  last_push_time: ISODateTime | null;
  subscriber_count: number;
  is_running: boolean;
  push_interval_ms: number;
  enable_push: boolean;
};
```

`GET /stream/metadata/stats`

```ts
type StreamMetadataStats = {
  total_pushes: number;
  start_time: ISODateTime | null;
  last_push_time: ISODateTime | null;
  subscriber_count: number;
  is_running: boolean;
  push_interval_ms: number;
  enable_push: boolean;
  current_metadata: StreamMetadata;
};
```

### 7.5 数据库查询与导出

```ts
type DanmakuRecord = {
  id: number;
  danmaku_id: string;
  nickname: string;
  message: string;
  client_ip: string | null;
  sender_level: number;
  timestamp: ISODateTime;
  created_at: ISODateTime;
};

type ReplyRecord = {
  id: number;
  danmaku_record_id: number | null;
  danmaku_id: string;
  danmaku_nickname: string;
  danmaku_message: string;
  ai_reply: AIReply;
  ai_emotions: string[] | null;
  mood_before: number | null;
  stress_before: number | null;
  darkness_before: number | null;
  mood_impact: number;
  stress_impact: number;
  darkness_impact: number;
  mood_after: number | null;
  stress_after: number | null;
  darkness_after: number | null;
  emotional_tone: string | null;
  content_intensity: number | null;
  context_relevance: number | null;
  analysis_reasoning: string | null;
  key_factors: string[] | null;
  selected_at: ISODateTime;
  created_at: ISODateTime;
};
```

`GET /database/stats`

返回：`{ database_path: string, total_danmaku: number, total_replies: number }`。

`GET /database/danmaku?limit=100&offset=0&start_time=<ISO>&end_time=<ISO>`

返回：`{ records: DanmakuRecord[], total: number }`。

`GET /database/replies?limit=100&offset=0&start_time=<ISO>&end_time=<ISO>`

返回：`{ records: ReplyRecord[], total: number }`。

`GET /database/export?start_time=<ISO>&end_time=<ISO>`

```ts
type DatabaseExportResponse = {
  export_time: ISODateTime;
  summary: {
    total_danmaku: number;
    total_replies: number;
    time_range: {
      start: ISODateTime | null;
      end: ISODateTime | null;
    };
  };
  danmaku_records: DanmakuRecord[];
  reply_records: ReplyRecord[];
  danmaku_with_replies: Array<DanmakuRecord & { replies: ReplyRecord[] }>;
};
```

这些接口包含用户消息和 IP，仅用于受信任后台。

### 7.6 短期记忆统计

`GET /memory/stats`

预期类型：

```ts
type RoomMemoryStats = {
  total_users: number;
  total_danmaku: number;
  total_topics: number;
  hot_topics_count: number;
};
```

当前实现遗漏了 `await`，运行时可能返回 500；前端暂时不要接入，修复后再启用。

### 7.7 情绪管理调试

`GET /emotion/stats`

```ts
type EmotionStats = {
  total_emotions: number;
  category_counts: Record<string, number>;
  recent_emotions: string[];
  recent_frequency: Record<string, number>;
  randomness: number;
  available_emotions: string[];
};
```

`GET /emotion/info/{emotion_name}`

```ts
type EmotionInfo = {
  name: string;
  category: "positive" | "negative" | "neutral" | "dark" | "playful" | string;
  weight: number;
  mood_bonus: number;
  darkness_bonus: number;
  stress_bonus: number;
  description: string;
};
```

不存在时返回 `404`。

`GET /emotion/randomness` → `{ "randomness": number }`。

`POST /emotion/randomness?randomness=0.3`

参数在查询字符串，不在 JSON Body；范围 `0..1`。返回 `{ success: boolean, randomness: number }`。

`POST /emotion/select?mood=0.7&stress=0.2&darkness=0.1&count=2`

四个参数都在查询字符串；前三项范围 `0..1`，`count` 范围 `1..5`。返回 `{ selected_emotions: string[] }`。

`POST /emotion/reset-history`

返回 `{ success: boolean, message: string }`，会重置服务内近期情绪动作历史。
