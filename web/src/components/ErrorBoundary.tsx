import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "@/components/ui/button";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("UI crash:", error, info.componentStack);
  }

  private reset = () => this.setState({ error: null });

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex h-screen items-center justify-center bg-background p-6">
        <div className="flex max-w-md flex-col gap-4 rounded-xl border border-border bg-card p-8 text-center shadow-xl">
          <h1 className="text-xl font-bold">Something went wrong.</h1>
          <p className="text-sm text-muted-foreground">
            The UI hit an unexpected error. You can reload the page, or try to dismiss the
            error and keep going.
          </p>
          <pre className="max-h-40 overflow-auto rounded bg-secondary p-3 text-left text-xs text-muted-foreground">
            {this.state.error.message}
          </pre>
          <div className="flex justify-center gap-2">
            <Button variant="secondary" onClick={this.reset}>
              Dismiss
            </Button>
            <Button onClick={() => window.location.reload()}>Reload</Button>
          </div>
        </div>
      </div>
    );
  }
}
