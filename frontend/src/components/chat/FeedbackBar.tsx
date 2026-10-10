import { useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { toast } from "sonner";

import { sendFeedback } from "@/api/chat";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";

/**
 * 消息反馈：👍=100 👎=0（服务端 score 0-100）。
 * 只有拿到服务端 message_id 的消息才能反馈。
 */
export function FeedbackBar({
  messageId,
  score,
  onScored,
}: {
  messageId?: string;
  score?: number;
  onScored: (score: number) => void;
}) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState<null | number>(null);
  if (!messageId) return null;

  const scored = typeof score === "number" && score >= 0;
  const id = messageId;

  async function submit(nextScore: number, nextReason = "") {
    setPending(nextScore);
    try {
      await sendFeedback(id, nextScore, nextReason);
      onScored(nextScore);
      toast.success("已记录反馈");
      setDialogOpen(false);
      setReason("");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "反馈失败");
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="mt-1 flex items-center gap-1">
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        title="有帮助"
        disabled={pending !== null}
        onClick={() => submit(100)}
      >
        <ThumbsUp
          className={`h-3.5 w-3.5 ${scored && (score ?? 0) >= 50 ? "text-emerald-600" : ""}`}
        />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        title="有问题"
        disabled={pending !== null}
        onClick={() => setDialogOpen(true)}
      >
        <ThumbsDown
          className={`h-3.5 w-3.5 ${scored && (score ?? 0) < 50 ? "text-destructive" : ""}`}
        />
      </Button>
      {scored && (
        <span className="text-xs text-muted-foreground">已反馈</span>
      )}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>反馈问题</DialogTitle>
          </DialogHeader>
          <Textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="哪里有问题？（可留空）"
          />
          <Button onClick={() => submit(0, reason)} disabled={pending !== null}>
            提交
          </Button>
        </DialogContent>
      </Dialog>
    </div>
  );
}
