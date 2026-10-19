import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { listNotifications, markNotificationsRead } from "@/api/supervision";
import type { Notification } from "@/api/supervision";
import { Button } from "@/components/ui/button";

function errorText(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "通知加载失败";
}

const LEVEL_LABEL: Record<string, string> = {
  info: "",
  success: "",
  warning: "（提醒）",
  error: "（异常）",
};

/**
 * 站内通知铃铛：只显示未读，点开后可全部标记已读。
 * 通知由服务端在任务下发、提交、审核等动作里生成，前端不自己造事件。
 */
export function NotificationsBell() {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const notifications = useQuery({
    queryKey: ["supervision", "notifications", "unread"],
    queryFn: () => listNotifications(true),
    refetchInterval: 60_000,
  });

  const markAll = useMutation({
    mutationFn: () => markNotificationsRead({ mark_all: true }),
    onSuccess: () => {
      toast.success("已全部标记为已读");
      queryClient.invalidateQueries({ queryKey: ["supervision", "notifications"] });
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const items = notifications.data ?? [];
  const unread = items.length;

  return (
    <div className="relative">
      <Button
        variant="ghost"
        size="sm"
        className="relative w-full justify-start"
        onClick={() => setOpen((value) => !value)}
      >
        <Bell className="h-4 w-4" />
        通知
        {unread > 0 && (
          <span className="ml-auto rounded-full bg-primary px-1.5 text-xs text-primary-foreground">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </Button>

      {open && (
        <div className="absolute left-0 right-0 z-20 mt-1 max-h-80 overflow-y-auto rounded-md border bg-popover p-2 text-popover-foreground shadow-md">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-sm font-medium">未读通知</span>
            {unread > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => markAll.mutate()}
                disabled={markAll.isPending}
              >
                全部已读
              </Button>
            )}
          </div>
          {notifications.isLoading && (
            <p className="text-xs text-muted-foreground">加载中…</p>
          )}
          {notifications.isError && (
            <p className="text-xs text-destructive">
              {errorText(notifications.error)}
            </p>
          )}
          {!notifications.isLoading && items.length === 0 && (
            <p className="text-xs text-muted-foreground">没有未读通知。</p>
          )}
          <ul className="space-y-2">
            {items.map((item) => (
              <NotificationRow
                key={item.notification_id}
                item={item}
                onOpen={() => {
                  setOpen(false);
                  if (item.project_id) navigate(`/supervision/${item.project_id}`);
                }}
              />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function NotificationRow({
  item,
  onOpen,
}: {
  item: Notification;
  onOpen: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        className="w-full rounded-sm p-2 text-left text-xs hover:bg-accent hover:text-accent-foreground"
      >
        <div className="font-medium">
          {item.title}
          {LEVEL_LABEL[item.level] ?? ""}
        </div>
        {item.body && <div className="text-muted-foreground">{item.body}</div>}
        <div className="mt-1 text-[11px] text-muted-foreground">
          {item.event_code}
          {item.object_code ? ` · ${item.object_code}` : ""}
        </div>
      </button>
    </li>
  );
}
