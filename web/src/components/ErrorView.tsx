import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";

export function ErrorView({
  error,
  onRetry,
}: {
  error: Error | string;
  onRetry?: () => void;
}) {
  const message = typeof error === "string" ? error : error.message;
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-6 py-12 text-center">
      <AlertTriangle className="h-6 w-6 text-destructive" />
      <div className="text-sm font-semibold">Something went wrong</div>
      <pre className="max-w-md overflow-auto text-xs text-muted-foreground">{message}</pre>
      {onRetry && (
        <Button variant="secondary" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}
