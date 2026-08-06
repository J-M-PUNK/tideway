import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type DependencyList,
} from "react";

interface Page<T> {
  items: T[];
  has_more: boolean;
}

/**
 * Offset-paginated infinite list. Loads the first page eagerly, then more
 * as a sentinel scrolls into view — so a listing that used to resolve all
 * 100 albums up front (25s+) instead streams ~18 at a time.
 *
 * `fetchPage(offset)` returns one page; the hook advances the offset by
 * `pageSize` (the backend pages the underlying listing by offset, so we
 * step by the requested size, not by however many resolved). `firstPage`
 * is exposed so callers can read page-0 extras (e.g. genre picker options).
 * The sentinel uses the viewport as root, which works for our nested
 * scroll container in practice; `rootMargin` prefetches before the user
 * hits the bottom.
 */
export function useInfinitePages<T, P extends Page<T>>(
  fetchPage: (offset: number) => Promise<P>,
  pageSize: number,
  deps: DependencyList,
) {
  const [items, setItems] = useState<T[]>([]);
  const [firstPage, setFirstPage] = useState<P | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const offset = useRef(0);
  const busy = useRef(false);
  const hasMoreRef = useRef(false);
  const fetchRef = useRef(fetchPage);
  fetchRef.current = fetchPage;

  useEffect(() => {
    let cancelled = false;
    offset.current = 0;
    busy.current = true;
    hasMoreRef.current = false;
    setItems([]);
    setFirstPage(null);
    setHasMore(false);
    setError(null);
    setLoading(true);
    fetchRef
      .current(0)
      .then((p) => {
        if (cancelled) return;
        setItems(p.items);
        setFirstPage(p);
        offset.current = pageSize;
        setHasMore(p.has_more);
        hasMoreRef.current = p.has_more;
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
        busy.current = false;
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const loadMore = useCallback(() => {
    if (busy.current || !hasMoreRef.current) return;
    busy.current = true;
    setLoadingMore(true);
    const at = offset.current;
    fetchRef
      .current(at)
      .then((p) => {
        setItems((prev) => [...prev, ...p.items]);
        offset.current = at + pageSize;
        setHasMore(p.has_more);
        hasMoreRef.current = p.has_more;
      })
      .catch(() => {
        /* keep what we have; the sentinel re-fires on the next scroll */
      })
      .finally(() => {
        busy.current = false;
        setLoadingMore(false);
      });
  }, [pageSize]);

  const ioRef = useRef<IntersectionObserver | null>(null);
  const sentinelRef = useCallback(
    (el: HTMLElement | null) => {
      ioRef.current?.disconnect();
      if (!el) {
        ioRef.current = null;
        return;
      }
      ioRef.current = new IntersectionObserver(
        (entries) => {
          if (entries.some((e) => e.isIntersecting)) loadMore();
        },
        { rootMargin: "600px" },
      );
      ioRef.current.observe(el);
    },
    [loadMore],
  );

  return {
    items,
    firstPage,
    hasMore,
    loading,
    loadingMore,
    error,
    sentinelRef,
  };
}
