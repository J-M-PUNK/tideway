import type { LucideIcon } from "lucide-react";

interface Props {
  icon: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
}

export function EmptyState({ icon: Icon, title, description, action }: Props) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border/60 px-6 py-16 text-center">
      <div className="rounded-full bg-secondary p-4 text-muted-foreground">
        <Icon className="h-6 w-6" />
      </div>
      <div className="text-base font-semibold">{title}</div>
      {description && (
        <div className="max-w-sm text-sm text-muted-foreground">
          {description}
        </div>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
