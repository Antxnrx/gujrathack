import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

/**
 * Confirmations and failures, bottom-right, self-dismissing.
 *
 * Mutations in this console are one click on a dense list — triage a row, tag it, star
 * it — and the row itself often does not visibly change. Without an acknowledgement the
 * operator clicks again, which for `POST /incidents` means a second incident. So every
 * mutation reports, and failures stay twice as long as successes.
 */

type Toast = { id: number; kind: "ok" | "err" | "info"; text: string };
type Push = (kind: Toast["kind"], text: string) => void;

const Ctx = createContext<Push>(() => {});

export const useToast = () => useContext(Ctx);

/** `useToast()` wrapped for the common case: report whatever a mutation rejected with. */
export function useReport() {
  const push = useToast();
  return useMemo(
    () => ({
      ok: (text: string) => push("ok", text),
      err: (e: unknown, what?: string) =>
        push("err", `${what ? what + ": " : ""}${(e as Error)?.message ?? String(e)}`),
    }),
    [push],
  );
}

export function ToastHost({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);

  const push = useCallback<Push>((kind, text) => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs.slice(-4), { id, kind, text }]);
    window.setTimeout(() => setItems((xs) => xs.filter((t) => t.id !== id)), kind === "err" ? 8000 : 3500);
  }, []);

  return (
    <Ctx.Provider value={push}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`}>{t.text}</div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
