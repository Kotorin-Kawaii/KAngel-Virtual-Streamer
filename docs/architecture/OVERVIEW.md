# 物理架构总览

Kangel 已完成从仓库根目录平铺模块到 `src/kangel` 的迁移。领域、协议和基础设施现在各有唯一实现，旧根业务包已删除。

```text
src/kangel/
├── app/              # 应用创建、生命周期、依赖装配
├── persona/          # 人格领域
├── audience/         # 身份、关系、presence、表情
├── memory/           # 长期记忆与数据治理
├── danmaku/          # 弹幕池、选择、文本分析与负载
├── stream/           # 排期、主题、活动和直播元数据
├── integrations/     # AI、SuperChat 和平台接入
├── transport/        # HTTP / WebSocket 协议适配
├── infrastructure/   # 数据库、安全、事件和并发实现
├── plugins/          # 稳定插件 API
├── config/           # 配置模型与加载
└── shared/           # 无业务语义的时钟、ID、错误和类型
```

## 迁移期入口

- 规范应用对象：`kangel.main:app`
- 规范命令：`kangel-server`
- 便捷命令：`python main.py`，根启动文件只转发到规范应用入口。

`kangel.app.bootstrap` 只装配规范 transport、infrastructure 与领域服务；根目录旧兼容包已退役。`config/` 仍是正式配置实现，不属于兼容层。

## 仓库边界

- 私有开发仓库维护完整功能、测试与私有配置。
- 开源仓库是独立 Git 仓库，不嵌套在私有仓库中。
- 两者暂不自动同步；安全同步工具在物理架构 P6 实现。
