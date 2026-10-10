import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/** 路由级错误边界：某个页面崩溃时不白屏，保留侧栏并可重试。 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("页面渲染异常", error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
        <AlertTriangle className="h-8 w-8 text-destructive" />
        <div className="text-base font-medium">页面出错了</div>
        <pre className="max-w-xl overflow-x-auto rounded-md bg-muted/40 p-3 text-xs text-muted-foreground">
          {this.state.error.message}
        </pre>
        <Button
          size="sm"
          variant="outline"
          onClick={() => this.setState({ error: null })}
        >
          <RotateCcw className="h-4 w-4" />
          重试
        </Button>
      </div>
    );
  }
}
