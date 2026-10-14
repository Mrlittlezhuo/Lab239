import { detectIntents, type MemoryProposal } from "@/api/memory";

/**
 * 发送用户消息时顺带检测“记住 / 忘记”意图（第二期设计方案 §4.3）。
 *
 * 只把用户本轮输入发给服务端，因此知识库文档片段与模型回答不会触发卡片
 * （P2-12）。这一步与流式回答并行，失败也只影响卡片本身——不能因为它
 * 出错就打断正常回答，所以这里吞掉所有异常并返回空列表。
 *
 * 服务端这一步不写任何数据：写入只发生在用户点「确认保存」之后。
 */
export async function detectProposals(
  text: string,
  conversationId?: string,
): Promise<MemoryProposal[]> {
  const trimmed = (text || "").trim();
  if (!trimmed) return [];
  try {
    const res = await detectIntents(trimmed, conversationId ?? null);
    return Array.isArray(res?.proposals) ? res.proposals : [];
  } catch {
    return [];
  }
}
