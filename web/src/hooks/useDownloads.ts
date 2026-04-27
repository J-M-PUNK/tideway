import { useEffect, useMemo, useState } from "react";
import type { DownloadItem } from "@/api/types";
import { useDownloadStream } from "./useDownloadStream";

export function useDownloads() {
  const stream = useDownloadStream();
  const [items, setItems] = useState<Record<string, DownloadItem>>({});
  // Tracks insertion order so UI rows don't reshuffle as titles arrive.
  const [order, setOrder] = useState<string[]>([]);

  useEffect(() => {
    return stream.subscribe((payload) => {
      if (payload.type === "reset") {
        const arr = (payload.items as DownloadItem[]) ?? [];
        const next: Record<string, DownloadItem> = {};
        for (const it of arr) next[it.id] = it;
        setItems(next);
        setOrder(arr.map((i) => i.id));
        return;
      }
      if (payload.type === "remove") {
        const removeId = payload.id as string;
        setItems((prev) => {
          if (!(removeId in prev)) return prev;
          const next = { ...prev };
          delete next[removeId];
          return next;
        });
        setOrder((prev) => prev.filter((id) => id !== removeId));
        return;
      }
      if (payload.type === "item") {
        const item = (payload.item as DownloadItem) ?? null;
        if (!item?.id) return;
        setItems((prev) => ({ ...prev, [item.id]: item }));
        setOrder((prev) =>
          prev.includes(item.id) ? prev : [...prev, item.id],
        );
      }
    });
  }, [stream]);

  const list = useMemo(
    () => order.map((id) => items[id]).filter((x): x is DownloadItem => !!x),
    [items, order],
  );
  const active = useMemo(
    () => list.filter((i) => i.status !== "Complete" && i.status !== "Failed"),
    [list],
  );
  const completed = useMemo(
    () => list.filter((i) => i.status === "Complete"),
    [list],
  );
  const failed = useMemo(
    () => list.filter((i) => i.status === "Failed"),
    [list],
  );

  return { items: list, active, completed, failed };
}
