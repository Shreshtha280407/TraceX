import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  GoogleAuthProvider,
  createUserWithEmailAndPassword,
  onIdTokenChanged,
  signInWithEmailAndPassword,
  signInWithPopup,
  signOut,
} from "firebase/auth";
import { Navigate } from "react-router-dom";
import { firebaseAuth } from "./firebase";
import { setAuthToken } from "./api";

type AuthContextValue = {
  token: string | null;
  actor: string | null;
  signup: (email: string, password: string) => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  loginWithGoogle: () => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  // `ready` gates rendering `children` until Firebase has resolved whatever
  // session it may have restored from its own persistence. Without this gate,
  // a protected page's own data-fetching effect can fire before the ID token
  // is known, the same race the previous localStorage-backed auth avoided by
  // reading a token synchronously at module load -- there is no synchronous
  // equivalent once the source of truth is Firebase's own async session.
  const [state, setState] = useState<{ token: string | null; actor: string | null; ready: boolean }>({
    token: null,
    actor: null,
    ready: false,
  });

  useEffect(() => {
    // `onIdTokenChanged` (not `onAuthStateChanged`) fires on sign-in, sign-out,
    // *and* the SDK's own hourly token refresh -- the only way `setAuthToken`
    // stays correct for the lifetime of a session, not just at sign-in.
    return onIdTokenChanged(firebaseAuth, async (user) => {
      if (!user) {
        setAuthToken(null);
        setState({ token: null, actor: null, ready: true });
        return;
      }
      const token = await user.getIdToken();
      setAuthToken(token);
      setState({ token, actor: user.email ?? user.displayName ?? user.uid, ready: true });
    });
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      token: state.token,
      actor: state.actor,
      // No manual state update after these calls: a successful sign-in/sign-up
      // fires `onIdTokenChanged` above, which is the single place state changes.
      signup: async (email, password) => {
        await createUserWithEmailAndPassword(firebaseAuth, email, password);
      },
      login: async (email, password) => {
        await signInWithEmailAndPassword(firebaseAuth, email, password);
      },
      loginWithGoogle: async () => {
        await signInWithPopup(firebaseAuth, new GoogleAuthProvider());
      },
      logout: () => signOut(firebaseAuth),
    }),
    [state]
  );

  if (!state.ready) {
    return (
      <div style={{ display: "grid", placeItems: "center", minHeight: "100vh" }} aria-busy="true">
        Loading…
      </div>
    );
  }

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

/** Firebase's own error shape (`{ code, message }`) surfaced as readable copy. */
export function authErrorMessage(err: unknown): string {
  const code = (err as { code?: string } | undefined)?.code;
  switch (code) {
    case "auth/email-already-in-use":
      return "An account with that email already exists. Try signing in instead.";
    case "auth/invalid-credential":
    case "auth/wrong-password":
    case "auth/user-not-found":
      return "Incorrect email or password.";
    case "auth/weak-password":
      return "Password must be at least 8 characters.";
    case "auth/invalid-email":
      return "Enter a valid email address.";
    case "auth/popup-closed-by-user":
      return "Google sign-in was cancelled.";
    case "auth/network-request-failed":
      return "Could not reach Firebase. Check your connection.";
    default:
      return "Could not sign in. Please try again.";
  }
}
