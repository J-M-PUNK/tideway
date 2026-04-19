import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { CheckCircle2, Info, X, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";

export type ToastKind = "success" | "error" | "info";

export interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  description?: string;
  durationMs: number;
}

interface ToastContextValue {
  show: (t: Omit<Toast, "id" | "durationMs"> & { durationMs?: number }) => void;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  // Track active dismissal timers so provider unmount (e.g. HMR,
  // user logout) doesn't leak queued setTimeout handles that would
  // later try to update unmounted state.
  const timersRef = useRef<Set<number>>(new Set());

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const show = useCallback<ToastContextValue["show"]>(
    (t) => {
      const id = nextId.current++;
      const durationMs = t.durationMs ?? (t.kind === "error" ? 6000 : 3000);
      // Spread the caller payload BEFORE id/durationMs so a caller passing
      // `durationMs: undefined` explicitly can't wipe the resolved value
      // (and so the resolved id always wins).
      setToasts((prev) => [...prev, { ...t, id, durationMs }]);
      const handle = window.setTimeout(() => {
        timersRef.current.delete(handle);
        dismiss(id);
      }, durationMs);
      timersRef.current.add(handle);
    },
    [dismiss],
  );

  useEffect(() => {
    return () => {
      timersRef.current.forEach((h) => window.clearTimeout(h));
      timersRef.current.clear();
    };
  }, []);

  const value = useMemo(() => ({ show, dismiss }), [show, dismiss]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-28 right-4 z-[60] flex w-80 flex-col gap-2">
        {toasts.map((t) => (
          <ToastView key={t.id} toast={t} onDismiss={() => dismiss(t.id)} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

function ToastView({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  const [entering, setEntering] = useState(true);
  useEffect(() => {
    const t = window.setTimeout(() => setEntering(false), 10);
    return () => window.clearTimeout(t);
  }, []);

  const Icon = toast.kind === "success" ? CheckCircle2 : toast.kind === "error" ? XCircle : Info;
  const tint =
    toast.kind === "success"
      ? "text-primary"
      : toast.kind === "error"
        ? "text-destructive"
        : "text-muted-foreground";

  return (
    <div
      role="status"
      className={cn(
        "pointer-events-auto flex items-start gap-3 rounded-lg border border-border bg-card/95 px-4 py-3 shadow-xl backdrop-blur-sm transition-all",
        entering ? "translate-x-6 opacity-0" : "translate-x-0 opacity-100",
      )}
    >
      <Icon className={cn("mt-0.5 h-4 w-4 flex-shrink-0", tint)} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold">{toast.title}</div>
        {toast.description && (
          <div className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
            {toast.description}
          </div>
        )}
      </div>
      <button
        onClick={onDismiss}
        className="-m-1 rounded p-1 text-muted-foreground hover:text-foreground"
        aria-label="Dismiss"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}
