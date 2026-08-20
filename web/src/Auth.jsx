import { useState } from "react";
import { api, ApiError } from "./api";

/* Sign-in, registration, and the two flows that arrive by emailed link.
 *
 * Deliberate: registration and forgot-password both report success without
 * saying whether the address exists. The API answers identically either way,
 * and a screen that said "that email is already registered" would hand back
 * the enumeration oracle the API just refused to give. */

function Field({ label, hint, ...props }) {
  return (
    <label className="field">
      <span>{label}</span>
      <input {...props} />
      {hint && <small className="muted">{hint}</small>}
    </label>
  );
}

export function AuthScreen({ onSignedIn }) {
  const [mode, setMode] = useState("login"); // login | register | forgot
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [sent, setSent] = useState(null);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") {
        await api.login(email, password);
        onSignedIn();
      } else if (mode === "register") {
        await api.register(email, password);
        setSent("If that address can receive mail, a confirmation link is on its way.");
      } else {
        await api.forgot(email);
        setSent("If that address has an account, a reset link is on its way.");
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <div className="auth card">
        <h1>Check your email</h1>
        <p className="muted">{sent}</p>
        <button className="link" onClick={() => { setSent(null); setMode("login"); }}>
          Back to sign in
        </button>
      </div>
    );
  }

  return (
    <div className="auth card">
      <h1>Recur</h1>
      <p className="muted">
        Find out what you're actually paying for. Upload a CSV your bank gave
        you &mdash; no bank login, ever.
      </p>

      <form onSubmit={submit}>
        <Field label="Email" type="email" value={email} required autoComplete="email"
               onChange={(e) => setEmail(e.target.value)} />
        {mode !== "forgot" && (
          <Field
            label="Password" type="password" value={password} required minLength={12}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            hint={mode === "register" ? "At least 12 characters." : undefined}
            onChange={(e) => setPassword(e.target.value)} />
        )}
        {error && <p className="err" role="alert">{error}</p>}
        <button type="submit" disabled={busy} className="primary">
          {busy ? "Working…"
            : mode === "login" ? "Sign in"
            : mode === "register" ? "Create account"
            : "Send reset link"}
        </button>
      </form>

      <div className="auth-links">
        {mode !== "login" && (
          <button className="link" onClick={() => setMode("login")}>Sign in</button>
        )}
        {mode !== "register" && (
          <button className="link" onClick={() => setMode("register")}>Create an account</button>
        )}
        {mode !== "forgot" && (
          <button className="link" onClick={() => setMode("forgot")}>Forgot password</button>
        )}
      </div>
    </div>
  );
}

/* Both emailed links land here. The token comes from the query string, so it
 * is stripped from the URL after use -- a link in browser history or a pasted
 * address bar should not stay usable. */
export function TokenScreen({ kind, token, onDone }) {
  const [password, setPassword] = useState("");
  const [state, setState] = useState("idle");
  const [error, setError] = useState(null);

  async function run(e) {
    e?.preventDefault();
    setState("busy");
    setError(null);
    try {
      if (kind === "verify") await api.verify(token);
      else await api.reset(token, password);
      window.history.replaceState({}, "", "/");
      setState("done");
    } catch (err) {
      setError(err.message);
      setState("idle");
    }
  }

  if (state === "done") {
    return (
      <div className="auth card">
        <h1>{kind === "verify" ? "Email confirmed" : "Password updated"}</h1>
        <p className="muted">
          {kind === "verify"
            ? "You can sign in now."
            : "Every other session has been signed out."}
        </p>
        <button className="primary" onClick={onDone}>Sign in</button>
      </div>
    );
  }

  return (
    <div className="auth card">
      <h1>{kind === "verify" ? "Confirm your email" : "Set a new password"}</h1>
      <form onSubmit={run}>
        {kind === "reset" && (
          <Field label="New password" type="password" value={password} required
                 minLength={12} autoComplete="new-password" hint="At least 12 characters."
                 onChange={(e) => setPassword(e.target.value)} />
        )}
        {error && <p className="err" role="alert">{error}</p>}
        <button className="primary" type="submit" disabled={state === "busy"}>
          {state === "busy" ? "Working…" : kind === "verify" ? "Confirm" : "Update password"}
        </button>
      </form>
    </div>
  );
}
