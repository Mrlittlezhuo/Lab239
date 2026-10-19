import {
  deleteJson,
  getJson,
  patchJson,
  postJson,
  putJson,
  request,
} from "@/api/http";

/**
 * 师生科研协作接口封装（《学生研究与导师审核功能重新设计方案》里程碑0A/1/2）。
 *
 * 三条必须记住的后端约定：
 * 1. 当前用户只来自服务端令牌，请求体里没有 user_id 可传；
 * 2. 非成员与不存在的资源一律 404，角色不足 403，状态不允许 409，字段非法 400；
 * 3. 页面上看到的「待导师审核 / 退回修改 / 等待重审」等标签由后端 `label`
 *    现算（§5.6），前端不得自己拼混合状态。
 */

// ---------------------------------------------------------------------------
// 枚举取值（与后端 CHECK 约束一一对应）
// ---------------------------------------------------------------------------

export type ProjectRole = "student" | "mentor";
export type ProjectStatus = "draft" | "active" | "archived";
export type TaskStatus = "draft" | "active" | "completed" | "cancelled" | "archived";
export type SubmissionStatus = "submitted" | "withdrawn" | "superseded";
export type ReviewStatus = "not_started" | "drafting" | "published";
export type ReviewDecision =
  | ""
  | "approved"
  | "revision_required"
  | "rejected"
  | "paused";
export type AcConclusion =
  | "met"
  | "partially_met"
  | "not_met"
  | "more_evidence_required"
  | "not_applicable";
export type SelfAssessment =
  | "done"
  | "partially_done"
  | "not_done"
  | "not_applicable";
export type BriefStatus = "draft" | "confirmed" | "rejected" | "superseded";

// ---------------------------------------------------------------------------
// 0A：项目、成员、审计、通知
// ---------------------------------------------------------------------------

export interface ProjectMember {
  user_id: string;
  role: ProjectRole;
  status: "active" | "removed";
  joined_at?: string | null;
  removed_at?: string | null;
}

export interface SupervisionProject {
  project_id: string;
  code: string;
  name: string;
  description: string;
  status: ProjectStatus;
  my_role: ProjectRole | null;
  create_time?: string | null;
  archived_at?: string | null;
  archive_requested_at?: string | null;
  archive_decision_at?: string | null;
}

export interface ProjectDetail extends SupervisionProject {
  members: ProjectMember[];
}

export interface AuditEvent {
  event_code: string;
  object_type: string;
  object_code: string;
  actor_id: string;
  actor_role: string;
  action: string;
  detail: string;
  created_at?: string | null;
}

export interface Notification {
  notification_id: string;
  event_code: string;
  event_type: string;
  project_id: string | null;
  object_type: string;
  object_code: string;
  title: string;
  body: string;
  level: string;
  read: boolean;
  created_at?: string | null;
  read_at?: string | null;
}

/** 绑导师用的最小用户信息：后端只给这三个字段。 */
export interface UserHint {
  user_id: string;
  username: string;
  display_name: string;
}

export const listProjects = () =>
  getJson<SupervisionProject[]>("/supervision/projects");

export const createProject = (body: { name: string; description?: string }) =>
  postJson<ProjectDetail>("/supervision/projects", body);

export const getProject = (projectRef: string) =>
  getJson<ProjectDetail>(`/supervision/projects/${encodeURIComponent(projectRef)}`);

export const activateProject = (projectRef: string) =>
  postJson<ProjectDetail>(`/supervision/projects/${encodeURIComponent(projectRef)}/activate`);

export const addProjectMember = (
  projectRef: string,
  body: { user_id: string; role: ProjectRole },
) =>
  postJson<ProjectDetail>(
    `/supervision/projects/${encodeURIComponent(projectRef)}/members`,
    body,
  );

export const removeProjectMember = (projectRef: string, memberUserId: string) =>
  deleteJson<ProjectDetail>(
    `/supervision/projects/${encodeURIComponent(projectRef)}/members/${encodeURIComponent(memberUserId)}`,
  );

export const requestArchive = (projectRef: string, reason: string) =>
  postJson<ProjectDetail>(
    `/supervision/projects/${encodeURIComponent(projectRef)}/archive/request`,
    { reason },
  );

export const decideArchive = (
  projectRef: string,
  body: { approve: boolean; reason?: string },
) =>
  postJson<ProjectDetail>(
    `/supervision/projects/${encodeURIComponent(projectRef)}/archive/decide`,
    body,
  );

export const deleteProject = (projectRef: string, reason: string) =>
  request<ProjectDetail>(
    `/supervision/projects/${encodeURIComponent(projectRef)}`,
    { method: "DELETE", body: { reason } },
  );

export const listAuditEvents = (
  projectRef: string,
  query?: { object_code?: string; action?: string },
) =>
  getJson<AuditEvent[]>(
    `/supervision/projects/${encodeURIComponent(projectRef)}/audit`,
    { query },
  );

export const listNotifications = (unreadOnly = false) =>
  getJson<Notification[]>("/supervision/notifications", {
    query: { unread_only: unreadOnly },
  });

export const markNotificationsRead = (body: {
  notification_id?: string;
  mark_all?: boolean;
}) => postJson<{ updated: number }>("/supervision/notifications/read", body);

/** 关键字至少两个字符，否则后端返回 400。 */
export const searchUsers = (keyword: string) =>
  getJson<UserHint[]>("/supervision/users", { query: { q: keyword } });

// ---------------------------------------------------------------------------
// 里程碑1：阶段目标、任务与验收标准、进展、提交、审核、附件
// ---------------------------------------------------------------------------

export interface Milestone {
  milestone_id: string;
  code: string;
  title: string;
  description: string;
  success_criteria: string;
  planned_start?: string | null;
  planned_end?: string | null;
  status: string;
  owner_user_id: string;
  mentor_confirmation: string;
  mentor_confirmation_note: string;
  mentor_confirmed_at?: string | null;
  create_time?: string | null;
}

export interface TaskAc {
  ac_code: string;
  statement: string;
  evidence_requirement: string;
  required: boolean;
  check_type: string;
  version: number;
}

export interface Task {
  task_id: string;
  code: string;
  title: string;
  purpose: string;
  description: string;
  status: TaskStatus;
  /** §5.6 派生标签，前端直接显示，不再自行推导 */
  label: string;
  priority: string;
  version: number;
  due_at?: string | null;
  requires_code_review: boolean;
  requires_runtime: boolean;
  required_materials: { key?: string; label?: string; required?: boolean }[];
  milestone_id?: string | null;
  acs: TaskAc[];
  published_at?: string | null;
  cancel_reason: string;
  create_time?: string | null;
}

export interface ProgressNote {
  note_id: string;
  note: string;
  author_id: string;
  author_role: string;
  create_time?: string | null;
}

export interface ProgressUpdate {
  progress_id: string;
  code: string;
  task_id?: string | null;
  milestone_id?: string | null;
  done: string;
  results: string;
  materials: string;
  problems: string;
  next_steps: string;
  links: Record<string, unknown>[];
  author_id: string;
  author_role: string;
  create_time?: string | null;
  notes: ProgressNote[];
}

export interface Submission {
  submission_id: string;
  code: string;
  /** 例如 S-001-v2；接口引用就用它 */
  display_code: string;
  version: number;
  task_id: string;
  status: SubmissionStatus;
  summary: string;
  git_repo: string;
  git_branch: string;
  git_commit: string;
  experiment_notes: string;
  known_issues: string;
  unfinished_items: string;
  self_assessment: Record<string, unknown>[];
  /** 提交当时的验收标准快照；导师审核始终绑定它 */
  ac_snapshot: Record<string, unknown>[];
  materials: Record<string, unknown>[];
  /** §6.4 完整性提醒：只警告，不阻止提交 */
  completeness_check: Record<string, unknown>[];
  author_id: string;
  create_time?: string | null;
  withdrawn_at?: string | null;
  withdraw_reason: string;
}

export interface ReviewAcConclusion {
  ac_code: string;
  conclusion: AcConclusion;
  reason: string;
  ai_relation: string;
}

export interface Review {
  review_id: string;
  code: string;
  submission_id: string;
  task_id: string;
  status: ReviewStatus;
  overall_decision: ReviewDecision;
  decision_reason: string;
  feedback: string;
  must_fix: string[];
  suggestions: string[];
  next_steps: string;
  reviewer_id: string;
  first_touched_at?: string | null;
  published_at?: string | null;
  conclusions: ReviewAcConclusion[];
  notes: ProgressNote[];
}

export interface Attachment {
  attachment_id: string;
  code: string;
  name: string;
  ext: string;
  mime_type: string;
  size: number;
  sha256: string;
  version: number;
  kind: string;
  status: "active" | "replaced" | "removed";
  task_id?: string | null;
  submission_id?: string | null;
  uploaded_by: string;
  create_time?: string | null;
}

export interface TaskInput {
  title: string;
  purpose: string;
  description?: string;
  acs?: {
    ac_code?: string;
    statement: string;
    evidence_requirement?: string;
    required?: boolean;
    check_type?: string;
  }[];
  required_materials?: { key: string; label?: string; required?: boolean }[];
  due_at?: string | null;
  priority?: string;
  requires_code_review?: boolean;
  requires_runtime?: boolean;
  milestone_id?: string;
  publish?: boolean;
}

export interface SubmissionInput {
  summary?: string;
  self_assessment?: {
    ac_code: string;
    self_assessment: SelfAssessment;
    evidence?: string;
    note?: string;
  }[];
  git_repo?: string;
  git_branch?: string;
  git_commit?: string;
  experiment_notes?: string;
  known_issues?: string;
  unfinished_items?: string;
  attachment_codes?: string[];
}

const projectPath = (ref: string, suffix = "") =>
  `/supervision/projects/${encodeURIComponent(ref)}${suffix}`;

export const listMilestones = (projectRef: string) =>
  getJson<Milestone[]>(projectPath(projectRef, "/milestones"));

export const createMilestone = (projectRef: string, body: Record<string, unknown>) =>
  postJson<Milestone>(projectPath(projectRef, "/milestones"), body);

export const updateMilestone = (
  projectRef: string,
  milestoneRef: string,
  body: Record<string, unknown>,
) =>
  patchJson<Milestone>(
    projectPath(projectRef, `/milestones/${encodeURIComponent(milestoneRef)}`),
    body,
  );

export const confirmMilestone = (
  projectRef: string,
  milestoneRef: string,
  body: { confirmation: string; note?: string },
) =>
  postJson<Milestone>(
    projectPath(projectRef, `/milestones/${encodeURIComponent(milestoneRef)}/confirm`),
    body,
  );

export const listTasks = (projectRef: string) =>
  getJson<Task[]>(projectPath(projectRef, "/tasks"));

export const createTask = (projectRef: string, body: TaskInput) =>
  postJson<Task>(projectPath(projectRef, "/tasks"), body);

export const getTask = (projectRef: string, taskRef: string) =>
  getJson<Task>(projectPath(projectRef, `/tasks/${encodeURIComponent(taskRef)}`));

export const updateTask = (
  projectRef: string,
  taskRef: string,
  body: Partial<TaskInput>,
) =>
  patchJson<Task>(
    projectPath(projectRef, `/tasks/${encodeURIComponent(taskRef)}`),
    body,
  );

export const publishTask = (projectRef: string, taskRef: string) =>
  postJson<Task>(projectPath(projectRef, `/tasks/${encodeURIComponent(taskRef)}/publish`));

export const cancelTask = (projectRef: string, taskRef: string, reason: string) =>
  postJson<Task>(
    projectPath(projectRef, `/tasks/${encodeURIComponent(taskRef)}/cancel`),
    { reason },
  );

export const listProgress = (projectRef: string, taskRef?: string) =>
  getJson<ProgressUpdate[]>(projectPath(projectRef, "/progress"), {
    query: taskRef ? { task_ref: taskRef } : undefined,
  });

export const createProgress = (projectRef: string, body: Record<string, unknown>) =>
  postJson<ProgressUpdate>(projectPath(projectRef, "/progress"), body);

export const addProgressNote = (
  projectRef: string,
  progressId: string,
  body: { note: string; kind?: string },
) =>
  postJson<ProgressUpdate>(
    projectPath(projectRef, `/progress/${encodeURIComponent(progressId)}/notes`),
    body,
  );

export const listSubmissions = (projectRef: string, taskRef: string) =>
  getJson<Submission[]>(
    projectPath(projectRef, `/tasks/${encodeURIComponent(taskRef)}/submissions`),
  );

export const createSubmission = (
  projectRef: string,
  taskRef: string,
  body: SubmissionInput,
) =>
  postJson<Submission>(
    projectPath(projectRef, `/tasks/${encodeURIComponent(taskRef)}/submissions`),
    body,
  );

export const getSubmission = (projectRef: string, submissionRef: string) =>
  getJson<Submission>(
    projectPath(projectRef, `/submissions/${encodeURIComponent(submissionRef)}`),
  );

export const withdrawSubmission = (
  projectRef: string,
  submissionRef: string,
  reason: string,
) =>
  postJson<Submission>(
    projectPath(projectRef, `/submissions/${encodeURIComponent(submissionRef)}/withdraw`),
    { reason },
  );

export const listReviewQueue = (projectRef: string) =>
  getJson<Submission[]>(projectPath(projectRef, "/review-queue"));

/** 没有审核记录时后端返回 null，不是 404。 */
export const getReview = (projectRef: string, submissionRef: string) =>
  getJson<Review | null>(
    projectPath(projectRef, `/submissions/${encodeURIComponent(submissionRef)}/review`),
  );

export const saveReviewDraft = (
  projectRef: string,
  submissionRef: string,
  body: Record<string, unknown>,
) =>
  postJson<Review>(
    projectPath(projectRef, `/submissions/${encodeURIComponent(submissionRef)}/review/draft`),
    body,
  );

export const publishReview = (
  projectRef: string,
  submissionRef: string,
  body: Record<string, unknown> & { decision: ReviewDecision; reason?: string },
) =>
  postJson<Review>(
    projectPath(projectRef, `/submissions/${encodeURIComponent(submissionRef)}/review/publish`),
    body,
  );

export const addReviewNote = (
  projectRef: string,
  reviewId: string,
  body: { note: string; kind?: string },
) =>
  postJson<Review>(
    projectPath(projectRef, `/reviews/${encodeURIComponent(reviewId)}/notes`),
    body,
  );

export const listAttachments = (projectRef: string) =>
  getJson<Attachment[]>(projectPath(projectRef, "/attachments"));

export const uploadAttachment = (projectRef: string, form: FormData) =>
  request<Attachment>(projectPath(projectRef, "/attachments"), {
    method: "POST",
    body: form,
  });

export const getAttachment = (projectRef: string, attachmentRef: string) =>
  getJson<Attachment>(
    projectPath(projectRef, `/attachments/${encodeURIComponent(attachmentRef)}`),
  );

export const removeAttachment = (
  projectRef: string,
  attachmentRef: string,
  reason: string,
) =>
  deleteJson<Attachment>(
    projectPath(projectRef, `/attachments/${encodeURIComponent(attachmentRef)}`),
    { body: { reason } },
  );

/** 附件下载要带 Bearer，不能直接丢给 <a href>；调用方 fetch 成 blob。 */
export async function downloadAttachment(
  projectRef: string,
  attachmentRef: string,
): Promise<Blob> {
  const { apiUrl } = await import("@/lib/config");
  const { getAuthToken } = await import("@/stores/auth");
  const res = await fetch(
    apiUrl(
      projectPath(projectRef, `/attachments/${encodeURIComponent(attachmentRef)}/download`),
    ),
    {
      headers: getAuthToken() ? { Authorization: `Bearer ${getAuthToken()}` } : {},
      credentials: "omit",
    },
  );
  if (!res.ok) throw new Error(`下载失败（${res.status}）`);
  return res.blob();
}

// ---------------------------------------------------------------------------
// 里程碑2：项目内研究讨论与结构化研究定义
// ---------------------------------------------------------------------------

export interface ResearchDialogue {
  conversation_id: string;
  code: string;
  // 后端 DialogueOut 里这三个都是 Optional[str]，界面要能兜住空标题。
  name?: string | null;
  chat_type?: string | null;
  supervision_project_id?: string | null;
  message_count: number;
  create_time?: string | null;
  update_time?: string | null;
}

export interface DialogueMessage {
  message_id: string;
  query: string;
  response: string;
  metadata?: Record<string, unknown> | null;
  create_time?: string | null;
}

export interface DialogueMessages {
  conversation_id: string;
  code: string;
  messages: DialogueMessage[];
}

export interface ResearchBrief {
  brief_id: string;
  code: string;
  /** 例如 RB-001-v1 */
  display_code: string;
  project_id: string;
  owner_user_id: string;
  owner_role: ProjectRole;
  kind: "research_brief" | "mentor_guidance";
  title: string;
  status: BriefStatus;
  version_status: string;
  entry_type: string;
  current_version: number;
  shared_version: number | null;
  shown_version: number;
  confirmed_by: string;
  confirmed_at?: string | null;
  confirm_note: string;
  create_time?: string | null;
  update_time?: string | null;
  is_owner: boolean;
  can_edit: boolean;
  source_conversation_id?: string | null;
  source_message_ids: string[];
  background: string;
  question: string;
  objectives: string;
  subjects: string;
  scope: string;
  prior_attempts: string;
  hypotheses: string;
  constraints: string;
  methods: string;
  metrics: string;
  expected_outcomes: string;
  to_verify: string;
  keywords: string[];
  sub_questions: string[];
}

export interface BriefVersion {
  version: number;
  status: BriefStatus;
  entry_type: string;
  edited_fields: string[];
  confirmed_by: string;
  confirmed_at?: string | null;
  confirm_note: string;
  superseded_at?: string | null;
  create_time?: string | null;
  source_message_ids: string[];
  // 14 个内容字段与后端 BriefVersionOut 一一对应（不是 unknown）。
  background: string;
  question: string;
  objectives: string;
  subjects: string;
  scope: string;
  prior_attempts: string;
  hypotheses: string;
  constraints: string;
  methods: string;
  metrics: string;
  expected_outcomes: string;
  to_verify: string;
  keywords: string[];
  sub_questions: string[];
  [key: string]: unknown;
}

/**
 * 版本比较的一条字段差异。
 *
 * 文本字段的 `from`/`to` 是字符串且没有 `added`/`removed`；列表字段
 * （keywords、sub_questions）两边都是字符串数组，并额外给出增删清单
 * （后端 `diff_brief_content`），所以这里必须是联合类型而不是 string。
 */
export interface BriefChange {
  field: string;
  label: string;
  from: string | string[];
  to: string | string[];
  added?: string[];
  removed?: string[];
}

export interface BriefComparison {
  brief_id: string;
  code: string;
  from_version: number;
  to_version: number;
  from_status: BriefStatus;
  to_status: BriefStatus;
  from_confirmed_at?: string | null;
  to_confirmed_at?: string | null;
  changes: BriefChange[];
}

export const createDialogue = (
  projectRef: string,
  body: { title: string; chat_type?: string },
) =>
  postJson<ResearchDialogue>(projectPath(projectRef, "/research/dialogues"), body);

export const listDialogues = (projectRef: string) =>
  getJson<ResearchDialogue[]>(projectPath(projectRef, "/research/dialogues"));

export const getDialogueMessages = (
  projectRef: string,
  conversationId: string,
  limit = 200,
) =>
  getJson<DialogueMessages>(
    projectPath(
      projectRef,
      `/research/dialogues/${encodeURIComponent(conversationId)}/messages`,
    ),
    { query: { limit } },
  );

/**
 * 在项目内研究讨论里发一条言并请求 AI 回复（§15.2 第1、6条）。
 *
 * 后端刻意分两步：发言先落库。模型未接入时返回 503，但发言本身已经保存
 * （`metadata.pending_reply === true`），界面要显示成「已保存，尚未回复」
 * 而不是「发送失败」，并给出补回复入口。
 */
export const sendDialogueMessage = (
  projectRef: string,
  conversationId: string,
  text: string,
) =>
  postJson<DialogueMessage>(
    projectPath(
      projectRef,
      `/research/dialogues/${encodeURIComponent(conversationId)}/messages`,
    ),
    { text },
  );

/** 给一条还没有回复的发言补 AI 回复；已有回复时后端返回 409。 */
export const regenerateDialogueReply = (projectRef: string, messageId: string) =>
  postJson<DialogueMessage>(
    projectPath(projectRef, `/research/messages/${encodeURIComponent(messageId)}/reply`),
    {},
  );

export const listBriefs = (
  projectRef: string,
  query?: { scope?: "mine" | "shared" | "all"; status?: string },
) =>
  getJson<ResearchBrief[]>(projectPath(projectRef, "/research/briefs"), { query });

export const listSharedBriefs = (projectRef: string) =>
  getJson<ResearchBrief[]>(projectPath(projectRef, "/research/shared-briefs"));

export const getBrief = (projectRef: string, briefRef: string) =>
  getJson<ResearchBrief>(
    projectPath(projectRef, `/research/briefs/${encodeURIComponent(briefRef)}`),
  );

export const createBrief = (projectRef: string, body: Record<string, unknown>) =>
  postJson<ResearchBrief>(projectPath(projectRef, "/research/briefs"), body);

export const updateBrief = (
  projectRef: string,
  briefRef: string,
  body: Record<string, unknown>,
) =>
  patchJson<ResearchBrief>(
    projectPath(projectRef, `/research/briefs/${encodeURIComponent(briefRef)}`),
    body,
  );

export const confirmBrief = (projectRef: string, briefRef: string, note = "") =>
  postJson<ResearchBrief>(
    projectPath(projectRef, `/research/briefs/${encodeURIComponent(briefRef)}/confirm`),
    { note },
  );

export const rejectBrief = (projectRef: string, briefRef: string, reason: string) =>
  postJson<ResearchBrief>(
    projectPath(projectRef, `/research/briefs/${encodeURIComponent(briefRef)}/reject`),
    { reason },
  );

export const deleteBrief = (projectRef: string, briefRef: string, reason: string) =>
  request<ResearchBrief>(
    projectPath(projectRef, `/research/briefs/${encodeURIComponent(briefRef)}`),
    { method: "DELETE", body: { reason } },
  );

/** 模型能力未接入时后端返回 503，前端要显示提示而不是白屏。 */
export const extractBrief = (
  projectRef: string,
  body: {
    conversation_id: string;
    message_ids?: string[];
    brief_ref?: string;
    title?: string;
  },
) => postJson<ResearchBrief>(projectPath(projectRef, "/research/briefs/extract"), body);

export const listBriefVersions = (projectRef: string, briefRef: string) =>
  getJson<BriefVersion[]>(
    projectPath(projectRef, `/research/briefs/${encodeURIComponent(briefRef)}/versions`),
  );

export const compareBriefVersions = (
  projectRef: string,
  briefRef: string,
  fromVersion: number,
  toVersion: number,
) =>
  getJson<BriefComparison>(
    projectPath(projectRef, `/research/briefs/${encodeURIComponent(briefRef)}/compare`),
    { query: { from_version: fromVersion, to_version: toVersion } },
  );

/** 只有 Brief 所有者能读来源消息；另一方调用会得到 404。 */
export const getBriefSourceMessages = (
  projectRef: string,
  briefRef: string,
  limit = 200,
) =>
  getJson<DialogueMessages>(
    projectPath(
      projectRef,
      `/research/briefs/${encodeURIComponent(briefRef)}/source-messages`,
    ),
    { query: { limit } },
  );

export const getProjectContext = (projectRef: string) =>
  getJson<{ project_id: string; context: string; has_project_context: boolean }>(
    projectPath(projectRef, "/research/context"),
  );

// ---------------------------------------------------------------------------
// 里程碑4：AI 静态预审（《AI预审功能详细设计方案》）
//
// 三条口径必须留在前端：
// 1. 创建类接口只排队，不调模型；执行是独立的 execute 调用；
// 2. 模型能力未接入时 execute 返回 503，预审本身仍然成立（导师照常审核）；
// 3. AI 状态与导师结论是两个域：AI 的 suggested_conclusion 只有导师显式
//    adopt 之后才会写进导师结论，页面不得把两者混成一个"审核结果"。
// ---------------------------------------------------------------------------

export type AiReviewStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";
export type AiTriggerSource =
  | "student_manual"
  | "mentor_manual"
  | "auto_on_submission";
export type AiVerdict = "satisfied" | "partial" | "not_satisfied" | "unverifiable";

export interface AiReviewRun {
  run_id: string;
  code: string;
  display_code: string;
  revision: number;
  project_id: string;
  task_id: string;
  task_code: string;
  submission_id?: string | null;
  submission_code: string;
  submission_version: number;
  submission_display: string;
  trigger_source: AiTriggerSource;
  triggered_by: string;
  triggered_role: string;
  status: AiReviewStatus;
  summary_state: string;
  summary_note: string;
  ac_version: number;
  total_batches: number;
  completed_batches: number;
  unchecked_acs: string[];
  partial_results: boolean;
  call_plan: Record<string, unknown>[];
  model_calls: Record<string, unknown>[];
  input_budget: Record<string, unknown>;
  material_manifest: Record<string, unknown>[];
  unread_materials: Record<string, unknown>[];
  partially_read_materials: Record<string, unknown>[];
  // 后端是 List[str]（安全提示文本），不是对象数组。
  security_flags: string[];
  finding_counts: Record<string, number>;
  visible_to_mentor: boolean;
  attached_to_submission: boolean;
  check_snapshot: Record<string, unknown>;
  draft_fingerprint: string;
  model_name: string;
  rules_version: string;
  parser_version: string;
  whitelist_version: string;
  budget_config_version: string;
  stale: boolean;
  stale_reasons: string[];
  stale_label: string;
  attach_allowed: boolean;
  queue_status: Record<string, unknown>;
  model_available: boolean;
  failure_reason: string;
  cancel_reason: string;
  cancelled_by: string;
  queued_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  cancelled_at?: string | null;
  create_time?: string | null;
}

export interface AiReviewFinding {
  finding_id: string;
  run_id: string;
  run_code: string;
  ac_code: string;
  ac_statement: string;
  ac_version: number;
  required: boolean;
  check_type: string;
  verdict: AiVerdict;
  evidence_depth: string;
  support_relation: string;
  reason: string;
  satisfied_parts: string;
  missing_parts: string;
  evidence_refs: Record<string, unknown>[];
  // 后端是 List[str]（缺什么材料的名字），不是对象数组。
  missing_materials: string[];
  risks: Record<string, unknown>[];
  mentor_focus: string;
  injection_suspected: boolean;
  injection_note: string;
  runtime_claim_flag: boolean;
  downgrade_reason: string;
  validated: boolean;
  batch_index: number;
}

export interface AiReviewSettings {
  project_id: string;
  task_id: string;
  project_mode: string;
  task_mode: string;
  effective_mode: string;
  auto_review_enabled: boolean;
  default_is_off: boolean;
  priority_note: string;
}

export interface AiStaleness {
  run_code: string;
  stale: boolean;
  reasons: string[];
  based_on: string;
  snapshot_fingerprint: string;
  attach_allowed: boolean;
  rerun_allowed: boolean;
  label: string;
}

export interface AiSuggestion {
  ac_code: string;
  verdict: AiVerdict;
  suggested_conclusion: AcConclusion;
  suggested_label: string;
  reason: string;
  note: string;
}

export interface AiMentorDiff {
  ac_code: string;
  verdict: AiVerdict;
  suggested_conclusion: AcConclusion;
  /** 导师还没填这一条时后端返回空串，不是某个结论值（§13.4）。 */
  mentor_conclusion: AcConclusion | "";
  ai_relation: string;
  consistent: boolean;
}

export interface AiDraftBody {
  summary?: string;
  self_assessment?: {
    ac_code: string;
    self_assessment: SelfAssessment;
    evidence?: string;
    note?: string;
  }[];
  git_repo?: string;
  git_branch?: string;
  git_commit?: string;
  experiment_notes?: string;
  known_issues?: string;
  unfinished_items?: string;
  attachment_codes?: string[];
  core_attachment_codes?: string[];
  force?: boolean;
}

export const studentSelfCheck = (projectRef: string, taskRef: string, body: AiDraftBody) =>
  postJson<AiReviewRun>(
    projectPath(projectRef, `/tasks/${encodeURIComponent(taskRef)}/ai-review/self-check`),
    body,
  );

export const runMentorAiReview = (
  projectRef: string,
  submissionRef: string,
  coreAttachmentCodes: string[] = [],
) =>
  postJson<AiReviewRun>(
    projectPath(
      projectRef,
      `/submissions/${encodeURIComponent(submissionRef)}/ai-review/run`,
    ),
    { core_attachment_codes: coreAttachmentCodes },
  );

export const rerunMentorAiReview = (
  projectRef: string,
  submissionRef: string,
  coreAttachmentCodes: string[] = [],
) =>
  postJson<AiReviewRun>(
    projectPath(
      projectRef,
      `/submissions/${encodeURIComponent(submissionRef)}/ai-review/rerun`,
    ),
    { core_attachment_codes: coreAttachmentCodes },
  );

/** 模型未接入时返回 503；调用方必须把 503 显示成说明，而不是失败。 */
export const executeAiReview = (projectRef: string, runRef: string) =>
  postJson<AiReviewRun>(
    projectPath(projectRef, `/ai-review/${encodeURIComponent(runRef)}/execute`),
  );

export const cancelAiReview = (projectRef: string, runRef: string, reason: string) =>
  postJson<AiReviewRun>(
    projectPath(projectRef, `/ai-review/${encodeURIComponent(runRef)}/cancel`),
    { reason },
  );

export const attachSelfCheck = (
  projectRef: string,
  runRef: string,
  submissionRef: string,
) =>
  postJson<AiReviewRun>(
    projectPath(projectRef, `/ai-review/${encodeURIComponent(runRef)}/attach`),
    { submission_ref: submissionRef },
  );

export const retryAiReview = (projectRef: string, runRef: string) =>
  postJson<AiReviewRun>(
    projectPath(projectRef, `/ai-review/${encodeURIComponent(runRef)}/retry`),
  );

export const listAiReviews = (
  projectRef: string,
  query?: { task_ref?: string; submission_ref?: string },
) =>
  getJson<AiReviewRun[]>(projectPath(projectRef, "/ai-review/runs"), { query });

/** 该任务/提交还没有任何运行时，后端返回 null（不是 404）。 */
export const latestAiReview = (
  projectRef: string,
  taskRef: string,
  submissionRef = "",
) =>
  getJson<AiReviewRun | null>(
    projectPath(projectRef, `/tasks/${encodeURIComponent(taskRef)}/ai-review/latest`),
    { query: submissionRef ? { submission_ref: submissionRef } : undefined },
  );

export const getAiReview = (projectRef: string, runRef: string) =>
  getJson<AiReviewRun>(projectPath(projectRef, `/ai-review/${encodeURIComponent(runRef)}`));

export const listAiFindings = (projectRef: string, runRef: string) =>
  getJson<AiReviewFinding[]>(
    projectPath(projectRef, `/ai-review/${encodeURIComponent(runRef)}/findings`),
  );

/** 草稿内容变化只能靠把当前草稿传给后端比对指纹来发现（§7.4）。 */
export const checkAiReviewStaleness = (
  projectRef: string,
  runRef: string,
  draft?: AiDraftBody,
) =>
  postJson<AiStaleness>(
    projectPath(projectRef, `/ai-review/${encodeURIComponent(runRef)}/staleness`),
    draft ?? {},
  );

export const getAiReviewSettings = (projectRef: string, taskRef = "") =>
  getJson<AiReviewSettings>(projectPath(projectRef, "/ai-review/settings"), {
    query: taskRef ? { task_ref: taskRef } : undefined,
  });

export const setAiReviewSettings = (
  projectRef: string,
  body: { task_ref?: string; mode?: "off" | "on" | "inherit"; enabled?: boolean | null },
) =>
  putJson<AiReviewSettings>(projectPath(projectRef, "/ai-review/settings"), body);

export const listAiSuggestions = (projectRef: string, runRef: string) =>
  getJson<AiSuggestion[]>(
    projectPath(projectRef, `/ai-review/${encodeURIComponent(runRef)}/suggested-conclusions`),
  );

export const listAiMentorDiff = (projectRef: string, runRef: string) =>
  getJson<AiMentorDiff[]>(
    projectPath(projectRef, `/ai-review/${encodeURIComponent(runRef)}/mentor-diff`),
  );

/** 采纳是逐条的：一次 adopt 只影响一条 AC（§13.4、P-22）。 */
export const adoptAiSuggestion = (
  projectRef: string,
  runRef: string,
  acCode: string,
  reason: string,
) =>
  postJson<{
    run_code: string;
    ac_code: string;
    verdict: AiVerdict;
    conclusion: AcConclusion;
    ai_relation: string;
    review_code: string;
  }>(projectPath(projectRef, `/ai-review/${encodeURIComponent(runRef)}/adopt`), {
    ac_code: acCode,
    reason,
  });

// ---------------------------------------------------------------------------
// LaTeX 稿件分析与进展草稿（里程碑5）
//
// 三条硬口径写在这里，免得界面自己发明：
// 1. 全程静态解析，`compiled` 恒为 false，界面不得暗示"已经编译/已运行"；
// 2. 草稿 `requires_student_confirmation` 恒为 true：只有学生本人确认才会
//    生成正式进展，导师调用是 403；
// 3. 危险压缩包、超预算、条目被截断都会原样出现在响应里，界面必须显示。
// ---------------------------------------------------------------------------

export interface LatexSourceLocation {
  path: string;
  line: number;
  end_line: number;
  snippet: string;
}

export interface LatexFinding {
  kind: string;
  severity: "error" | "warning" | "notice" | string;
  message: string;
  path: string;
  line: number;
  end_line: number;
  snippet: string;
  target: string;
  detail: string;
}

export interface LatexSnapshot {
  snapshot_id: string;
  code: string;
  project_id: string;
  attachment_code: string;
  attachment_name: string;
  attachment_sha256: string;
  attachment_version: number;
  main_file: string;
  main_path: string;
  /** resolved / user_specified / ambiguous / not_found / invalid_selection */
  main_status: string;
  main: Record<string, unknown>;
  candidates: Record<string, unknown>[];
  parser_version: string;
  checker_version: string;
  zip_safety_version: string;
  limits: Record<string, unknown>;
  parse_budget: Record<string, unknown>;
  document_present: boolean;
  /** 恒 false：系统绝不调用 LaTeX 编译器。 */
  compiled: boolean;
  counts: Record<string, number>;
  outline: string[];
  word_count: number;
  findings: LatexFinding[];
  finding_counts: Record<string, number>;
  dangerous_findings: LatexFinding[];
  partially_read: Record<string, unknown>[];
  not_read: Record<string, unknown>[];
  unreferenced_tex: string[];
  available_files: string[];
  extracted_files: Record<string, unknown>[];
  file_count: number;
  total_bytes: number;
  document?: Record<string, unknown> | null;
  check?: Record<string, unknown> | null;
  created_by: string;
  created_role: string;
  create_time?: string | null;
}

export interface LatexDraftItem {
  item_type: string;
  /** added / removed / modified / summary */
  change: string;
  statement: string;
  locations: LatexSourceLocation[];
  delta: Record<string, unknown>;
}

export interface LatexProgressDraft {
  draft_id: string;
  code: string;
  project_id: string;
  library_draft_id: string;
  /** draft / baseline / confirmed / discarded */
  status: string;
  /** 恒 false：草稿本身永远不会被系统发布。 */
  published: boolean;
  /** 恒 true：必须学生本人确认。 */
  requires_student_confirmation: boolean;
  baseline_only: boolean;
  draft_version: string;
  parser_version: string;
  checker_version: string;
  zip_safety_version: string;
  snapshot_code: string;
  base_snapshot_code: string;
  attachment_code: string;
  attachment_sha256: string;
  attachment_version: number;
  base_attachment_code: string;
  base_attachment_version: number;
  base_attachment_sha256: string;
  main_file: string;
  base_main_file: string;
  summary: string;
  /** 固定口径说明：静态差异不能证明实验已执行。 */
  caveat: string;
  items: LatexDraftItem[];
  item_count: number;
  truncated: boolean;
  dropped_items: number;
  counts: Record<string, number>;
  word_count_old: number;
  word_count_new: number;
  word_count_delta: number;
  model_summary: string;
  /** "" / generated / unavailable / failed */
  model_summary_status: string;
  model_summary_note: string;
  model_name: string;
  confirmed_progress_code: string;
  confirmed_by: string;
  confirmed_role: string;
  confirmed_at?: string | null;
  discarded_by: string;
  discarded_role: string;
  discarded_at?: string | null;
  discard_reason: string;
  created_by: string;
  created_role: string;
  create_time?: string | null;
  update_time?: string | null;
}

export interface LatexProgress {
  progress_id: string;
  code: string;
  project_id: string;
  task_id?: string | null;
  done: string;
  results: string;
  materials: string;
  problems: string;
  next_steps: string;
  links: Record<string, unknown>[];
  author_id: string;
  author_role: string;
  create_time?: string | null;
}

export interface LatexConfirmResult {
  draft: LatexProgressDraft;
  progress: LatexProgress;
}

export interface LatexDraftConfirmBody {
  task_ref?: string;
  milestone_ref?: string;
  done?: string;
  results?: string;
  materials?: string;
  problems?: string;
  next_steps?: string;
}

export const analyzeLatexArchive = (
  projectRef: string,
  body: { attachment_ref: string; main_file?: string },
) => postJson<LatexSnapshot>(projectPath(projectRef, "/latex/analyze"), body);

export const listLatexSnapshots = (projectRef: string, attachmentRef = "") =>
  getJson<LatexSnapshot[]>(projectPath(projectRef, "/latex/snapshots"), {
    query: attachmentRef ? { attachment_ref: attachmentRef } : undefined,
  });

export const getLatexSnapshot = (projectRef: string, snapshotRef: string) =>
  getJson<LatexSnapshot>(
    projectPath(projectRef, `/latex/snapshots/${encodeURIComponent(snapshotRef)}`),
  );

/** 两个版本做静态差异生成进展草稿；没有 base 时只出基线，不编造进展。 */
export const createLatexProgressDraft = (
  projectRef: string,
  body: {
    attachment_ref: string;
    base_attachment_ref?: string;
    main_file?: string;
    base_main_file?: string;
  },
) => postJson<LatexProgressDraft>(projectPath(projectRef, "/latex/progress-draft"), body);

export const listLatexProgressDrafts = (
  projectRef: string,
  query?: { status?: string; attachment_ref?: string },
) =>
  getJson<LatexProgressDraft[]>(projectPath(projectRef, "/latex/progress-drafts"), {
    query,
  });

export const getLatexProgressDraft = (projectRef: string, draftRef: string) =>
  getJson<LatexProgressDraft>(
    projectPath(projectRef, `/latex/progress-drafts/${encodeURIComponent(draftRef)}`),
  );

/** 只有草稿创建者本人（学生）能确认；确认后生成一条正式进展 PR-NNN。 */
export const confirmLatexProgressDraft = (
  projectRef: string,
  draftRef: string,
  body: LatexDraftConfirmBody,
) =>
  postJson<LatexConfirmResult>(
    projectPath(projectRef, `/latex/progress-drafts/${encodeURIComponent(draftRef)}/confirm`),
    body,
  );

/** 丢弃只改状态，草稿与快照都保留。 */
export const discardLatexProgressDraft = (
  projectRef: string,
  draftRef: string,
  reason = "",
) =>
  postJson<LatexProgressDraft>(
    projectPath(projectRef, `/latex/progress-drafts/${encodeURIComponent(draftRef)}/discard`),
    { reason },
  );

// ---------------------------------------------------------------------------
// 论文检索与文献结论（《学生研究与导师审核功能重新设计方案》里程碑3）
//
// 三条必须照后端显示的口径：
// 1. 检索依据必须是已确认并共享的研究定义版本，只有草稿时后端 409 并说明原因；
// 2. 外部论文来源尚未接入 → 503，界面显示成「来源未接入」而不是检索失败；
// 3. 结论按版本保留（旧版本标 superseded），文献快照哈希用于判定依据已变。
// ---------------------------------------------------------------------------

export interface LiteratureSubtask {
  subtask_id: string;
  code: string;
  kind: string;
  /** pending / queued / running / completed / failed / cancelled */
  status: string;
  attempt: number;
  source: string;
  query: string;
  payload: Record<string, unknown>;
  result_total: number;
  new_total: number;
  duplicate_total: number;
  error_code: string;
  error_message: string;
  failure_history: Record<string, unknown>[];
  model_calls: Record<string, unknown>[];
  consumer: string;
  model_name: string;
  prompt_version: string;
  queued_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  cancelled_by: string;
  cancelled_at?: string | null;
  cancel_reason: string;
  created_by: string;
  created_role: string;
  create_time?: string | null;
  update_time?: string | null;
}

export interface LiteratureTask {
  task_id: string;
  code: string;
  project_id: string;
  /** draft / pending / searching / triage / concluded / closed */
  status: string;
  /** verify / supplement / refute / explore */
  purpose: string;
  goal: string;
  keywords: string[];
  synonyms: string[];
  exclude_terms: string[];
  sources: string[];
  year_from?: number | null;
  year_to?: number | null;
  require_fulltext: boolean;
  result_limit: number;
  deep_extract_limit: number;
  brief_id: string;
  brief_code: string;
  brief_version: number;
  brief_kind: string;
  brief_confirmed_by: string;
  brief_confirmed_at?: string | null;
  brief_snapshot: Record<string, unknown>;
  /** 依据的研究定义版本已被更新版取代：结论要提示"依据已变" */
  brief_superseded: boolean;
  brief_shared_version?: number | null;
  brief_latest_version?: number | null;
  search_plan: Record<string, unknown>;
  search_plan_note: string;
  search_plan_model: string;
  search_plan_prompt_version: string;
  search_plan_generated_at?: string | null;
  search_plan_confirmed_at?: string | null;
  search_plan_confirmed_by: string;
  plan_status: string;
  plan_error: string;
  hits_total: number;
  deduped_total: number;
  stored_total: number;
  source_failures: Record<string, unknown>[];
  searched_at?: string | null;
  concluded_at?: string | null;
  closed_at?: string | null;
  close_reason: string;
  created_by: string;
  created_role: string;
  create_time?: string | null;
  update_time?: string | null;
  result_total: number;
  included_total: number;
  excluded_total: number;
  read_later_total: number;
  undecided_total: number;
  deep_extract_used: number;
  conclusion_total: number;
  subtask_total: number;
  subtask_failed_total: number;
  recent_subtasks: LiteratureSubtask[];
}

export interface LiteratureEvidenceCard {
  card_id: string;
  code: string;
  project_id: string;
  result_id: string;
  result_code: string;
  extraction_id?: string | null;
  extraction_version: number;
  evidence_type: string;
  summary: string;
  quote: string;
  locator: Record<string, unknown>;
  /** 引句是否能在标题+摘要里逐字对上（v1 只有摘要级定位） */
  quote_verified: boolean;
  level: string;
  support_relation: string;
  brief_field: string;
  sub_question: string;
  created_by: string;
  created_role: string;
  create_time?: string | null;
  updated_at?: string | null;
}

export interface LiteratureResult {
  result_id: string;
  code: string;
  project_id: string;
  search_task_id: string;
  subtask_id: string;
  dedup_key: string;
  doi?: string | null;
  arxiv_id?: string | null;
  title_key?: string | null;
  title: string;
  authors: string;
  year?: number | null;
  abstract: string;
  url?: string | null;
  source: string;
  sources: Record<string, unknown>[];
  /** unknown / abstract / url / pdf */
  fulltext_status: string;
  /** high / relevant / reference / low */
  relevance_level: string;
  relevance_reason: string;
  relevance_matched: Record<string, unknown>[];
  relevance_missing: Record<string, unknown>[];
  relevance_score_source: string;
  /** v1 恒为 null：只有规则排序，没有模型分数 */
  relevance_score?: number | null;
  relevance_rules_version: string;
  /** priority_read / reference / low_relevant */
  recommended_action: string;
  /** AI 摘要级语义分析；绝不覆盖规则字段 */
  semantic_analysis: Record<string, unknown>;
  /** pending / included / excluded / read_later */
  decision: string;
  decision_reason: string;
  decided_by: string;
  decided_role: string;
  decided_at?: string | null;
  decision_history: Record<string, unknown>[];
  /** none / queued / running / completed / failed / cancelled */
  extract_state: string;
  evidence_cards: LiteratureEvidenceCard[];
  latest_extraction_version: number;
  first_seen_at?: string | null;
  create_time?: string | null;
  update_time?: string | null;
}

export interface LiteratureExtraction {
  extraction_id: string;
  project_id: string;
  result_id: string;
  result_code: string;
  version: number;
  subtask_id: string;
  subtask_code: string;
  /** fulltext / abstract：v1 只有 abstract */
  level: string;
  summary: string;
  research_problem: string;
  method: string;
  algorithm: string;
  experiments: string;
  results_metrics: string;
  limitations: string;
  differences: Record<string, unknown>;
  evidence: Record<string, unknown>[];
  model_name: string;
  prompt_version: string;
  created_by: string;
  created_role: string;
  create_time?: string | null;
  superseded_at?: string | null;
}

export interface LiteratureConclusion {
  conclusion_id: string;
  code: string;
  version: number;
  project_id: string;
  search_task_id?: string | null;
  search_task_code: string;
  statement: string;
  supported_facts: Record<string, unknown>[];
  student_judgements: Record<string, unknown>[];
  unverified_hypotheses: Record<string, unknown>[];
  research_gaps: string[];
  literature_snapshot: Record<string, unknown>[];
  literature_snapshot_hash: string;
  literature_count: number;
  evidence_cards: Record<string, unknown>[];
  /** draft / confirmed / superseded */
  status: string;
  confirmed_by: string;
  confirmed_at?: string | null;
  confirm_note: string;
  /** "" / confirmed / questioned / evidence_required */
  mentor_state: string;
  mentor_note: string;
  mentor_by: string;
  mentor_role: string;
  mentor_at?: string | null;
  superseded_at?: string | null;
  superseded_by_code: string;
  superseded_by_version: number;
  /** 结论绑定的文献集合已变化：结论依据不再对应当前纳入集合 */
  literature_changed: boolean;
  current_included_count: number;
  created_by: string;
  created_role: string;
  create_time?: string | null;
  update_time?: string | null;
}

export interface LiteratureCitation {
  citation_id: string;
  code: string;
  project_id: string;
  /** project_result / conclusion / private_paper / private_evidence_card */
  source_kind: string;
  source_ref: string;
  source_code: string;
  source_owner_id: string;
  /** mentor_review / research_plan / submission / progress */
  purpose: string;
  bound_object_type: string;
  bound_object_code: string;
  snapshot: Record<string, unknown>;
  source_version: string;
  content_hash: string;
  granted_by: string;
  granted_role: string;
  granted_at?: string | null;
  note: string;
  revoked_at?: string | null;
  revoked_by: string;
  revoke_reason: string;
  create_time?: string | null;
}

export interface LiteratureTaskBody {
  brief_ref: string;
  goal: string;
  purpose?: string;
  keywords?: string[];
  synonyms?: string[];
  exclude_terms?: string[];
  sources?: string[];
  year_from?: number | null;
  year_to?: number | null;
  require_fulltext?: boolean;
  result_limit?: number;
  deep_extract_limit?: number | null;
  as_draft?: boolean;
}

export interface LiteratureConclusionBody {
  statement: string;
  supported_facts?: Record<string, unknown>[];
  student_judgements?: Record<string, unknown>[];
  unverified_hypotheses?: Record<string, unknown>[];
  research_gaps?: string[];
  evidence_card_codes?: string[];
  task_ref?: string;
  confirm?: boolean;
}

export const createLiteratureTask = (projectRef: string, body: LiteratureTaskBody) =>
  postJson<LiteratureTask>(projectPath(projectRef, "/literature/tasks"), body);

export const listLiteratureTasks = (projectRef: string, status = "") =>
  getJson<LiteratureTask[]>(projectPath(projectRef, "/literature/tasks"), {
    query: status ? { status } : undefined,
  });

export const getLiteratureTask = (projectRef: string, taskRef: string) =>
  getJson<LiteratureTask>(
    projectPath(projectRef, `/literature/tasks/${encodeURIComponent(taskRef)}`),
  );

export const updateLiteratureTask = (
  projectRef: string,
  taskRef: string,
  body: Partial<Omit<LiteratureTaskBody, "brief_ref" | "as_draft">>,
) =>
  patchJson<LiteratureTask>(
    projectPath(projectRef, `/literature/tasks/${encodeURIComponent(taskRef)}`),
    body,
  );

export const closeLiteratureTask = (projectRef: string, taskRef: string, reason: string) =>
  postJson<LiteratureTask>(
    projectPath(projectRef, `/literature/tasks/${encodeURIComponent(taskRef)}/close`),
    { reason },
  );

export const generateLiteraturePlan = (projectRef: string, taskRef: string, note = "") =>
  postJson<LiteratureTask>(
    projectPath(projectRef, `/literature/tasks/${encodeURIComponent(taskRef)}/plan`),
    { note },
  );

export const confirmLiteraturePlan = (projectRef: string, taskRef: string, note = "") =>
  postJson<LiteratureTask>(
    projectPath(projectRef, `/literature/tasks/${encodeURIComponent(taskRef)}/plan/confirm`),
    { note },
  );

export const runLiteratureSearch = (
  projectRef: string,
  taskRef: string,
  body: { sources?: string[]; keyword_batches?: Record<string, unknown>[] } = {},
) =>
  postJson<LiteratureTask>(
    projectPath(projectRef, `/literature/tasks/${encodeURIComponent(taskRef)}/search`),
    body,
  );

export const listLiteratureSubtasks = (
  projectRef: string,
  taskRef: string,
  query?: { kind?: string; status?: string; limit?: number },
) =>
  getJson<LiteratureSubtask[]>(
    projectPath(projectRef, `/literature/tasks/${encodeURIComponent(taskRef)}/subtasks`),
    { query },
  );

export const retryLiteratureSubtask = (projectRef: string, taskRef: string, subtaskRef: string) =>
  postJson<LiteratureSubtask>(
    projectPath(
      projectRef,
      `/literature/tasks/${encodeURIComponent(taskRef)}/subtasks/${encodeURIComponent(subtaskRef)}/retry`,
    ),
    {},
  );

export const cancelLiteratureSubtask = (
  projectRef: string,
  taskRef: string,
  subtaskRef: string,
  note = "",
) =>
  postJson<LiteratureSubtask>(
    projectPath(
      projectRef,
      `/literature/tasks/${encodeURIComponent(taskRef)}/subtasks/${encodeURIComponent(subtaskRef)}/cancel`,
    ),
    { note },
  );

export const listLiteratureResults = (
  projectRef: string,
  query?: { task_ref?: string; decision?: string; relevance?: string; limit?: number; offset?: number },
) =>
  getJson<LiteratureResult[]>(projectPath(projectRef, "/literature/results"), { query });

export const getLiteratureResult = (projectRef: string, resultRef: string) =>
  getJson<LiteratureResult>(
    projectPath(projectRef, `/literature/results/${encodeURIComponent(resultRef)}`),
  );

export const decideLiteratureResult = (
  projectRef: string,
  resultRef: string,
  body: { decision: string; reason: string },
) =>
  postJson<LiteratureResult>(
    projectPath(projectRef, `/literature/results/${encodeURIComponent(resultRef)}/decision`),
    body,
  );

export const runLiteratureDeepExtract = (projectRef: string, resultRef: string) =>
  postJson<LiteratureResult>(
    projectPath(projectRef, `/literature/results/${encodeURIComponent(resultRef)}/deep-extract`),
    {},
  );

export const getLiteratureExtraction = (projectRef: string, resultRef: string) =>
  getJson<LiteratureExtraction | null>(
    projectPath(projectRef, `/literature/results/${encodeURIComponent(resultRef)}/extraction`),
  );

export const listLiteratureExtractions = (projectRef: string, resultRef: string) =>
  getJson<LiteratureExtraction[]>(
    projectPath(projectRef, `/literature/results/${encodeURIComponent(resultRef)}/extractions`),
  );

export const createLiteratureEvidenceCard = (
  projectRef: string,
  body: {
    result_ref: string;
    quote: string;
    evidence_type?: string;
    summary?: string;
    locator?: Record<string, unknown>;
    support_relation?: string;
    brief_field?: string;
    sub_question?: string;
    level?: string;
    extraction_id?: string;
  },
) => postJson<LiteratureEvidenceCard>(projectPath(projectRef, "/literature/evidence-cards"), body);

export const listLiteratureEvidenceCards = (projectRef: string, resultRef = "") =>
  getJson<LiteratureEvidenceCard[]>(projectPath(projectRef, "/literature/evidence-cards"), {
    query: resultRef ? { result_ref: resultRef } : undefined,
  });

export const runLiteratureAbstractAnalysis = (
  projectRef: string,
  taskRef: string,
  body: { result_refs?: string[]; limit?: number | null } = {},
) =>
  postJson<LiteratureSubtask>(
    projectPath(projectRef, `/literature/tasks/${encodeURIComponent(taskRef)}/abstract-analysis`),
    body,
  );

export const compareLiteraturePapers = (projectRef: string, taskRef: string, resultRefs: string[]) =>
  postJson<LiteratureSubtask>(
    projectPath(projectRef, `/literature/tasks/${encodeURIComponent(taskRef)}/compare`),
    { result_refs: resultRefs },
  );

export const createLiteratureConclusion = (projectRef: string, body: LiteratureConclusionBody) =>
  postJson<LiteratureConclusion>(projectPath(projectRef, "/literature/conclusions"), body);

export const listLiteratureConclusions = (
  projectRef: string,
  query?: { status?: string; include_superseded?: boolean },
) =>
  getJson<LiteratureConclusion[]>(projectPath(projectRef, "/literature/conclusions"), { query });

export const getLiteratureConclusion = (projectRef: string, conclusionRef: string) =>
  getJson<LiteratureConclusion>(
    projectPath(projectRef, `/literature/conclusions/${encodeURIComponent(conclusionRef)}`),
  );

export const reviseLiteratureConclusion = (
  projectRef: string,
  conclusionRef: string,
  body: LiteratureConclusionBody,
) =>
  postJson<LiteratureConclusion>(
    projectPath(projectRef, `/literature/conclusions/${encodeURIComponent(conclusionRef)}/revise`),
    body,
  );

export const mentorReviewLiteratureConclusion = (
  projectRef: string,
  conclusionRef: string,
  body: { state: string; note?: string },
) =>
  postJson<LiteratureConclusion>(
    projectPath(
      projectRef,
      `/literature/conclusions/${encodeURIComponent(conclusionRef)}/mentor-review`,
    ),
    body,
  );

export const createLiteratureCitation = (
  projectRef: string,
  body: {
    source_kind: string;
    source_ref: string;
    purpose: string;
    bound_object_type?: string;
    bound_object_code?: string;
    note?: string;
  },
) => postJson<LiteratureCitation>(projectPath(projectRef, "/literature/citations"), body);

export const listLiteratureCitations = (
  projectRef: string,
  query?: { purpose?: string; source_kind?: string; include_revoked?: boolean },
) => getJson<LiteratureCitation[]>(projectPath(projectRef, "/literature/citations"), { query });

export const revokeLiteratureCitation = (projectRef: string, citationRef: string, reason: string) =>
  postJson<LiteratureCitation>(
    projectPath(projectRef, `/literature/citations/${encodeURIComponent(citationRef)}/revoke`),
    { reason },
  );
