import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import type { AuthStatus } from "@/api/types";

export function useAuth() {
  const [state, setState] = useState<AuthStatus & { loading: boolean }>({
    logged_in: false,
    username: null,
    loading: true,
  });

  const refresh = useCallback(async () => {
    try {
      const s = await api.auth.status();
      setState({ ...s, loading: false });
    } catch {
      setState({ logged_in: false, username: null, loading: false });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const logout = useCallback(async () => {
    await api.auth.logout();
    await refresh();
  }, [refresh]);

  return { ...state, refresh, logout };
}
