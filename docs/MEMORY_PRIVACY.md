# 账号人物记忆与隐私接口

这些接口只接受登录 Cookie 或 `Authorization: Bearer <access_token>`。服务端始终从已验证令牌取得 `account_id`，不接受前端指定其他账号 ID。

## 查询人物记忆

`GET /auth/profile/memory`

返回当前记忆开关、保留期限及主播对当前账号保存的关系数据：

```json
{
  "account_id": "4fcd6163-5f63-43c2-ab7c-1b8d0db32677",
  "long_term_memory_enabled": true,
  "retention_days": 180,
  "relationship": {
    "viewer_key": "account:4fcd6163-5f63-43c2-ab7c-1b8d0db32677",
    "nickname": "小爱",
    "familiarity": 0.35,
    "affinity": 0.64,
    "trust": 0.58,
    "recent_topics": ["工作"],
    "last_message": "今天工作有点累"
  },
  "recent_conversations": [
    {
      "nickname": "小爱",
      "nickname_version": 2,
      "viewer_message": "今天工作有点累",
      "streamer_reply": "又被工作榨干啦？先在这里喘口气。",
      "topic_label": "工作",
      "transition": "continuation",
      "created_at": "2026-07-02T06:00:00+00:00"
    }
  ],
  "topic_summaries": []
}
```

尚无记忆或已退出长期记忆时，`relationship` 为 `null`，对话与摘要数组为空。

## 导出人物记忆

`GET /auth/profile/memory/export`

返回查询接口内容、活跃与归档对话片段、话题摘要，并附带昵称版本历史和 UTC 导出时间。响应不包含密码哈希、令牌摘要、其他账号数据或内部改名提示状态。

## 开启或退出长期记忆

`PUT /auth/profile/memory/preferences`

```json
{
  "long_term_memory_enabled": false
}
```

设置为 `false` 时，服务端会立即清除该账号已有的人物关系和未来 P10 对话记忆，并阻止后续持久化；当前直播连接和游客上下文仍可正常回复。重新开启只允许从空记忆重新积累，不恢复已删除内容。

默认是否启用由 `MEMORY__ENABLED_BY_DEFAULT` 配置，当前默认值为 `true`。

## 只清除已有记忆

`DELETE /auth/profile/memory`

成功返回 `204 No Content`。该操作清除人物记忆但保持记忆开关不变，也不会删除账号、登录会话、当前昵称和昵称历史。若希望清除后不再写入，应同时关闭长期记忆。

## 存储与隐私规则

- 默认保留 180 天，由 `MEMORY__RETENTION_DAYS` 配置；过期人物关系在读取或使用前删除。
- 单段文本最多保存 500 个字符，由 `MEMORY__MAX_TEXT_LENGTH` 配置。
- 邮箱、手机号、身份证号以及密码、API Key、Access Token、Secret 等凭据会在持久化前替换为隐藏标记。
- 包含“不要记住”“不要保存”“别记录”等明确拒绝语句的消息不会写入长期人物记忆。
- 游客不写入账号级长期记忆；游客断开后连接级关系立即丢弃。
- 旧版昵称键数据保留为 `legacy_nickname`，绝不会因为同名自动归入登录账号。
- 关闭或删除是不可逆操作，服务端不保留用于静默恢复的副本。

所有接口在未认证或令牌失效时返回 `401 Unauthorized`。
