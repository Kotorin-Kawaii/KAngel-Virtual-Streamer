# Security Policy

## 受支持范围

当前项目优先支持最新主分支代码。早期实验性版本不保证安全修复回合。

## 报告安全问题

如果你发现以下问题，请不要公开粘贴可利用细节：

- API Key、Cookie、Token 或用户数据泄漏；
- 认证绕过；
- 管理接口未授权访问；
- 可导致服务不可用的请求放大或资源耗尽问题；
- 跨站请求、跨站脚本或 WebSocket 鉴权问题。

请通过 GitHub Security Advisory 或私下联系维护者报告，并包含：

- 影响版本或提交；
- 最小复现步骤；
- 影响范围；
- 建议修复方向（如果有）。

## 部署安全基线

- 生产环境必须使用 HTTPS。
- `CORS__ALLOWED_ORIGINS` 使用精确 Origin，不要使用 `*`。
- 跨站 Cookie 登录时启用 `Secure; SameSite=None`。
- 管理接口默认不要暴露到公网。
- 入口层建议配置 CDN/WAF、源站防火墙、连接数限制和请求速率限制。
- 应用内限流只能缓解误用，不能替代 DDoS 防护。

## 敏感数据

请勿提交或公开：

- `.env`；
- API Key；
- SQLite 数据库；
- 用户长期记忆；
- 日志；
- 管理员密钥；
- Cookie / Authorization Header。
