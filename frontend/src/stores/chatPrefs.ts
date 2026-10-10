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
  setModel: (model: string) => void;
  setVisionModel: (model: string) => void;
  setTemperature: (t: number | null) => void;
  setToolMode: (mode: ToolMode) => void;
  setSelectedTools: (tools: string[]) => void;
  setUseMcp: (v: boolean) => void;
  setDraft: (conversationKey: string, text: string) => void;
  clearDraft: (conversationKey: string) => void;
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
    }),
    { name: "chatchat.chatPrefs" },
  ),
);
