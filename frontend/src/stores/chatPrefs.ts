import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ToolMode =
  /** 智能路由（auto_agent：意图路由自动选直接回答/知识库/工具/Agent） */
  | "auto"
  /** 纯模型直答，不调用任何工具 */
  | "plain"
  /** 手动指定工具（tools 列表） */
  | "manual"
  /** 单 Agent 任务闭环（agent_run 调试路径） */
  | "agent_run";

interface ChatPrefsState {
  model: string; // "" 表示使用服务端默认模型
  /** 视觉模型（image2text）；图片解析轮必须显式选择，"" 表示未配置 */
  visionModel: string;
  temperature: number | null; // null 表示使用服务端默认
  toolMode: ToolMode;
  selectedTools: string[];
  useMcp: boolean;
  /** 会话输入草稿（key=conversationId 或 "new"），登出/刷新不丢 */
  drafts: Record<string, string>;
  /**
   * 会话级"本次不使用个人偏好"（key=conversationId 或 "new"）。
   * 只影响当前会话的后续请求，不删除服务端保存的偏好；新会话不受影响。
   */
  memoryOff: Record<string, boolean>;
  /**
   * 会话级"临时对话"（key=conversationId 或 "new"）：新建会话时带上，
   * 服务端把会话标记为 temporary，之后不创建、不保留会话私有上下文，
   * 也不能写入个人偏好（第二期设计方案 §4.4）。会话创建后以服务端标记为准。
   */
  temporary: Record<string, boolean>;
  /**
   * 会话选定的项目空间（key=conversationId 或 "new"，值=project_id）。
   * 只有该项目的范围记忆能在这个会话里被召回（第三期设计方案 §4.4）；
   * 会话创建后以服务端记录为准，这里只负责"新会话还没建好时"的选择。
   */
  projectChoice: Record<string, string>;
  setModel: (model: string) => void;
  setVisionModel: (model: string) => void;
  setTemperature: (t: number | null) => void;
  setToolMode: (mode: ToolMode) => void;
  setSelectedTools: (tools: string[]) => void;
  setUseMcp: (v: boolean) => void;
  setDraft: (conversationKey: string, text: string) => void;
  clearDraft: (conversationKey: string) => void;
  setMemoryOff: (conversationKey: string, off: boolean) => void;
  setTemporary: (conversationKey: string, on: boolean) => void;
  /** 新会话拿到真实 id 后，把 "new" 上的选择搬过去，避免首轮之后失效 */
  mergeMemoryOff: (fromKey: string, toKey: string) => void;
  mergeTemporary: (fromKey: string, toKey: string) => void;
  setProjectChoice: (conversationKey: string, projectId: string) => void;
  clearProjectChoice: (conversationKey: string) => void;
  mergeProjectChoice: (fromKey: string, toKey: string) => void;
}

export const useChatPrefs = create<ChatPrefsState>()(
  persist(
    (set) => ({
      model: "",
      visionModel: "",
      temperature: null,
      toolMode: "auto",
      selectedTools: [],
      useMcp: false,
      drafts: {},
      memoryOff: {},
      temporary: {},
      projectChoice: {},
      setModel: (model) => set({ model }),
      setVisionModel: (visionModel) => set({ visionModel }),
      setTemperature: (temperature) => set({ temperature }),
      setToolMode: (toolMode) => set({ toolMode }),
      setSelectedTools: (selectedTools) => set({ selectedTools }),
      setUseMcp: (useMcp) => set({ useMcp }),
      setDraft: (conversationKey, text) =>
        set((s) => ({ drafts: { ...s.drafts, [conversationKey]: text } })),
      clearDraft: (conversationKey) =>
        set((s) => {
          const next = { ...s.drafts };
          delete next[conversationKey];
          return { drafts: next };
        }),
      setMemoryOff: (conversationKey, off) =>
        set((s) => {
          const next = { ...s.memoryOff };
          if (off) next[conversationKey] = true;
          else delete next[conversationKey];
          return { memoryOff: next };
        }),
      setTemporary: (conversationKey, on) =>
        set((s) => {
          const next = { ...s.temporary };
          if (on) next[conversationKey] = true;
          else delete next[conversationKey];
          return { temporary: next };
        }),
      mergeMemoryOff: (fromKey, toKey) =>
        set((s) => {
          if (!s.memoryOff[fromKey]) return s;
          const next = { ...s.memoryOff, [toKey]: true };
          delete next[fromKey];
          return { memoryOff: next };
        }),
      mergeTemporary: (fromKey, toKey) =>
        set((s) => {
          if (!s.temporary[fromKey]) return s;
          const next = { ...s.temporary, [toKey]: true };
          delete next[fromKey];
          return { temporary: next };
        }),
      setProjectChoice: (conversationKey, projectId) =>
        set((s) => {
          const next = { ...s.projectChoice };
          if (projectId) next[conversationKey] = projectId;
          else delete next[conversationKey];
          return { projectChoice: next };
        }),
      clearProjectChoice: (conversationKey) =>
        set((s) => {
          if (!(conversationKey in s.projectChoice)) return s;
          const next = { ...s.projectChoice };
          delete next[conversationKey];
          return { projectChoice: next };
        }),
      mergeProjectChoice: (fromKey, toKey) =>
        set((s) => {
          if (!s.projectChoice[fromKey]) return s;
          const next = { ...s.projectChoice, [toKey]: s.projectChoice[fromKey] };
          delete next[fromKey];
          return { projectChoice: next };
        }),
    }),
    { name: "chatchat.chatPrefs" },
  ),
);
