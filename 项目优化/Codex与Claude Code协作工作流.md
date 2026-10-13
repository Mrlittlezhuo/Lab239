# Codex 与 Claude Code 人工交接协作工作流

> 适用项目：Langchain-Chatchat
>
> 适用场景：Codex 负责分析、任务拆分、代码审查和 Git 交付；Claude Code 由用户在本地人工调用并负责限定范围内的编码实现。
>
> 目标：即使更换电脑或开启新的 Codex 会话，只要读取本文件和当前 Git 状态，就能继续协作。

## 1. 指令优先级

本文件是项目协作约定，不覆盖更高优先级的系统指令、用户当前请求、仓库根目录 `AGENTS.md` 或安全规则。仓库级 `AGENTS.md` 已纳入版本控制，换电脑后也必须先读取。

新的 Codex 会话读取本文件后，应把它作为本项目的默认协作方式：

- 不直接、自动调用 Claude Code；
- 需要 Claude Code 时，先生成任务文档并交给用户；
- 用户人工把任务文档上传或粘贴给 Claude Code；
- Claude Code 完成后，由用户通知 Codex；
- Codex 审查全部改动、运行测试、修复问题；
- 只有 Codex 审查通过后才提交和推送。

如果用户在当前会话中明确要求采用其他流程，以用户当前请求为准。

## 2. 三方职责

### 2.1 用户

用户是 Claude Code 的人工中转和最终需求确认者，负责：

1. 告诉 Codex 当前需求和优先级；
2. 审核 Codex 生成的 Claude 任务文档；
3. 在项目根目录人工启动 Claude Code；
4. 将任务文档上传或完整粘贴给 Claude Code；
5. 等待 Claude Code 完成；
6. 不在 Claude 执行期间让 Codex 同时修改相同文件；
7. Claude 完成后通知 Codex，例如：“Claude 已执行完成，请审查”；
8. 当 Codex 发现需求歧义或高风险变更时作出最终选择。

### 2.2 Codex

Codex 是任务负责人和最终审查者，负责：

1. 读取用户需求、项目规则、相关源码和现有文档；
2. 检查 Git 基线和未提交改动；
3. 把大需求拆成适合 Claude Code 的单一、可验收任务；
4. 在 `项目优化/Claude任务/` 下生成任务文档；
5. 明确允许修改范围、禁止事项、测试和完成定义；
6. 等待用户人工调用 Claude Code，不主动通过工具调用 Claude；
7. Claude 完成后检查全部 tracked、untracked 和必要的 ignored 文件变化，以及 Claude 已推送的提交；
8. 审查架构、权限、安全、迁移、兼容性和测试覆盖；
9. 对小问题直接修复，对较大返工生成新的 Claude 修正任务；
10. 运行最终验证；
11. 只暂存本任务文件，创建 Git 提交并推送；
12. 向用户报告审查结论、测试结果、提交哈希、远端分支和推送结果。

Codex 不应因为 Claude 声称“已完成”就认定改动可用。Claude 的改动即使已经推送，仍按未审查代码处理：审查在提交之后进行，发现问题用独立修复提交补上，不改写已推送历史（见 10.4）。

### 2.3 Claude Code

Claude Code 是限定范围内的实现者，负责：

1. 完整阅读用户上传的任务文档；
2. 先检查任务指定的相关源码，不做无关扫描；
3. 只修改任务允许的文件和功能；
4. 遵守任务中的禁止事项；
5. 编写或更新对应测试；
6. 运行任务要求的定向测试；
7. 改动完成并通过定向验证后按「Git 交付」自行提交并推送当前分支：只暂存本任务文件、按逻辑拆分提交；不执行 Git reset、checkout，不清理用户改动；
8. 不安装依赖，除非任务文档和用户明确授权；
9. 不读取或修改运行数据、密钥、日志和任务明确排除的目录；
10. 完成后报告修改文件、设计选择、测试结果和已知限制。

Claude Code 不负责最终合并，也不负责判断整个项目是否可以发布。

## 3. 标准协作流程

### 阶段 0：Codex 建立基线

Codex 在生成任务前执行只读检查：

```powershell
git status --short --branch
git rev-parse HEAD
git log -5 --oneline --decorate
git remote -v
git diff HEAD --stat
git diff --cached --stat
git ls-files --others --exclude-standard
git stash list
git submodule status
```

并记录：

- 当前分支；
- 基线 commit；
- 已有未提交文件；
- 哪些改动属于用户或前一个任务；
- staged、unstaged、untracked、stash 和 submodule 是否存在遗留状态；
- 是否存在未完成的 Claude 任务；
- 相关测试入口。

如果工作区已有改动，Codex不得擅自丢弃、覆盖或与新任务混合提交。

### 阶段 1：Codex 生成 Claude 任务文档

建议文件位置：

```text
项目优化/Claude任务/NNN-任务名称.md
```

命名示例：

```text
001-后端用户认证与会话隔离.md
002-WebUI登录与历史会话接入.md
003-用户隔离审查问题修复.md
```

每份任务只解决一个相对独立的问题。对于本地推理较慢的 Claude Code，优先拆成：

1. 数据库与认证；
2. 后端 API 与权限；
3. WebUI 接入；
4. 数据迁移；
5. 测试与问题修复。

不要把整个大型功能一次性塞入一个任务。

任务文档是持久化交接物，必须遵守：

1. Codex 记录目标分支和完整源码基线 commit；
2. Codex 明确源码基线后允许出现的规则/任务文档提交；
3. Codex 将 `ready_for_claude` 的任务文档提交并推送后再交给用户；
4. Claude 开始前核对分支、工作区、源码基线后的文件和 stash；
5. Claude 将执行结果写回同一任务文档的固定报告区；
6. Codex 将最终审查记录与代码一起提交。

源码基线表示实现前的业务代码版本，不要求 `HEAD` 与它完全相等，因为任务文档提交会位于该基线之上。Claude 必须确认基线后的提交只修改了任务声明允许的规则或任务文档；如果混入业务代码则停止。

### 阶段 2：用户人工调用 Claude Code

用户在项目根目录启动 Claude Code。

Windows 示例：

```powershell
cd <仓库路径>\Langchain-Chatchat
git status --short --branch
git rev-parse HEAD
git diff --name-status <源码基线commit>..HEAD
git stash list
claude.cmd
```

如果 `claude` 被 PowerShell 执行策略阻止，应使用 `claude.cmd`，不要为了运行 Claude 而修改整个系统的 PowerShell 执行策略。

启动 Claude 前必须满足：当前分支与任务一致、工作区干净、基线之后只有任务声明允许的文档提交、没有来源不明的 stash。任何一项不满足都先停止并交给 Codex 判断。

用户向 Claude Code 提供：

1. 任务文档完整内容；
2. 项目根目录；
3. 明确指令：“按任务文档执行，不要提交或推送”；
4. 必要时提供 Codex 指定的补充上下文。

不要上传：

- `.env`、API Key、访问令牌和密码；
- 用户真实聊天数据；
- 数据库备份；
- 运行日志；
- `chatchat-study-data` 等任务排除的数据目录；
- 与任务无关的私有文件。

### 阶段 3：Claude Code 实现

Claude Code 执行期间：

- 用户不要让 Codex 同时修改相同文件；
- 不要手工格式化或重排 Claude 正在编辑的代码；
- 本地模型推理较慢时允许耐心等待；
- 不把“进程仍在运行”当作失败；
- 如果 Claude 请求扩大范围，先暂停并由用户交给 Codex重新评估。

Claude 完成后，应先跑定向验证（后端相关 `tests/unit_tests`；前端 `npx vitest run` 与 `npm run typecheck`，改了前端再 `npm run build`），验证通过就把本任务文件提交并推送当前分支，再把修改文件、设计选择、测试结果、提交哈希、未完成项和风险追加到任务文档的“Claude 执行报告”，将状态改为 `ready_for_codex_review`。用户随后通知 Codex 开始审查。聊天窗口中的总结可以同时保留，但不能作为唯一记录。

验证没通过时不提交，保留现场如实报告，等 Codex 处理。

### 阶段 4：Codex 审查

Codex 首先检查：

```powershell
git status --short --branch
git diff HEAD --stat
git diff HEAD
git diff --cached
git diff
git ls-files --others --exclude-standard
git stash list
git submodule status
git log --oneline origin/master..HEAD
```

改动方已经提交推送，所以审查对象包括已推送但尚未审查的提交：用 `git show <提交>` 或 `git diff <基线>..HEAD` 逐个看，不能只看工作区 diff。然后按本文件第 7 节进行完整审查。

`git diff HEAD` 用于覆盖全部 tracked 改动，`git diff --cached` 和 `git diff` 分别用于确认 staged/unstaged 边界。Codex 不只阅读 Claude 的总结；必须直接检查代码 diff、未跟踪文件内容和任务报告。stash 中若有内容，必须先查明归属，不能直接 drop 或假设与任务无关。

### 阶段 5：问题处理

审查结论分为三类：

| 结论 | 处理方式 |
|---|---|
| 通过 | 运行最终测试，准备提交 |
| 小问题 | Codex 直接修复并补测试 |
| 较大问题 | Codex 生成新的修正任务文档，由用户再次交给 Claude |

较大问题包括：

- 需要重新设计数据模型；
- 权限边界大面积遗漏；
- 迁移可能丢数据；
- Claude 修改了禁止范围；
- 需要跨多个模块重新实现；
- 测试与实际行为明显不一致。

### 阶段 6：最终验证、提交和推送

最终验证由 Codex负责：

1. 运行格式和静态检查；
2. 运行定向单元测试；
3. 运行相关 API/集成测试；
4. 检查数据库迁移和回滚；
5. 检查工作区是否混入无关文件；
6. 暂存本任务文件；
7. 创建提交；
8. 推送远端；
9. 再次确认本地与远端状态。

这一阶段的 Git 操作由改动方执行：Claude 完成的改动由 Claude 提交推送，Codex 补修的改动由 Codex 提交推送。审查发现的问题用独立修复提交补上，不改写已推送历史、不 force push。

## 4. Claude 任务文档模板

仓库中的权威模板是 [`项目优化/Claude任务/000-任务模板.md`](./Claude任务/000-任务模板.md)。Codex 应复制该文件创建任务，不能另写一个结构不一致的临时提示词。

每份任务至少包含：

1. 状态、负责人、目标分支和完整源码基线 commit；
2. Claude 执行前命令和明确停止条件；
3. 背景、目标、允许修改范围和禁止事项；
4. 数据库、API、安全、兼容性等设计约束；
5. 可离线重复的测试要求；
6. 客观验收标准；
7. Claude 只能追加的执行报告区；
8. Codex 审查记录区。

禁止只在聊天中发送一次性任务要求而不落盘。任务指令和报告都必须保存在仓库任务文档中。

## 5. 给 Claude Code 的固定开场指令

用户可以在上传任务文档时附上：

```text
请完整阅读我提供的任务文档，并在当前 Langchain-Chatchat 仓库中执行。

先执行任务文档中的基线检查。若分支不符、工作区已有不明改动、源码基线后混入未声明业务代码、stash 不符合预期或文件发生冲突，请立即停止并报告，不要自行清理。

严格遵守允许范围和禁止事项。不要读取任务排除的数据，不要安装未授权依赖，不要执行 git commit、git push、git reset、git checkout、git clean 或 git stash，不要覆盖已有无关改动。

检查相关源码后再实施和运行定向测试。完成后把状态改为 ready_for_codex_review，并在任务文档的“Claude 执行报告”中写入修改文件、设计选择、实际测试命令与结果、未完成项和风险。
```

## 6. 用户通知 Codex 的建议格式

Claude 完成后，用户可以发送：

```text
Claude Code 已执行完成，请按照协作工作流审查当前工作区。

任务文档：项目优化/Claude任务/<任务文件>.md
Claude 的聊天总结：<可选粘贴；任务文档中的执行报告为持久化记录>
```

如果 Claude 中途失败，也应通知 Codex，不要先删除其修改：

```text
Claude Code 执行中断，请检查当前工作区的部分改动并判断如何继续。
```

## 7. Codex 审查清单

### 7.1 范围

- 是否只修改任务允许的文件？
- 是否修改了用户已有文件？
- 是否出现未说明的新依赖？
- 是否修改了配置、运行数据、日志或大文件？
- 是否触碰了任务明确禁止的模块？

### 7.2 正确性

- 用户主流程是否真的可用？
- 错误路径是否能安全恢复？
- 空数据、重复请求、并发和重试是否合理？
- 数据库状态和内存状态是否一致？
- 流式与非流式路径是否遵循相同规则？

### 7.3 权限和安全

- 当前用户是否来自服务端认证上下文？
- 是否错误相信请求中的 `user_id`？
- 列表、详情、更新、删除是否执行相同的所有权校验？
- 后台任务和流式任务是否继承正确身份？
- 是否存在 IDOR、路径穿越、敏感信息泄露或越权？
- 令牌、密码和密钥是否进入日志或响应？
- 禁用用户和过期凭据是否立即生效？

### 7.4 数据库和迁移

- 迁移是否幂等？
- 是否会 drop 表或静默丢数据？
- 旧数据如何回填？
- 非空约束是否在回填后添加？
- SQLite 和目标生产数据库是否兼容？
- 是否有迁移前备份、校验和回滚路径？

### 7.5 兼容性

- 原有 API 和客户端是否被无意破坏？
- OpenAI 兼容接口是否仍能工作？
- 普通聊天、RAG 和 Agent 是否行为一致？
- Windows 路径、编码和进程行为是否兼容？
- 公共知识库、模型、工具或 MCP 是否被误改成用户私有？

### 7.6 测试

- 测试是否真正覆盖缺陷，而非只覆盖辅助函数？
- 是否包含正向、拒绝、越权和旧数据迁移测试？
- 测试是否离线可重复？
- 是否错误依赖用户本地数据库或模型服务？
- Claude 报告的测试是否可以由 Codex重复运行？

### 7.7 Git

- 是否只有当前任务文件被暂存？
- 是否存在未跟踪的生成文件？
- 是否混入数据库、索引、日志或密钥？
- commit 是否准确描述变更？
- push 后本地分支是否与远端一致？

## 8. Codex 审查报告格式

Codex 向用户报告时使用以下结构：

```markdown
## 审查结论

通过 / 需要修复 / 阻断

## 主要发现

1. P0/P1/P2：问题、证据、影响和处理

## Claude 完成的内容

- 功能和文件

## Codex 补充修改

- 修复和原因

## 验证

- 命令：结果

## 未完成和风险

- 内容

## Git

- commit
- branch
- push result
```

## 9. 工作状态流转

| 状态 | 负责人 | 含义 |
|---|---|---|
| `draft` | Codex | 正在分析和编写任务 |
| `ready_for_claude` | Codex | 任务文档已提交推送，可交给 Claude |
| `claude_running` | 用户 | 用户已启动本地实现；可只作为运行态通知 |
| `ready_for_codex_review` | Claude Code | Claude 已填写执行报告，等待审查 |
| `changes_requested` | Codex | 审查发现需要修正 |
| `validated` | Codex | 代码和测试已通过 |
| `completed` | Codex | 审查记录完成，可与最终代码一起提交 |

任务文档顶部应更新当前状态。`ready_for_claude` 必须持久化到远端；Claude 的 `ready_for_codex_review` 与源码改动一起等待审查；`completed` 与最终代码一起提交。提交和推送结果通过 Git 历史、分支跟踪状态及 Codex 最终报告判断，不在文档中维护会造成自引用的 commit 哈希。

## 10. 换电脑或新 Codex 会话的恢复步骤

### 10.1 获取最新仓库

新电脑优先从远端重新克隆仓库。已有仓库先确认工作区干净，再直连同步；没有用户当前授权时不得自行启用代理。

```powershell
git fetch --all --prune
git status --short --branch
git rev-parse HEAD
git rev-parse '@{upstream}'
git stash list
git submodule status
```

只有工作区干净且本地只是落后远端时，才可以执行 `git pull --ff-only`。本地有提交、改动、stash、分叉或 submodule 异常时先停止分析，不能强制覆盖。

### 10.2 恢复工作上下文

新的 Codex 在开始工作前应按顺序执行：

1. 读取系统/用户指令和仓库 `AGENTS.md`；
2. 读取本文件；
3. 读取 `项目优化/README.md` 和当前功能方案；
4. 检查 `项目优化/Claude任务/` 中最近的任务状态；
5. 执行：

```powershell
git status --short --branch
git rev-parse HEAD
git log -5 --oneline --decorate
git remote -v
git diff HEAD --stat
git diff --cached --stat
git ls-files --others --exclude-standard
git stash list
git submodule status
```

6. 如果工作区干净，从最近任务状态继续；
7. 如果工作区有改动，先判断是用户改动、Claude 改动还是 Codex 改动；
8. 不得假设未提交改动已经验证；
9. 如果用户说 Claude 已完成，进入 Codex 审查流程；
10. 如果 Claude 尚未执行，检查并完善任务文档后交还用户；
11. 如果 Claude 执行中断，保留现场并审查部分改动，不得直接 reset；
12. 只有验证完成后才提交和推送。

### 10.3 跨电脑转移未审查代码

最安全的方式是在 Claude 所在电脑上完成 Codex 审查和正式提交后再换电脑。未提交工作区、untracked 文件和 stash 都是本机状态，不会随 `git clone`、`fetch` 或 `pull` 转移。

如果必须在审查前换电脑：

1. 用户明确授权创建临时交接分支；
2. Codex 先检查改动中没有密钥、数据库、日志、用户数据和无关文件；
3. Codex 创建 `handoff/<任务编号>-<日期>` 分支；
4. 以 `wip(handoff): <任务编号> unreviewed changes` 提交并推送；
5. 明确标记该提交“未审查、不得合入 master”；
6. 新电脑检出交接分支后，由 Codex 从完整审查流程继续；
7. 审查通过后用正常任务提交交付，并删除临时远端分支。

临时交接分支只是传输容器，不代表验收通过。不得使用 stash 作为跨电脑传输方案，也不得把**尚未验证或尚未完成**的改动推到 `master`。

已经跑过定向验证的改动不走交接分支：按「Git 交付」直接提交并推送当前分支，审查在提交之后进行（见 10.4）。

### 10.4 审查后置与已推送提交

用户 2026-10-13 明确要求：每轮改动完成并通过定向验证后立即提交并推送，不攒改动、不等再次指示。因此审查不再是提交前的闸门，而是提交后的必做环节：

1. 改动方（Claude 或 Codex）验证通过后提交并推送当前远端分支；
2. Codex 审查已推送的提交和剩余工作区状态，不只看工作区 diff；
3. 发现问题：小问题 Codex 直接修复并补测试，用独立修复提交推送；较大问题生成新的 Claude 修正任务文档；
4. 不改写已推送历史、不 force push、不 reset 远端分支；
5. 只有用户明确要求「先审查再提交」时，才回到提交前闸门模式。

代价是 `master` 上可能短暂存在未审查但已验证的提交；换来的是改动不会滞留在单台机器的工作区里，换电脑或换会话都能从正确基线继续。

## 11. 冲突和异常处理

### Claude 修改范围过大

Codex 不直接全盘接受。先分类：

- 任务必要改动；
- 可以保留但需要解释的改动；
- 无关改动；
- 危险或破坏性改动。

未经用户同意，不使用 `git reset --hard` 或 `git checkout --` 清理。

### Claude 中断但留下部分代码

保留工作区，用户通知 Codex。Codex 检查：

- 哪些模块已完成；
- 是否可以安全运行测试；
- 是否适合由 Codex 补完；
- 是否需要新建一个更小的 Claude 任务。

### 改动被误放入 stash

先执行 `git stash list` 和 `git stash show --name-status --include-untracked 'stash@{N}'`，确认每个文件的归属。需要恢复部分文件时，按明确路径从 stash source 恢复，不要直接 pop 整个 stash。只有目标文件已恢复并验证、其余内容确认不需要后，才允许 drop 对应 stash。

drop 前记录 stash 名称和对象哈希。stash 只存在于当前仓库本机；drop 后通常不能再通过 `git stash` 恢复，底层对象也可能被 Git 垃圾回收。

### 测试无法运行

区分原因：

- 代码错误；
- 缺少本地依赖；
- 需要模型或外部服务；
- 测试本身污染全局配置；
- Windows 兼容问题。

不能把“测试未运行”写成“测试通过”。

### Git 推送失败

保留本地 commit，报告分支领先状态和准确错误。遵守当前项目代理规则，不自行改变系统代理或凭据。

## 12. 当前项目特别约束

针对 Langchain-Chatchat，默认遵守：

- 永久知识库、知识文件和向量索引保持全局共享，除非用户以后明确改变需求；
- 当前用户隔离重点是会话、历史消息、反馈以及临时聊天内容；
- 所有登录用户可以上传和编辑公共知识库；
- 不得为了用户隔离移动或重建公共知识库；
- `chatchat-study-data` 视为运行/学习数据，任务未明确要求时不得读取或修改；
- 数据库迁移必须保守、幂等、可验证，不得静默丢失旧会话；
- Streamlit 换用户时必须清理用户私有状态；
- 谁改动谁交付：改动完成并通过定向验证后立即提交并推送当前分支，不攒改动、不等再次指示；
- Codex 负责最终审查、修复、测试，并交付自己的改动；审查已推送提交时发现的问题用独立修复提交补上。

## 13. 最简操作摘要

```text
用户提出需求
    -> Codex 分析并写 Claude 任务文档
    -> 用户人工上传任务给 Claude Code
    -> Claude Code 编码、定向测试，验证通过后提交并推送
    -> 用户通知 Codex
    -> Codex 审查已推送提交与剩余工作区、补修、复测
    -> Codex 用独立修复提交补上并推送
```

任何新电脑上的 Codex，只要读取本文件、当前任务文档和 Git 状态，就应能够从正确阶段继续工作。
