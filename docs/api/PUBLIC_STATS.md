# 公开直播数据接口

`GET /public/stats` 是匿名、只读的公开统计接口，返回当前直播站点的匿名聚合数字。
它不要求登录，不返回昵称、用户名、订单、原始弹幕或其他身份信息。

## 响应

```json
{
  "generated_at": "2026-09-10T00:30:00+00:00",
  "timezone": "Asia/Shanghai",
  "today": {
    "replies": 12,
    "danmaku": 34,
    "new_accounts": 5
  },
  "all_time": {
    "replies": 120,
    "danmaku": 340,
    "registered_accounts": 50
  }
}
```

`today` 使用 `settings.stream.timezone` 的自然日，时间范围是半开区间
`[00:00:00, 次日 00:00:00)`。回复只统计实际观众回复（`normal` 与历史兼容的
`legacy` 来源），不会把 SC 或内部事件计入。`all_time` 统计现有业务记录的累计值。

服务端对结果使用不超过 60 秒的进程内缓存。缓存或数据库异常只会让该接口返回
`503`，不会阻塞弹幕、SC、WebSocket 或其他直播链路。

## 前端调用

```js
const response = await fetch(`${API_BASE}/public/stats`, {
  credentials: 'include',
});
const stats = await response.json();
```

接口可被兼容的前端客户端直接使用；公开网页不需要挂载直播间 WebSocket。
