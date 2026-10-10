import { useRef, useState } from "react";
import { ImagePlus, Loader2, Send, Square, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  streaming,
  placeholder,
  allowImage = true,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: (text: string, imageDataUrl?: string) => void;
  onStop: () => void;
  streaming: boolean;
  placeholder?: string;
  /** 知识库对话等路径不支持图片，隐藏附件入口 */
  allowImage?: boolean;
}) {
  const [imageDataUrl, setImageDataUrl] = useState<string | undefined>();
  const fileRef = useRef<HTMLInputElement>(null);

  function pickImage() {
    fileRef.current?.click();
  }

  function handleFile(file: File | undefined) {
    if (!file) return;
    if (!file.type.startsWith("image/")) return;
    if (file.size > MAX_IMAGE_BYTES) return;
    const reader = new FileReader();
    reader.onload = () => setImageDataUrl(reader.result as string);
    reader.readAsDataURL(file);
  }

  function submit() {
    if (streaming) return;
    if (!value.trim() && !imageDataUrl) return;
    onSend(value, imageDataUrl);
    setImageDataUrl(undefined);
    if (fileRef.current) fileRef.current.value = "";
  }

  return (
    <div className="border-t bg-background pt-3">
      {imageDataUrl && (
        <div className="mb-2 flex items-center gap-2">
          <img
            src={imageDataUrl}
            alt="待发送图片"
            className="max-h-24 rounded-md border"
          />
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6"
            onClick={() => setImageDataUrl(undefined)}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}
      {imageDataUrl && (
        <div className="mb-1 text-xs text-muted-foreground">
          图片解析走 /v1 直通接口：本轮不调用工具/Agent，也不写入会话历史。
        </div>
      )}
      <div className="flex items-end gap-2">
        {allowImage && (
          <>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => handleFile(e.target.files?.[0])}
            />
            <Button
              variant="outline"
              size="icon"
              title="附加图片（需选择视觉模型）"
              onClick={pickImage}
            >
              <ImagePlus className="h-4 w-4" />
            </Button>
          </>
        )}
        <Textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={
            placeholder ?? "输入消息，Enter 发送（Ctrl/⌘+Enter 亦可），Shift+Enter 换行"
          }
          rows={3}
          className="flex-1"
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            } else if (e.key === "Escape" && streaming) {
              e.preventDefault();
              onStop();
            }
          }}
        />
        {streaming ? (
          <Button variant="destructive" onClick={onStop}>
            <Square className="h-4 w-4" />
            停止
          </Button>
        ) : (
          <Button onClick={submit} disabled={!value.trim() && !imageDataUrl}>
            <Send className="h-4 w-4" />
            发送
          </Button>
        )}
      </div>
      {streaming && (
        <div className="flex items-center gap-1 py-1 text-xs text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          正在生成回复…
        </div>
      )}
    </div>
  );
}
