import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { api, setAuthToken } from "./api";

const STORAGE_KEY = "tracex.auth";

type StoredAuth = { token: string; actor: string };

type AuthContextValue = {
  token: string | null;
  actor: string | null;
  signup: (displayName: string, password: string) => Promise<void>;
  login: (displayName: string, password: string) => Promise<void>;
  logout: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

function readStored(): StoredAuth | null {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as StoredAuth;
  } catch {
    return null;
  }
}

// Sync the api client's token at module-load time (before any component mounts or
// fetches), not inside a useEffect — an effect fires after child pages' own fetch
// effects on a fresh page load, racing a request out with no Authorization header.
setAuthToken(readStored()?.token ?? null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [stored, setStored] = useState<StoredAuth | null>(() => readStored());

  const value = useMemo<AuthContextValue>(
    () => ({
      token: stored?.token ?? null,
      actor: stored?.actor ?? null,
      signup: async (displayName, password) => {
        const result = await api.signup(displayName, password);
        localStorage.setItem(STORAGE_KEY, JSON.stringify(result));
        setAuthToken(result.token);
        setStored(result);
      },
      login: async (displayName, password) => {
        const result = await api.login(displayName, password);
        localStorage.setItem(STORAGE_KEY, JSON.stringify(result));
        setAuthToken(result.token);
        setStored(result);
      },
      logout: () => {
        localStorage.removeItem(STORAGE_KEY);
        setAuthToken(null);
        setStored(null);
      },
    }),
    [stored]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider");
  return context;
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const { token } = useAuth();
  if (!token) return <Navigate to="/" replace />;
  return children;
}

export function initials(name: string | null): string {
  if (!name) return "?";
  const parts = name.trim().split(/\s+/);
  return (parts[0]?.[0] ?? "").concat(parts[1]?.[0] ?? "").toUpperCase() || name.slice(0, 2).toUpperCase();
}
