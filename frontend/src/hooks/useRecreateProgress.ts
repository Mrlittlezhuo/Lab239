import { useCallback, useState } from "react";

import { streamSse } from "@/lib/sse/stream";

export interface RecreateProgress {
  total: number;
  finished: number;
  doc?: string;
  running: boolean;
  error?: string;
}

/** recreate_vector_store 的 SSE 进度流。 */
export function useRecreateProgress() {
  const [progress, setProgress] = useState<RecreateProgress | null>(null);

  const start = useCallback(async (kbName: string) => {
    setProgress({ total: 0, finished: 0, running: true });
    try {
      await streamSse({
        path: "/knowledge_base/recreate_vector_store",
        body: { knowledge_base_name: kbName, allow_empty_kb: true },
        onEvent: (event) => {
          if (event.kind === "progress") {
            setProgress((prev) => ({
              total: event.total,
              finished: event.finished,
              doc: event.doc,
              running: true,
              error: prev?.error,
            }));
          }
        },
      });
      setProgress((prev) => (prev ? { ...prev, running: false } : prev));
    } catch (err) {
      setProgress((prev) => ({
        total: prev?.total ?? 0,
        finished: prev?.finished ?? 0,
        running: false,
        error: err instanceof Error ? err.message : "重建失败",
      }));
    }
  }, []);

  const clear = useCallback(() => setProgress(null), []);

  return { progress, start, clear };
}
