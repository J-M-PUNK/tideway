import type { PropsWithChildren } from "react";

export function Grid({ children }: PropsWithChildren) {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
      {children}
    </div>
  );
}

export function SectionHeader({ title, action }: { title: string; action?: React.ReactNode }) {
  return (
    <div className="mb-4 mt-8 flex items-end justify-between">
      <h2 className="text-2xl font-bold tracking-tight">{title}</h2>
      {action}
    </div>
  );
}
