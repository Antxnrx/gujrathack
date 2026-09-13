/**
 * Sign in — against Model 1, not against this service.
 *
 * The registry is the identity provider for the whole platform: it holds the
 * operator accounts, issues the RS256 tokens, and publishes the JWKS this API
 * verifies them against. So the credentials go straight from the browser to
 * Model 1 and the token comes straight back. The analytics layer never sees a
 * password, which is the property that lets one account open both consoles
 * without either trusting the other with more than a public key.
 *
 * Two ways in:
 *   - this form, for an operator arriving at the console directly;
 *   - a `#token=` fragment, for one arriving from the Model 1 portal already
 *     signed in. A fragment rather than a query string because fragments are
 *     not sent to servers and stay out of access logs and Referer headers.
 */
import { useEffect, useState } from "react";
import { setToken } from "../lib/api";

type Props = {
  /** Where Model 1 lives, from `/api/health`. */
  model1: string;
  /** Called once a token has been stored, so the shell can re-render. */
  onSignedIn: () => void;
};

/** Pull a token handed over by the Model 1 portal, and scrub it from the URL. */
export function consumeTokenFromUrl(): string | null {
  const raw = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : "";
  const token = new URLSearchParams(raw).get("token");
  if (!token) return null;
  setToken(token);
  // Leaving it in the address bar means it survives a copied link and a
  // screenshot, neither of which should carry a credential.
  history.replaceState(null, "", window.location.pathname + window.location.search);
  return token;
}

export default function Login({ model1, onSignedIn }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (consumeTokenFromUrl()) onSignedIn();
  }, [onSignedIn]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${model1}/api/v1/auth/login`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        setError(
          res.status === 401
            ? "That email and password were not accepted by the registry."
            : `The registry answered ${res.status}. It may be restarting.`,
        );
        return;
      }
      const body = await res.json();
      if (!body.access_token) {
        setError("The registry signed in but returned no token.");
        return;
      }
      setToken(body.access_token, body.refresh_token ?? null, model1);
      onSignedIn();
    } catch {
      setError(`Could not reach the registry at ${model1}.`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div className="login-head">
          <h1>Setu</h1>
          <p className="tiny faint">
            Control room · Gujarat Police Innovation Challenge
          </p>
        </div>

        <p className="tiny dim login-note">
          Sign in with your <strong>Sentinel Registry</strong> account. The same
          credentials open the registry portal — this console verifies the token
          against the registry&rsquo;s published keys and never sees your password.
        </p>

        <label className="login-field">
          <span className="tiny faint">Email</span>
          <input
            id="login-email"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </label>

        <label className="login-field">
          <span className="tiny faint">Password</span>
          <input
            id="login-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>

        {error && <div className="login-error">{error}</div>}

        <button className="btn primary" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="tiny faint login-foot mono">{model1}</p>
      </form>
    </div>
  );
}
