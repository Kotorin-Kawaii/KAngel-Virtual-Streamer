# 账号注册、登录与 WebSocket 身份接入

## 设计选择

注册和登录使用 HTTP。凭据交换需要明确的状态码、请求校验及未来的限流能力，不适合混入持续连接的弹幕 WebSocket。浏览器登录成功后会得到两个 `HttpOnly` Cookie：access Cookie 用于常规 HTTP/WebSocket 鉴权，refresh Cookie 只用于刷新会话；同源浏览器建立弹幕 WebSocket 时会自动携带 access Cookie。

- HTTP 基础地址示例：`http://localhost:8000`
- WebSocket 地址：`ws://localhost:8000/danmaku`
- 请求与响应编码：`application/json; charset=utf-8`
- access token 默认有效期：168 小时，可由 `AUTH__ACCESS_TOKEN_TTL_HOURS` 配置；Cookie 名称默认是 `kangel_access_token`，可由 `AUTH__COOKIE_NAME` 配置。
- refresh token 默认有效期：720 小时，可由 `AUTH__REFRESH_TOKEN_TTL_HOURS` 配置；Cookie 名称默认是 `kangel_refresh_token`，可由 `AUTH__REFRESH_COOKIE_NAME` 配置。
- refresh Cookie 的 Path 固定为 `/auth/refresh`，每次成功刷新都会轮换，已使用的 refresh token 不能再次使用。
- 生产 HTTPS 环境应设置 `AUTH__COOKIE_SECURE=true`。
- 未携带令牌的 WebSocket 保持现有游客行为。
- 携带无效或过期令牌时不会降级为游客，而是以 WebSocket 关闭码 `1008` 拒绝连接。

## 1. 创建账号

`POST /auth/register`

### 请求体

```json
{
  "username": "alice_01",
  "password": "a-strong-password",
  "nickname": "小爱"
}
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `username` | string | 3–64 位，不允许空白和控制字符 | 登录名；使用 NFKC + casefold 判断重复，因此 `Alice` 与 `alice` 是同一登录名 |
| `password` | string | 默认 8–128 位 | 仅在请求中出现；服务端使用随机盐和 scrypt 保存哈希 |
| `nickname` | string | 1–100 位 | 直播间展示名，不是账号主键，后续允许改名 |

### 成功响应

状态码：`201 Created`

```json
{
  "account": {
    "account_id": "4fcd6163-5f63-43c2-ab7c-1b8d0db32677",
    "username": "alice_01",
    "nickname": "小爱",
    "nickname_version": 1,
    "created_at": "2026-07-02T04:00:00+00:00"
  },
  "access_token": "opaque-random-token",
  "token_type": "bearer",
  "expires_at": "2026-07-09T04:00:00+00:00"
}
```

注册成功会自动创建登录会话，前端无需紧接着再次调用登录接口。

响应还会包含：

```http
Set-Cookie: kangel_access_token=<token>; HttpOnly; Secure; Max-Age=604800; Path=/; SameSite=none; Partitioned
Set-Cookie: kangel_refresh_token=<token>; HttpOnly; Secure; Max-Age=2592000; Path=/auth/refresh; SameSite=none; Partitioned
```

浏览器前端应忽略 JSON 中为非浏览器客户端保留的 `access_token`，不得写入 `localStorage`、`sessionStorage`、IndexedDB 或应用状态持久化层。

### 错误响应

| 状态码 | 场景 | `detail` 示例 |
|---|---|---|
| `409 Conflict` | 用户名已存在 | `用户名已存在` |
| `422 Unprocessable Entity` | 字段缺失、长度不合法或格式不合法 | Pydantic 校验详情或密码策略说明 |

### curl 示例

```bash
curl -X POST http://localhost:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice_01","password":"a-strong-password","nickname":"小爱"}'
```

## 2. 登录

`POST /auth/login`

### 请求体

```json
{
  "username": "alice_01",
  "password": "a-strong-password"
}
```

### 成功响应

状态码：`200 OK`

响应结构与注册成功相同。每次成功登录都会签发一个新的独立令牌，旧令牌在到期前仍有效。

### 错误响应

| 状态码 | 场景 | `detail` |
|---|---|---|
| `401 Unauthorized` | 用户名不存在或密码错误 | `用户名或密码错误` |
| `422 Unprocessable Entity` | 请求字段格式错误 | Pydantic 校验详情 |

服务端对“用户名不存在”和“密码错误”返回相同信息，避免通过错误内容枚举账号。

### curl 示例

```bash
curl -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice_01","password":"a-strong-password"}'
```

## 3. 刷新浏览器登录态

`POST /auth/refresh`

该接口没有请求体，也不接受 `Authorization` 或 access token。浏览器以 `credentials: "include"` 调用，服务端只读取 `HttpOnly` refresh Cookie；成功时轮换 refresh token，并重新设置 access/refresh 两个 `HttpOnly` Cookie。

成功状态码：`200 OK`

```json
{
  "account": {
    "account_id": "4fcd6163-5f63-43c2-ab7c-1b8d0db32677",
    "username": "alice_01",
    "nickname": "小爱",
    "nickname_version": 1,
    "created_at": "2026-07-02T04:00:00+00:00"
  },
  "expires_at": "2026-07-09T04:00:00+00:00"
}
```

响应不会返回 access 或 refresh token。refresh Cookie 缺失、失效、过期或已被使用时返回 `401`（`登录状态已过期，请重新登录`）。

页面恢复登录态的固定流程：先请求 `GET /auth/profile`；仅在它返回 `401` 时调用一次 refresh；refresh 成功后只重试 profile 一次；profile 成功后关闭旧 WebSocket 并重新连接 `/danmaku`，使握手携带新的 access Cookie。refresh 或重试后的 profile 仍为 `401` 时，才清除前端登录 UI 并要求重新登录。网络错误、`429`、`5xx` 不表示退出登录，不应触发刷新循环或清理 UI。

```javascript
async function restoreBrowserSession(reconnectDanmaku) {
  let profile = await fetch("/auth/profile", { credentials: "include" });
  if (profile.status === 401) {
    const refreshed = await fetch("/auth/refresh", {
      method: "POST",
      credentials: "include",
    });
    if (refreshed.status === 401) return null;
    if (refreshed.status !== 200) throw new Error("refresh request failed");
    profile = await fetch("/auth/profile", { credentials: "include" });
  }
  if (profile.status === 401) return null;
  if (profile.status !== 200) throw new Error("profile request failed");
  const account = await profile.json();
  reconnectDanmaku();
  return account;
}
```

## 4. 修改密码

`PATCH /auth/profile/password`

该接口必须携带当前 access Cookie 或 `Authorization: Bearer <access_token>`。请求体如下：

```json
{
  "current_password": "a-strong-password",
  "new_password": "a-new-strong-password"
}
```

两个密码字段长度均为 1–128 位；新密码还必须满足服务端 `AUTH__MIN_PASSWORD_LENGTH`（默认 8 位）。服务端只在当前密码验证成功后生成新随机盐并以 scrypt 保存。新密码不能与当前密码相同。

成功状态码：`200 OK`

```json
{
  "account": {
    "account_id": "4fcd6163-5f63-43c2-ab7c-1b8d0db32677",
    "username": "alice_01",
    "nickname": "小爱",
    "nickname_version": 1,
    "created_at": "2026-07-02T04:00:00+00:00"
  },
  "reauthentication_required": true
}
```

修改成功会原子撤销该账号所有 access/refresh 会话，并通过 `Max-Age=0` 清理当前浏览器 Cookie；接口不会签发新令牌，前端必须回到登录页。旧设备也必须使用新密码重新登录。

| 状态码 | 场景 | `detail` 示例 |
|---|---|---|
| `400 Bad Request` | 新密码与当前密码相同 | `新密码不能与当前密码相同` |
| `401 Unauthorized` | 登录状态失效，或当前密码错误 | `当前密码不正确` |
| `422 Unprocessable Entity` | 字段缺失、长度或密码策略不合法 | Pydantic 校验详情或密码策略说明 |
| `429 Too Many Requests` | 账号/IP/全局限流或渐进失败冷却 | `RateLimitError` |

错误响应不会包含密码、哈希、令牌或其他凭据。该接口没有自动找回密码能力。

## 5. 忘记密码人工兜底

当前不提供重置 API、邮箱/手机找回或后台自助重置。前端可以引导用户加入交流群 `1015206230` 联系管理员人工处理；用户不应发送密码、登录令牌或其他敏感信息。

## 6. 修改昵称与昵称历史

以下接口必须携带登录 Cookie，或者使用请求头：

```http
Authorization: Bearer <access_token>
```

### 6.1 修改当前昵称

`PATCH /auth/profile/nickname`

请求体：

```json
{
  "nickname": "小爱改名了"
}
```

成功状态码：`200 OK`

```json
{
  "account_id": "4fcd6163-5f63-43c2-ab7c-1b8d0db32677",
  "username": "alice_01",
  "nickname": "小爱改名了",
  "nickname_version": 2,
  "created_at": "2026-07-02T04:00:00+00:00"
}
```

改名以 `account_id` 为归属原子追加版本，不会创建新账号，也不会清空人物关系。该账号已经建立的 WebSocket 连接会立即采用新昵称，无需重连。同名更新是幂等操作，不会增加版本号。

错误状态：未登录或令牌失效返回 `401`；昵称格式错误返回 `422`。

### 6.2 查询自己的昵称历史

`GET /auth/profile/nickname-history`

成功状态码：`200 OK`

```json
{
  "account_id": "4fcd6163-5f63-43c2-ab7c-1b8d0db32677",
  "history": [
    {
      "version": 2,
      "nickname": "小爱改名了",
      "started_at": "2026-07-03T04:00:00+00:00",
      "ended_at": null,
      "is_current": true
    },
    {
      "version": 1,
      "nickname": "小爱",
      "started_at": "2026-07-02T04:00:00+00:00",
      "ended_at": "2026-07-03T04:00:00+00:00",
      "is_current": false
    }
  ]
}
```

只能读取令牌所属账号的历史，接口不接受客户端指定 `account_id`。

### 6.3 删除自己的旧昵称

`DELETE /auth/profile/nickname-history/{version}`

成功状态码：`204 No Content`。删除是物理删除，之后该旧名不会出现在查询、导出或主播提示词中。

| 状态码 | 场景 |
|---|---|
| `401 Unauthorized` | 未登录或令牌失效 |
| `404 Not Found` | 版本不存在或不属于当前账号 |
| `409 Conflict` | 尝试删除当前昵称版本 |

主播仅会获得一次“该登录观众近期改过名”的提示，默认有效期为 14 天。为了避免在公开直播中暴露隐私，旧昵称原文不会进入 AI 回复提示；主播可以自然注意到改名，但不能念出或猜测旧名。

## 7. 人物记忆与隐私控制

登录用户可以查询、导出、清除或关闭人物长期记忆。完整请求、响应及脱敏规则见 [账号人物记忆与隐私接口](../concepts/MEMORY_PRIVACY.md)。

主要接口：

- `GET /auth/profile/memory`
- `GET /auth/profile/memory/export`
- `PUT /auth/profile/memory/preferences`
- `DELETE /auth/profile/memory`

这些接口只能操作当前访问令牌所属账号，不能由客户端传入其他 `account_id`。

## 8. 携带登录身份连接弹幕 WebSocket

### 浏览器推荐方式：HttpOnly Cookie

当前端与服务端同源，注册或登录响应设置 Cookie 后直接连接：

```javascript
const ws = new WebSocket("ws://localhost:8000/danmaku");
```

浏览器会在握手中自动发送 Cookie，JavaScript 无需也无法读取其中的令牌。

### 非浏览器或跨域调试方式：查询参数

```text
ws://localhost:8000/danmaku?access_token=<登录响应中的 access_token>
```

查询参数示例：

```javascript
const token = loginResponse.access_token;
const ws = new WebSocket(
  `ws://localhost:8000/danmaku?access_token=${encodeURIComponent(token)}`
);
```

连接后的弹幕发送格式暂时保持不变：

```json
{
  "nickname": "客户端缓存的昵称",
  "message": "晚上好！",
  "danmakuID": "msg_001",
  "type": "normal"
}
```

对于登录连接，服务端以账号记录中的昵称为准，忽略弹幕负载中伪造或过期的昵称；对于游客连接，仍使用消息中的昵称。弹幕负载中的 `account_id` 或 `user_id` 字段不会被信任。

### 8.1 游客连接

不传令牌即可保持原有行为：

```javascript
const ws = new WebSocket("ws://localhost:8000/danmaku");
```

游客会获得仅在当前连接有效的临时身份。相同昵称的不同游客不会共享账号关系，也不会自动继承旧昵称数据。

## 9. 前端保存建议

1. 同源浏览器只使用服务端设置的 `HttpOnly` access/refresh Cookie，不要由 JavaScript 复制、缓存或持久化任何令牌。
2. 生产环境必须使用 HTTPS/WSS，并设置 `AUTH__COOKIE_SECURE=true`。
3. 查询参数仅用于 Cookie 不可用的客户端；不要把含令牌 URL 写入日志、埋点、错误上报或分享内容。
4. `GET /auth/profile` 首次 `401` 时，先调用一次 `/auth/refresh` 并重试 profile；仅当 refresh 或重试仍是 `401` 时才清除本地登录状态。WebSocket `1008` 也应遵循同一恢复流程。
5. `account_id` 仅用于前端关联账号数据，不能替代访问令牌进行认证。
6. 登录账号只能通过 `PATCH /auth/profile/nickname` 改名，不要通过弹幕字段尝试修改昵称。

## 10. 当前安全边界与后续项

- 密码以 scrypt + 每账号随机盐保存。
- 数据库仅保存 access/refresh 令牌的 SHA-256 摘要，不保存令牌明文。
- 账号 ID 为不可变 UUID；用户名和昵称都不是记忆归属键。
- 当前仍未提供独立登出接口、自动找回密码或邮箱/手机绑定；修改密码会撤销该账号全部现有会话，登录和修改密码入口均有应用内限流。
