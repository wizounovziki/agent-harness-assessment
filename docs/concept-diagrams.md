# 运维 Agent Harness：架构图与执行流程图

日期：2026-11-12  
设计路线：自写精简 Harness，使用官方模型 SDK；参考 Pi 的循环、工具契约和事件设计。

## 英文交付图

架构图采用五层堆叠结构，展示交互入口、运维业务逻辑、执行与安全、模型与工具适配、数据与证据。业务逻辑层突出服务评估、手册诊断、事件规划和结果报告，ReAct 作为决策机制注释。

![Operations Agent Harness: Layered Architecture](./images/operations-layered-architecture-en-v2.png)

流程图展示证据收集、事件必要性判断、用户审批、创建与核对、最终报告，以及失败和停止路径。

![Operations Assistant: Business Workflow](./images/operations-business-workflow-en-v1.png)

英文设计说明见 [Architecture and Business Workflow](./business-architecture-and-workflow-en.md)。ReAct 结合方法见 [补充说明](./react-business-design.md)。下方模块调用关系和控制流程用于实现契约设计。

## 1. 模块调用关系参考

模型负责提出下一步动作；Harness 管理执行、审批、校验和预算。用户通过 API 提交目标、审批动作并查询结果。

```mermaid
flowchart TB
    U["用户 / Postman"]

    subgraph H["Agent Harness · Python / FastAPI"]
        API["HTTP API<br/>提交目标 · 查询执行 · 审批"]
        C["执行控制器 / ReAct Loop<br/>推理 → 行动 → 观察 · 执行预算"]
        MA["Reason：模型适配器<br/>目标 · 观测 · 运维策略 → 下一步决策"]
        G["Action：工具执行网关<br/>输入输出与业务校验 · 超时 · 重试 · 幂等"]
        A["审批服务<br/>绑定动作与参数 · 保存决定 · 恢复执行"]

        API --> C
        API -->|用户审批| A
        C <--> MA
        C --> G
        G <-->|创建前核验批准| A
    end

    L["LLM 服务<br/>通过官方 SDK 接入"]
    DB[("SQLite<br/>执行状态 · 消息 · 动作 · 审批")]
    T["日志与 Traces<br/>按 run_id 关联全过程"]

    subgraph MT["Mock Tools · 场景数据驱动"]
        KB["search_knowledge_base<br/>查询内部文档"]
        SS["get_service_status<br/>查询服务状态"]
        CI["create_incident<br/>创建模拟事件"]
    end

    U <--> API
    MA <--> L
    G --> KB
    G --> SS
    G -->|有效批准后执行| CI
    C <--> DB
    A <--> DB
    C -.->|汇总状态、模型、工具和审批事件| T
```

架构职责：

- 执行控制器：检查预算、推进状态、调用模型和工具、判断结束。
- 工具网关：验证输入输出，实施超时、有限重试和幂等控制。
- 审批服务：保存具体动作和参数，验证用户决定，通知控制器恢复原动作。
- 状态与观测：保存可恢复的执行历史，生成可按执行编号查询的 Trace。
- Mock Tools：从固定数据集中读取文档、服务状态和故障场景；创建事件写入模拟事件表。

## 2. 执行控制流程参考

```mermaid
flowchart TD
    START(["用户提交目标"])
    INIT["创建 run_id<br/>保存目标、初始状态与 Trace"]
    BUDGET{"执行预算充足？"}
    MODEL["Reason：根据目标与观测决定下一步<br/>请求模型 · 响应校验 · 有限修复"]
    VALIDATE["Action：选择工具与参数<br/>校验业务规则 · 逐个处理本轮调用"]
    APPROVAL{"调用 create_incident？"}
    WAIT["保存原动作与参数，暂停执行<br/>waiting_approval"]
    DECISION{"审批结果"}
    EXECUTE["受控执行工具<br/>预算检查 · 超时 · 有限重试 · 幂等"]
    OUTCOME{"执行及输出校验结果"}
    SAVE["Observation：已验证结果或结构化错误<br/>保存观测 · 更新上下文 · 记录 Trace"]
    MORE{"本轮还有待处理工具？"}
    FINISH(["核对工具证据，生成报告<br/>succeeded / failed"])
    FAIL(["保存失败原因<br/>failed"])
    STOP(["保存停止原因<br/>stopped"])
    REVIEW(["保留现场，等待核对<br/>needs_review"])

    START --> INIT --> BUDGET
    BUDGET -->|是| MODEL
    BUDGET -->|否| STOP
    MODEL -->|最终回答| FINISH
    MODEL -->|工具调用| VALIDATE
    MODEL -->|请求或格式修复失败| FAIL
    VALIDATE -->|有效| APPROVAL
    VALIDATE -->|无效| SAVE
    APPROVAL -->|是| WAIT --> DECISION
    APPROVAL -->|否| EXECUTE
    DECISION -->|有效批准原参数| EXECUTE
    DECISION -->|拒绝或过期| STOP
    EXECUTE -->|预算耗尽| STOP
    EXECUTE --> OUTCOME
    OUTCOME -->|成功且输出有效| SAVE
    OUTCOME -->|已确认失败或输出无效| SAVE
    OUTCOME -->|按幂等键核对后写入仍未知| REVIEW
    SAVE --> MORE
    MORE -->|是| VALIDATE
    MORE -->|否| BUDGET
```

流程规则：

1. 每次实际模型请求和工具尝试都检查剩余预算；重试和格式修复计入累计用量。
2. 等待审批时保存状态并释放执行资源；审批使用独立有效期，恢复后沿用原执行预算。
3. 批准绑定用户、run、动作、参数和有效期；批准后执行保存的原动作。参数变化生成新审批。
4. 同轮工具按顺序处理，审批暂停时保存处理游标；该轮结果回填完整后再请求模型。
5. 工具错误作为结构化反馈供模型判断下一步；持续失败最终由完成判定或预算限制结束执行。
6. 写入超时先按幂等键确认结果，确认成功则复用结果，无法确认则进入 needs_review。
7. 每次状态转换及模型、工具、审批活动都写入 Trace；持久化失败时停止推进写动作。

## 3. 实现思路

1. 先定义模型响应、工具输入输出、运行状态和事件契约。
2. 使用脚本模型与三个 Mock Tools 实现完整循环，验证审批、故障和限制路径。
3. 接入 SQLite 与 HTTP API，补齐审批恢复、重复提交和 Postman 演示。
4. 替换为真实模型适配器，完成端到端验证并整理测试证据。

详细方案见 [需求与概念设计](./requirements-and-concept-design.md)，实施任务见 [TODO](./TODO.md)。
