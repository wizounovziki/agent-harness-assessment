# ReAct 与运维业务的结合

更新：2026-11-13。

ReAct 使用行动的外部反馈更新下一步决策。[原始论文](https://arxiv.org/abs/2210.03629)。本项目的业务能力包括故障响应、部署、更新、下线与恢复；Harness 统一控制身份、参数、审批、资源、快照与预算。

| 环节 | 故障响应 | 服务变更 |
| --- | --- | --- |
| Reason：简短决策摘要 | 根据状态与 Runbook 判断是否需要事件 | 根据容量、环境、快照及健康观测决定继续、停止或申请回滚 |
| Action：提出受控动作 | 查询状态/知识库、提交事件草案 | 预检、保存快照、提交变更、验证、提交回滚 |
| Observation：已验证结果 | 状态、手册、错误、确认的事件编号 | READY/BLOCKED、快照校验值、版本、健康与恢复结果 |

工具选择由模型提出，执行边界负责检查。模型不能通过跳过预检、改写操作者、使用其他执行的快照或伪造批准来执行变更。审批之后仍会检查资源、权限、快照和当前版本。

| 决策依据 | 工具动作 | 下一轮证据 |
| --- | --- | --- |
| 确认环境可承载明确的目标计划 | check_change_readiness | 容量、环境、权限、镜像与配置检查结果 |
| 前置条件通过，需保留可恢复状态 | capture_change_snapshot | 清单、配置、镜像摘要、源版本、校验值 |
| 已有快照，提交具体变更及恢复范围 | execute_service_change → 用户审批 | 运行端复检后产生的变更编号、版本和初次健康结果 |
| 确认上线后的业务结果 | verify_service_change | HEALTHY/OFFLINE 或 UNHEALTHY |
| 后续健康检查失败 | rollback_service_change → 独立审批 | 回退前快照、恢复版本与健康结果 |

预检拒绝后停止变更；成功恢复仍保留原部署失败结果；回滚失败或结果不明进入 NEEDS_REVIEW。最终报告引用确认的变更编号和验证结果。审计链保留动作、可信操作者、批准、参数哈希、快照与版本。

- [英文分层架构](./images/operations-layered-architecture-en-v3.png)
- [英文业务流程](./images/operations-business-workflow-en-v2.png)
- [变更安全契约与实现](./operations-safety.md)
