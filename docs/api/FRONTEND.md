# KAngel Server 前端接口完整契约

本文档面向前端开发 agent，覆盖当前后端全部 HTTP 路由、WebSocket 收发事件、认证方式、字段类型、状态码和接入时序。

> 对应后端状态：2026-08-10。WebSocket 事件的权威清单见 [`WEBSOCKET_EVENTS.md`](WEBSOCKET_EVENTS.md)；本文保留字段类型、HTTP 契约与接入细节。

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
| 产品核心 | `/auth/**`、`/stream/metadata`、`/persona/state`、`/emotion/list`、`/danmaku` WebSocket | 登录、账号设置、直播页和弹幕互动 |
| 可选展示 | `/status`、`/stream/activities`、`/memory/context` | 状态页、直播间辅助信息 |
| 内部管理/调试 | `/config`、`/plugins/**`、`/connections`、`/database/**`、`/persona/impact/**`、大部分 `/memory/**` 和 `/emotion/**` 写接口 | 仅后台工具，不应进入公开用户前端 |

内部管理/调试接口默认关闭并返回 `404`。仅当服务端显式设置 `ADMIN__ENABLED=true` 且配置独立 `ADMIN__API_KEY` 后，才接受 `Authorization: Bearer <admin-key>` 或 `X-Admin-Key`；普通用户访问令牌和 Cookie 不能调用。`/config` 中密钥使用掩码序列化，前端仍不得保存或记录管理响应。

## 3. 认证与登录状态

### 3.1 登录结果

注册和登录成功后，服务端同时：

1. 在 JSON 中返回不透明 `access_token`；
2. 设置同值的 `HttpOnly` access Cookie，默认名为 `kangel_access_token`；
3. 设置仅随 `POST /auth/refresh` 发送的 `HttpOnly` refresh Cookie，默认名为 `kangel_refresh_token`。

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

JSON 中的 `access_token` 仅为 CLI、原生客户端和跨域调试的 Bearer 方式保留。浏览器前端必须忽略该字段，不得把 token 写入 `localStorage`、`sessionStorage`、IndexedDB 或持久化状态。推荐浏览器只使用 `HttpOnly` Cookie。若使用 Cookie，HTTP 请求需要：

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

## 4. 产品核心 HTTP 接口

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

当前提供登出和修改密码接口；忘记密码仍需联系管理员人工处理。

### 4.3 刷新浏览器会话

`POST /auth/refresh`

认证：只接受浏览器自动携带的 refresh Cookie；没有请求体，不能用 Bearer 或 access token 刷新。

```ts
type AuthRefreshResponse = {
  account: Account;
  expires_at: ISODateTime; // 新 access token 的到期时间
};
```

成功：`200 AuthRefreshResponse`，并重新设置两个 `HttpOnly` Cookie。响应不包含 access 或 refresh token。refresh Cookie 默认有效期 720 小时、Path 是 `/auth/refresh`，且为一次性令牌：每次成功刷新都会轮换，旧值立即失效。

`POST /auth/logout`

退出当前浏览器/客户端会话。服务端撤销请求中携带的 access/refresh 会话令牌，并返回两个 `Max-Age=0` 的 `HttpOnly` Cookie。接口幂等，即使令牌已经过期或缺失也返回 `204`；不会影响同一账号在其他设备上的登录。

失败：`401` 表示 refresh Cookie 缺失、无效、过期或已经使用。`401` 以外的网络错误、`429` 或 `5xx` 不代表用户退出登录。

浏览器登录态恢复只允许一次刷新和一次 profile 重试，避免循环：

```ts
async function restoreSession(reconnectDanmaku: () => void): Promise<Account | null> {
  let profile = await fetch("/auth/profile", { credentials: "include" });
  if (profile.status === 401) {
    const refresh = await fetch("/auth/refresh", {
      method: "POST",
      credentials: "include",
    });
    if (refresh.status === 401) return null;
    if (refresh.status !== 200) throw new Error("refresh request failed");
    profile = await fetch("/auth/profile", { credentials: "include" });
  }
  if (profile.status === 401) return null;
  if (profile.status !== 200) throw new Error("profile request failed");
  const account = await profile.json() as Account;
  reconnectDanmaku(); // 先关闭旧连接，再 new WebSocket("/danmaku")
  return account;
}
```

只有 refresh 或这次 profile 重试也返回 `401` 时才清除登录 UI、要求重新登录。WebSocket 重连必须在 profile 重试成功后执行，确保握手携带新 access Cookie。

### 4.4 当前登录账号

`GET /auth/profile`

认证：登录账号。返回当前 Cookie 或 Bearer 令牌对应的 `Account`，不返回访问令牌。
浏览器应在页面启动时以 `credentials: "include"` 调用此接口恢复登录态；首次 `401` 时按 4.3 调用 refresh 并重试 profile 一次，只有 refresh 或重试仍为 `401` 才表示本地展示缓存应清除。`429`、`408`、`504` 或网络错误均不得视为退出登录。

### 4.5 修改昵称

`PATCH /auth/profile/nickname`

认证：登录账号。

```ts
type NicknameUpdateRequest = {
  nickname: string; // 1..100，禁止控制字符
};
```

成功：`200 Account`。

作用：原子结束旧昵称版本并创建新版本；`account_id`、关系和长期记忆不变。该账号已有 WebSocket 会立即使用新昵称。

### 4.6 查询昵称历史

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

### 4.7 删除旧昵称版本

`DELETE /auth/profile/nickname-history/{version}`

认证：登录账号。路径参数 `version: integer >= 1`。

成功：`204 No Content`。

错误：`404` 版本不存在；`409` 当前昵称版本不可删除。

作用：物理删除旧昵称版本。当前昵称不能通过此接口删除。

### 4.8 人物记忆类型

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

### 4.9 查询人物记忆

`GET /auth/profile/memory`

认证：登录账号。

成功：`200 AccountMemoryResponse`。最多返回近期活跃对话和话题摘要；没有记忆时关系为 `null`、数组为空。

### 4.10 导出人物记忆

`GET /auth/profile/memory/export`

认证：登录账号。

```ts
type AccountMemoryExportResponse = AccountMemoryResponse & {
  nickname_history: NicknameHistoryEntry[];
  exported_at: ISODateTime;
};
```

与普通查询不同，导出包含允许保留的活跃及归档对话。响应是 JSON，不是文件流；前端如需下载，应自行创建 `Blob`。

### 4.11 开启或退出长期记忆

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

### 4.12 清除已有记忆

`DELETE /auth/profile/memory`

认证：登录账号。

成功：`204 No Content`。

只清除已有记忆，记忆开关保持不变；若希望清除后不再写入，应调用偏好接口关闭长期记忆。

### 4.13 服务状态

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

### 4.14 当前人格状态

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

### 4.15 当前直播元数据

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
  special_date_theme: SpecialDateTheme | null;
  stream_session_id: string | null;      // 等于 current_stream_start_time；下播为 null
  session_theme: SessionTheme | null;    // 本场冻结主题；配置热更新不改写
  daily_stream_plan: DailyStreamPlan | null;
  current_mainline_beat: MainlineBeat | null;
  mainline_config_valid: boolean;
  mainline_errors: string[];
  current_activity: StreamerActivity | null;
  activity_config_valid: boolean;
  activity_errors: string[];
  streamer_idle_state: StreamerIdleState | null;
  extra: Record<string, unknown>;
};

type SessionTheme = {
  id: string;
  name: string;
  date: string; // schedule_timezone 下的 YYYY-MM-DD
};

type DailyStreamPlan = {
  profile_id: string;
  version: number;   // Plan 版本；本场内恒定
  direction: string; // 一句话的本场方向，可展示
};

type MainlineBeat = {
  id: string;
  kind: "opening" | "mainline" | "detour" | "transition" | "wrap_up";
  label: string;
  return_to: string | null; // 仅 detour 有值
  version: number;          // 与 Plan 版本相互独立，单调递增
  started_at: ISODateTime;
};

type SpecialDateTheme = {
  id: string;
  name: string;
  title: string;
  frontend_theme: string | null;
  date: string; // schedule_timezone 下的 YYYY-MM-DD
};

type StreamerActivity = {
  activity_id: string;
  category: string;
  display_name: string;
  object_name: string;
  started_at: ISODateTime;
  version: number;
};

type StreamerIdleState = {
  idle_state: string;
  idle_text: string;
  frontend_animation: string;
  background_music_hint: string | null;
  priority: number;
  version: number;
};
```

`is_live` 和 `stream_status` 由后端按排期计算，不代表服务器进程是否运行。每日主题及当前具体活动均由后端选择；前端必须直接消费这些字段，不自行按浏览器时区、本地随机数或主题名称推算。下播时 `current_activity=null`。

`special_date_theme` 是当天特殊日期的安全展示摘要；为 `null` 时沿用普通每日主题。前端可根据 `frontend_theme` 点缀页面，未知 ID 或缺失值必须回退普通展示，不能自行推断节日或向用户展示服务端提示词/人格 bias。

主线字段（P26）在 `STREAM__MAINLINE_ENABLED=false` 时恒为 `null`/`true`/`[]`，前端必须把它们当作可缺失字段处理。`daily_stream_plan` 只暴露 `profile_id`/`version`/`direction`，完整节拍图与每个 beat 的 `objective` 不对外。`current_mainline_beat.version` 独立于 Plan 版本单调递增，是唯一的去重依据；断线恢复以本快照为准，不补播历史 beat。`mainline_config_valid=false` 时 `mainline_errors` 给出降级原因（例如 `mainline_snapshot_unreadable: ...`），此时主线不再推进，但排期、主题与活动事实照常可用，前端只需隐藏主线相关展示。

`daily_theme_*` 与 `session_theme` 的关系取决于服务端灰度开关 `STREAM__MAINLINE_THEME_PROJECTION_ENABLED`：默认关闭时 `daily_theme_*` 跟随实时配置，直播中改配置会让它与 `session_theme` 短暂不一致；开启后直播期间 `daily_theme_*` 由本场冻结快照投影，两者始终一致。**前端应始终以 `session_theme` 作为"本场是什么主题"的判据**，`daily_theme_*` 仅作兼容展示。

### 4.16 最近进出活动

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

### 4.17 可用情绪动作

`GET /emotion/list`

```ts
type EmotionListResponse = {
  available_emotions: string[];
};
```

当前共 34 个值，按表现语义分组如下：

- 正向：`开心`、`喜欢`、`得意`、`卖萌`、`兴奋`、`温柔`
- 亲密/表现：`害羞`、`撒娇`、`自恋`、`做作`、`帅气`、`打招呼`
- 负向：`生气`、`委屈`、`无语`、`尴尬`、`伤心`、`焦虑`、`疲惫`、`厌恶`、`害怕`
- 强烈/阴暗：`阴暗`、`暴怒`、`嘲讽`、`崩溃`、`冷笑`、`震惊`
- 中性/动作：`眼神飘忽`、`祷告`、`认真`、`思考`、`惊讶`、`搞怪`、`宅系`

目录粒度以「观众能否看出差别」为准：每个标定都要对应一段独立动画。`亢奋`、`大笑`、
`笑着挥手`、`困倦`、`毒舌` 曾在列表内，但它们的画面与 `兴奋`、`开心`、`疲惫`、`嘲讽`
逐字节相同，已于 34 值版本移除；补上专属素材后可以再加回来。

前端应以接口返回值为准，并为未知值提供通用动作或静态立绘兜底。

### 4.18 提交与查询 SC

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

### 4.19 本站主播管理状态

`GET /moderation/status`，认证：登录账号。返回当前账号在本服务内的禁言状态，供
页面刷新或 WebSocket 重连后恢复；游客没有跨连接的 HTTP 查询能力。

```ts
type ModerationStatusResponse = {
  muted: boolean;
  mute_until: ISODateTime | null;
  pending: boolean;
  admin_review_required: boolean;
  retry_after_seconds: number;
};
```

响应不会包含 toxicity、confidence、reason_code、账号内部标识、IP 或违规原文。
未登录返回 `401`；读取过于频繁返回统一 `429`，前端按响应中的
`Retry-After` 倒计时，不要清除登录态。

### 4.20 观众表情配置

`GET /emotes/config`，无需登录。后端只维护稳定 ID，不返回图片 URL 或文件路径。

```ts
type EmoteConfigResponse = {
  allowed_ids: string[];
  cooldown_seconds: number;
};
```

前端 JavaScript 按 `emote_id` 映射本地静态资源。未知 ID 使用通用占位或忽略，禁止将 ID 直接拼接成未经校验的 HTML/URL。

### 4.21 赞助入口配置

`GET /sponsor/config`，无需登录。页面最底部赞助入口的展示元数据，文案由后端驱动，
改文案不需要重新发布前端。

```ts
type SponsorConfigResponse = {
  enabled: boolean;        // 总开关；false 时前端不渲染入口
  list_enabled: boolean;   // 感谢墙是否可见（总开关与名单同步都开启才为 true）
  platform_name: string;   // 展示用渠道名，如「爱发电」
  platform_url: string;    // 外链地址，可能为空字符串
  notice_text: string;     // 成本说明文案
};
```

**赞助不授予任何功能权益。** 不给权限、不给 SC 额度、不给徽章、不改排队优先级，
也不进入人格与记忆链路；它只影响页面上的展示。前端文案必须写明这一点，避免观众
误以为弹幕、SC 或 AI 回复需要付费。

响应永不包含收款平台凭据（`afdian_user_id` / `afdian_token`），这两项仅存在于服务端
配置中，也不会出现在 admin `GET /config` 里。`platform_url` 为空时不要渲染跳转按钮；
渲染时必须用 `target="_blank"` + `rel="noopener noreferrer"`。

### 4.22 赞助者名单

`GET /sponsors`，无需登录。感谢墙数据，**仅昵称**。

```ts
type SponsorListResponse = {
  enabled: boolean;              // 与 4.21 的 list_enabled 一致
  total_count: number;           // 去除隐藏项后的总人数
  updated_at: ISODateTime | null;// 最后一次同步成功时间
  sponsors: { display_name: string }[];
};
```

脱敏承诺：响应里**没有金额、没有档位、没有排名、没有平台用户 ID、没有订单号**。
`sponsors` 的顺序与金额无关（服务端按平台用户 ID 的哈希打散，同一批数据顺序稳定），
前端不要再按任何字段排序，也不要把出现位置解释成贡献大小。

`enabled=false`（总开关关闭或名单同步未开启）时固定返回
`{"enabled": false, "total_count": 0, "updated_at": null, "sponsors": []}`，
前端按空名单渲染占位文案即可，不要重试。`total_count` 可能大于 `sponsors.length`
（后端有返回上限），此时展示「已显示前 N 位」之类的说明，不要自行分页——没有分页接口。

`display_name` 已在服务端清理控制字符并截断，但仍属于用户输入：只能作为文本节点渲染，
禁止拼接进 HTML。想匿名的赞助者会显示统一占位名（默认「匿名赞助者」）；已经上墙的人
联系主播即可撤下，下一次同步生效。

名单同步失败时接口会继续返回上一次成功的数据（只是 `updated_at` 变旧），因此前端不需要
为「暂时读不到」设计特殊状态，按普通网络错误提示并允许重试即可。此链路是纯旁路，
任何失败都不影响弹幕、SC、AI 回复与鉴权。

### 4.23 Sponsor Fund Transparency

`GET /sponsor/transparency`，无需登录。该接口由 `SPONSOR__TRANSPARENCY_ENABLED` 独立控制，
关闭时返回 `enabled=false` 与空月份；不会关闭或改变感谢墙。收入只来自爱发电成功订单同步，
支出由维护者在管理接口登记，保存/编辑/作废后下一次请求即可看到，无需重启或重新构建前端。

```ts
type SponsorTransparencyResponse = {
  enabled: boolean;
  currency: "CNY";
  received_total_cents: number;
  spent_total_cents: number;
  remaining_cents: number;
  supporter_count: number;
  updated_at: ISODateTime | null;
  months: {
    month: string; // YYYY-MM
    opening_balance_cents: number;
    received_cents: number;
    spent_cents: number;
    closing_balance_cents: number;
    expenses: { category: string; title: string; amount_cents: number; note: string | null }[];
  }[];
};
```

公开金额全部是项目聚合口径，不可关联到具体赞助者。响应永远不包含 `order_key`、订单号、
平台用户 ID、昵称、订单备注或支付信息。公开文案应说明：收入数据自动同步自爱发电成功订单，
资金用途由项目维护者手动登记。透明数据失败时只隐藏资金区，感谢墙、赞助入口和直播主链继续可用。

管理端（均需 `x-admin-key`）新增：

- `GET /admin/sponsor/finance/stats`
- `POST /admin/sponsor/finance/sync`
- `GET /admin/sponsor/expenses?include_void=true`
- `POST /admin/sponsor/expenses`
- `PUT /admin/sponsor/expenses/{entry_id}`
- `POST /admin/sponsor/expenses/{entry_id}/void`

支出金额使用整数分，类别固定为 `ai_api`、`server`、`network`、`domain`、`cdn`、`software`、
`hardware`、`other`；后台不提供人工修改收入总额，错误支出只能作废而不能删除。

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
  | StreamerIdleStateEvent
  | StreamerBeatEvent
  | StreamMainlineBeatEvent
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

`messages` 同时包含普通弹幕和已接受的 SC。前端按 `Danmaku.type` 区分：

- `normal`：普通弹幕。
- `sc`：SC 公共展示消息，`danmakuID` 等于 `sc_id`。

SC 在服务端首次接受时就进入该历史，不等待主播回复；HTTP 幂等重放不会产生重复项。该历史为当前服务进程内的有界直播历史，服务重启后不会作为跨场回放恢复。

### 5.4 `danmaku_realtime`

每条合法弹幕会广播给所有连接，包括发送者。

首次成功接受的 SC 也使用此事件实时广播，其中 `data.type="sc"`。这只代表直播间展示，不表示 SC 已被主播回复；回复进度仍以 `sc_status` 为准。

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

type AIReplyEvent = NormalAIReplyEvent | SCAIReplyEvent | DirectorAIReplyEvent;
```

主线 Director 的主动台词（P26）也走同一个事件，但它不是对某条弹幕的回复，因此**没有** `danmaku_id` / `nickname` / `original_message`：

```ts
type DirectorAIReplyEvent = {
  type: "ai_reply";
  data: {
    source: "stream_director";
    stream_session_id: string;
    beat_version: number;
    reply: AIReply;
    timestamp: ISODateTime;
  };
};
```

前端必须按 `data.source` 判别再取字段：主动台词是最低优先级演出，建议在已有回复正在播放或排队时直接丢弃，并在收到普通/SC 回复时清掉队列里尚未播放的主动台词。该事件仅在 `STREAM__DIRECTOR_ENABLED=true` 且 `STREAM__DIRECTOR_PERFORMANCE_ENABLED=true` 时可能出现。

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
    special_date_theme: SpecialDateTheme | null;
    stream_session_id: string | null;
    session_theme: SessionTheme | null;
    daily_stream_plan: DailyStreamPlan | null;
    current_mainline_beat: MainlineBeat | null;
    mainline_config_valid: boolean;
    mainline_errors: string[];
    current_activity: StreamerActivity | null;
    activity_config_valid: boolean;
    activity_errors: string[];
    streamer_idle_state: StreamerIdleState | null;
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

### 5.12.2 `streamer_idle_state`

主播待机外显状态的独立增量事件，不属于弹幕回复或活动切换，也不占用 AI 队列。

```ts
type StreamerIdleStateEvent = {
  type: "streamer_idle_state";
  data: StreamerIdleState;
  timestamp: ISODateTime;
};
```

前端按 `version` 去重，仅使用 `idle_state`/`frontend_animation` 映射演出资源；未知状态或动画使用静态默认展示。断线恢复以 `stream_metadata.streamer_idle_state` 快照为准。`idle_text` 是演出文案，不应伪装成主播主动说出的弹幕回复。

### 5.12.3 `streamer_beat`

低频、可丢弃的主播微动作演出。它不属于 `ai_reply`、普通弹幕或 SC：后端只会在开播、低弹幕热度、没有 SC 排队和 AI 工作等待时发送；断线、拥塞或繁忙期间漏掉的事件不会补播。

```ts
type StreamerBeatEvent = {
  type: "streamer_beat";
  data: {
    stream_session_id: string;
    version: number;
    activity_version: number;
    beat_type:
      | "activity_progress"
      | "glance_chat"
      | "short_pause"
      | "compose_mood"
      | "invite_participation"
      | "natural_close";
    display_text: string;
    occurred_at: ISODateTime;
  };
  timestamp: ISODateTime;
};
```

以 `(stream_session_id, version)` 去重。`display_text` 是短演出提示，可展示为独立状态条或轻量气泡；不要写入弹幕历史、不要伪装成主播回复、不要由此更新 SC 或回复完成状态。未知 `beat_type` 保留文本或安全忽略即可。活动切换优先级更高，当前活动快照仍以 `stream_metadata.current_activity` 为准。

### 5.12.4 `stream_mainline_beat`

本场主线节拍推进时的增量事件（P26）。它只声明"直播走到了哪一段"，不含台词、不含动画、不占 AI 队列，也不是弹幕回复。仅在 `STREAM__MAINLINE_ENABLED=true` 且 Director 实际改写事实时发送（`STREAM__DIRECTOR_MODE=shadow` 只做影子比对，不会发）。

```ts
type StreamMainlineBeatEvent = {
  type: "stream_mainline_beat";
  data: {
    stream_session_id: string;
    plan_version: number;
    beat: MainlineBeat;
    activity_version: number;
    trigger_source: "stream_director" | string;
    reason_code: string; // 例如 OPENING_COMPLETE / ROOM_QUIET / SCHEDULE_WRAP_UP
  };
  timestamp: ISODateTime;
};
```

以 `(stream_session_id, beat.version)` 去重，并且必须**丢弃版本号不大于当前值的事件**——节拍只会前进，乱序或重放的旧版本不得回退展示。断线期间漏掉的推进不补播，重连后以 `stream_metadata.current_mainline_beat` 快照恢复。`beat.label` 可直接展示（例如状态条"正在推进：打通第三关"），`kind` 可用于选择展示样式，未知 `kind` 按 `mainline` 兜底。同一场次内 `plan_version` 恒定；若它发生变化说明是新的一场，应整体重置主线展示。

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

### 5.15 `streamer_moderation`

主播管理分析在原始弹幕广播后异步执行。事件只发送给触发该弹幕的连接，
不包含 toxicity、confidence、reason_code、账号 ID、IP 或违规原文。

```ts
type StreamerModerationEvent = {
  type: "streamer_moderation";
  data: {
    action: "warning" | "timeout" | "admin_review" | "muted" | "pending";
    scope: "self";
    muted: boolean;
    mute_until: ISODateTime | null;
    retry_after_seconds: number;
    message: string;
    moderation_id: string | null;
    timestamp: ISODateTime;
  };
};
```

主播针对越界内容的公开回应仍使用 `ai_reply`，但其 `data.source` 为
`"moderation"`，携带 `moderation_id` 和 `reply`，不携带被处理弹幕的
`original_message`。模型失败时后端使用固定设界模板，禁言状态不会因此撤销。

登录用户可通过 `GET /moderation/status` 恢复自己的本站禁言状态；游客只在当前
WebSocket 连接范围内保留行为状态。

### 5.16 推荐连接时序

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
      // 先看 data.source：普通/SC 按 data.danmaku_id 关联；
      // "stream_director" 是无来源弹幕的主动台词，按最低优先级播放或丢弃
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
    case "stream_mainline_beat":
      // 仅在 data.beat.version 大于当前值时更新主线展示
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

### 6.1 直播间短期上下文

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
  active_users: number;
  total_users: number;
  total_danmaku: number;
};
```

### 6.2 直播间话题讨论情况

`GET /memory/group-discussion?topic=<string>`

```ts
type GroupDiscussionResponse = {
  topic: string;
  exists: boolean;
  danmaku_count: number;
  user_count: number;
};
```

### 6.3 直播间对人格的聚合影响

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
};
```

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

### 7.8 赞助名单同步状态

`GET /admin/sponsor/stats`，认证：管理接口密钥。用于确认轮询是否健康。

```ts
type SponsorSyncStatsResponse = {
  enabled: boolean;
  sync_enabled: boolean;
  credentials_configured: boolean;  // 只报是否配置，不回显凭据本身
  sponsor_count: number;
  hidden_count: number;
  anonymous_count: number;
  synced_count: number;
  consecutive_failures: number;
  last_success_at: ISODateTime | null;
  last_attempt_at: ISODateTime | null;
  last_error_code: string | null;   // 受控错误码，如 network_error / api_error
};
```

不返回凭据、不返回单人金额、不返回平台用户 ID。`ADMIN__ENABLED=false` 时该路径返回
`404`（管理接口整体隐藏），密钥缺失或错误返回 `403`。

### 7.9 主播工作记忆（Prompt RAM）

`GET /admin/prompt-ram`，认证：管理接口密钥。

```ts
type PromptRamResponse = {
  enabled: boolean;
  stats: {
    harvested: number; rejected: number; fulfilled: number;
    superseded: number; expired: number; evicted: number; errors: number;
    open_entries: number; total_entries: number;
    last_harvest_at: ISODateTime | null;
  };
  entries: Array<{
    entry_id: string;
    kind: "awaiting_viewer" | "owed_followup" | "standing_idea" | "holding_back";
    state: "open" | "fulfilled" | "superseded";
    note: string;                     // 模型生成的念头原文，已消毒
    target_nickname: string;          // 仅供展示，永不用于身份匹配
    target_subject_id: string | null; // 身份主键，只在本接口出现
    created_at: ISODateTime;
    remaining_seconds: number;
    version: number;
  }>;
};
```

**前端（观众页）不消费这个接口。** `note` 是主播的内部念头、
`target_subject_id` 是身份主键，两者只允许出现在这个管理接口里：
不进 WS 广播、不进数据库、不进任何面向观众的响应。`ai_reply` 事件里
永远不会出现 `thoughts` 字段。

`PROMPT_RAM__ENABLED=false`（默认）时返回 `enabled: false` 且 `entries` 为空。

## 8. 前端实现建议

### 8.1 推荐模块边界

```text
api/http.ts             通用 fetch、401/422 处理
api/auth.ts             注册、登录、昵称和记忆偏好
api/live.ts             直播元数据与可选状态查询
api/admin.ts            仅内部后台接口
ws/danmaku-client.ts    连接、重连、事件联合类型和发送队列
stores/auth.ts          Account 与登录状态；不保存 HttpOnly Token
stores/live.ts          StreamMetadata、MoodData、在线人数
stores/chat.ts          Danmaku 与 AIReply，按 danmakuID 关联
```

### 8.2 必须做的容错

- 所有 WebSocket 事件按 `type` 判别，未知事件忽略。
- 弹幕按 `danmakuID` 去重。
- `history_batch` 没有 `data` 包装层。
- `confirmation` 没有 `danmakuID`。
- `AIReply.sentences` 可能为空或 1–4 条，按数组渲染。
- `ai_reply` 先判 `data.source`：`stream_director` 变体没有 `danmaku_id`/`nickname`/`original_message`。
- 主线字段（`session_theme`、`daily_stream_plan`、`current_mainline_beat`）可能整段为 `null`，按功能未开启处理。
- `stream_mainline_beat` 与 `streamer_beat`/`streamer_activity` 各有独立的 `version`，不可互相比较。
- `401` 和 WebSocket `1008` 进入重新登录流程。
- 所有 `extra` 和动态调试字段都按未知扩展字段处理。
- 不将 `client_ip`、Token、API Key、数据库导出内容送往日志和埋点。

### 8.3 当前后端缺口

- 没有 `GET /auth/me`；刷新页面后的账号资料暂时可通过重新登录结果保存，或由前端 agent 与后端补充该接口后再实现可靠恢复。
- `POST /auth/logout` 会撤销当前客户端携带的会话令牌并清除认证 Cookie；该操作不影响其他设备的登录。
- CORS 已支持精确来源白名单、认证 Cookie、`Authorization/Content-Type` 请求头和 `OPTIONS` 预检；生产前端域名必须加入 `CORS__ALLOWED_ORIGINS`，仍优先推荐反向代理同源部署。
- P4 排期、时区、开播边界和每日主题字段均已进入 `StreamMetadata`。
- 内部管理/调试 HTTP 接口已默认关闭并使用独立管理员密钥；尚未提供面向浏览器的管理员登录流程，因此公开前端仍不得调用。
- `/config` 的 Secret 字段已掩码，但整个配置接口仍仅限管理端。
- `user_activity.extra` 和部分调试接口可能暴露 IP。
- `/memory/stats` 当前可能因异步调用错误返回 500。

前端 agent 应优先实现产品核心接口与 WebSocket；内部管理页面必须等后端管理员鉴权和敏感字段裁剪完成后再对外部署。
