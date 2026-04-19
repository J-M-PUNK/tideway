import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import type { AuthStatus } from "@/api/types";

export function useAuth() {
  const [state, setState] = useState<AuthStatus & { loading: boolean }>({
    logged_in: false,
    username: null,
    avatar: null,
    loading: true,
  });

  const refresh = useCallback(async () => {
    try {
      const s = await api.auth.status();
      setState({ ...s, loading: false });
    } catch {
      setState({ logged_in: false, username: null, avatar: null, loading: false });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const logout = useCallback(async () => {
    // Refresh unconditionally. If the server-side logout call fails
    // (network, 401), we still want to re-read the status so the UI
    // reflects whatever truth the server actually has instead of
    // leaving the caller with an unhandled rejection.
    try {
      await api.auth.logout();
    } catch {
      /* swallow — refresh() below will surface the real state */
    } finally {
      await refresh();
    }
  }, [refresh]);

  return { ...state, refresh, logout };
}
