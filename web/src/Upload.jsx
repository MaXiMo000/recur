import { useState } from "react";
import { api } from "./api";

/* CSV upload, plus the review queue.
 *
 * The queue is here rather than in a CLI because in production there is no CLI.
 * Leaving it unanswerable would leave every total on the dashboard permanently
 * understated, with nothing on screen explaining why. */

const money = (c) => (c / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });

export function Upload({ onLoaded }) {
  const [file, setFile] = useState(null);
  const [account, setAccount] = useState("card");
  const [dayfirst, setDayfirst] = useState(false);
  const [currency, setCurrency] = useState("USD");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  async function submit(e) {
    e.preventDefault();
    if (!file) return;
    setBusy(true); setError(null); setResult(null);
    try {
      const r = await api.upload(file, { account, dayfirst, currency });
      setResult(r);
      onLoaded?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>Add a statement</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        Export a CSV from your bank and drop it here. Nothing is stored except
        the transactions themselves, and re-uploading the same file changes
        nothing.
      </p>

      <form onSubmit={submit} className="upload-form">
        <label className="field">
          <span>CSV file</span>
          <input type="file" accept=".csv,.tsv,.txt" required
                 onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
        <label className="field">
          <span>Account name</span>
          <input value={account} maxLength={64} onChange={(e) => setAccount(e.target.value)} />
        </label>
        <label className="field">
          <span>Currency</span>
          <input value={currency} maxLength={3} style={{ width: 90 }}
                 onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
        </label>
        <label className="check">
          <input type="checkbox" checked={dayfirst}
                 onChange={(e) => setDayfirst(e.target.checked)} />
          {/* No auto-detection: 08/03 is ambiguous and guessing wrong is silent. */}
          <span>Dates are day/month (outside the US)</span>
        </label>
        <button className="primary" type="submit" disabled={busy || !file}>
          {busy ? "Reading…" : "Upload"}
        </button>
      </form>

      {error && <p className="err" role="alert">{error}</p>}

      {result && (
        <div className="result">
          <p>
            <strong>{result.rows_read.toLocaleString()}</strong> rows read,{" "}
            <strong>{result.rows_new.toLocaleString()}</strong> new
            {result.rows_duplicate > 0 &&
              <> · {result.rows_duplicate.toLocaleString()} already present</>}
            {result.sign_flipped &&
              <> · treated positive amounts as charges</>}
          </p>
          <p>
            Found <strong>{result.subscriptions_found}</strong> recurring charges.
            {result.awaiting_review > 0 &&
              <> {result.awaiting_review} descriptor{result.awaiting_review === 1 ? "" : "s"} need
                 your call below.</>}
          </p>
        </div>
      )}
    </div>
  );
}

export function ReviewQueue({ items, onResolved }) {
  const [busyId, setBusyId] = useState(null);
  const [custom, setCustom] = useState({});

  if (!items?.length) return null;

  async function answer(item, merchant, ignore = false) {
    setBusyId(item.id);
    try {
      await api.resolveQueueItem(item.id, merchant, ignore);
      onResolved?.();
    } finally {
      setBusyId(null);
    }
  }

  return (
    <>
      <h2>
        Needs your call <span>· {items.length} descriptor{items.length === 1 ? "" : "s"}</span>
      </h2>
      <div className="card">
        {/* Stated plainly, because a total that is wrong for a reason should
            say so rather than look merely small. */}
        <p className="warn">
          Until these are answered, the figures above are understated &mdash; these
          charges aren&rsquo;t counted against any merchant yet.
        </p>
        <div className="rows">
          {items.map((item) => (
            <div className="queue-item" key={item.id}>
              <div className="queue-head">
                <code>{item.scrubbed}</code>
                <span className="muted">
                  {item.txn_count} charge{item.txn_count === 1 ? "" : "s"} · {item.reason}
                </span>
              </div>
              <div className="queue-actions">
                {(item.candidates || []).slice(0, 3).map((c) => (
                  <button key={c.name} disabled={busyId === item.id}
                          onClick={() => answer(item, c.name)}>
                    Same as <strong>{c.name}</strong>
                    <span className="muted"> ({Math.round(c.score)})</span>
                  </button>
                ))}
                <input placeholder="or name it yourself" maxLength={200}
                       value={custom[item.id] ?? ""}
                       onChange={(e) => setCustom({ ...custom, [item.id]: e.target.value })} />
                <button disabled={busyId === item.id || !custom[item.id]}
                        onClick={() => answer(item, custom[item.id])}>
                  Use this name
                </button>
                <button className="link" disabled={busyId === item.id}
                        onClick={() => answer(item, null, true)}>
                  Not a subscription
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

export function AccountPanel({ me, onSignedOut }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  async function download() {
    const data = await api.exportAll();
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "recur-export.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="card account-panel">
      <div>
        <div className="muted" style={{ fontSize: 12 }}>Signed in as</div>
        <div>{me.email}</div>
      </div>
      <div className="account-actions">
        <button onClick={download}>Download my data</button>
        <button onClick={async () => { await api.logout(); onSignedOut(); }}>Sign out</button>
        {confirming ? (
          <>
            <span className="err">Delete everything permanently?</span>
            <button className="danger" disabled={busy} onClick={async () => {
              setBusy(true);
              await api.deleteAccount();
              onSignedOut();
            }}>Yes, delete</button>
            <button className="link" onClick={() => setConfirming(false)}>Cancel</button>
          </>
        ) : (
          <button className="link danger" onClick={() => setConfirming(true)}>
            Delete my account
          </button>
        )}
      </div>
    </div>
  );
}

export { money };
