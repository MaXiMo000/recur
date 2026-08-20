import { Fragment, useEffect, useState } from "react";

import { api, ApiError } from "./api";
import { AuthScreen, TokenScreen } from "./Auth";
import { AccountPanel, ReviewQueue, Upload } from "./Upload";

const money = (c) => (c / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
const day = (d) => new Date(d + "T00:00:00").toLocaleDateString("en-US",
                                                               { month: "short", day: "numeric" });

/* Every fetch is a real request against the signed-in account. There is no
   fallback to sample data: showing a real user invented numbers when a request
   fails would be worse than showing them an error. */
function useApi(fn, deps = [], reloadKey = 0) {
  const [state, set] = useState({ loading: true });
  useEffect(() => {
    let live = true;
    fn()
      .then((data) => live && set({ data }))
      .catch((error) => live && set({ error }));
    return () => { live = false; };
  }, [...deps, reloadKey]);
  return state;
}

/* One series, so no legend -- the heading names it. Prices hold flat and then
   jump, so the line is stepped: interpolating between them would draw a gradual
   rise that never happened. */
function PriceHistory({ points, title }) {
  const W = 560, H = 150, P = { t: 14, r: 16, b: 22, l: 52 };
  const [hover, setHover] = useState(null);
  if (!points?.length) return null;

  const xs = points.map((p) => new Date(p.posted_date + "T00:00:00").getTime());
  const ys = points.map((p) => p.amount_cents);
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)];
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const pad = (hi - lo) * 0.25 || Math.max(hi * 0.1, 100);
  const [y0, y1] = [Math.max(0, lo - pad), hi + pad];

  const px = (t) => P.l + ((t - x0) / (x1 - x0 || 1)) * (W - P.l - P.r);
  const py = (v) => P.t + (1 - (v - y0) / (y1 - y0 || 1)) * (H - P.t - P.b);

  let d = "";
  points.forEach((p, i) => {
    const X = px(xs[i]), Y = py(ys[i]);
    d += i === 0 ? `M${X},${Y}` : `H${X}V${Y}`;
  });

  const ticks = [y0, (y0 + y1) / 2, y1];
  const h = hover !== null ? points[hover] : null;

  return (
    <figure style={{ margin: "10px 0 0" }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label={`${title}: ${points.length} charges from ${points[0].posted_date} to ${points.at(-1).posted_date}`}
           onMouseLeave={() => setHover(null)}>
        {ticks.map((v, i) => (
          <g key={i}>
            <line x1={P.l} x2={W - P.r} y1={py(v)} y2={py(v)} stroke="var(--grid)" strokeWidth="1" />
            <text x={P.l - 8} y={py(v) + 4} textAnchor="end" fontSize="10" fill="var(--text-muted)">
              {money(v)}
            </text>
          </g>
        ))}
        <path d={d} fill="none" stroke="var(--series-1)" strokeWidth="2"
              strokeLinejoin="round" strokeLinecap="round" />
        {points.map((p, i) => (
          <circle key={i} cx={px(xs[i])} cy={py(ys[i])} r={hover === i ? 5 : 3.5}
                  fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth="2" />
        ))}
        {/* Hit targets far wider than the marks. */}
        {points.map((p, i) => (
          <rect key={`h${i}`} x={px(xs[i]) - 14} y={0} width={28} height={H}
                fill="transparent" onMouseEnter={() => setHover(i)} />
        ))}
        <text x={P.l} y={H - 6} fontSize="10" fill="var(--text-muted)">{day(points[0].posted_date)}</text>
        <text x={W - P.r} y={H - 6} fontSize="10" fill="var(--text-muted)" textAnchor="end">
          {day(points.at(-1).posted_date)}
        </text>
      </svg>
      <figcaption className="muted" style={{ fontSize: 12, minHeight: 18 }}>
        {h ? `${day(h.posted_date)} — ${money(h.amount_cents)}`
           : `${points.length} charges · hover a point`}
      </figcaption>
    </figure>
  );
}

function Subscriptions({ rows }) {
  const [openId, setOpenId] = useState(null);
  const history = useApi(() => (openId ? api.history(openId) : Promise.resolve(null)),
                         [openId]);
  const max = Math.max(...rows.map((r) => r.annual_cents), 1);

  return (
    <table>
      <thead>
        <tr>
          <th>Merchant</th><th>Cadence</th>
          <th className="num">Amount</th><th className="num">Per year</th>
          <th className="bar-cell"></th><th className="num">Conf.</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <Fragment key={r.id}>
            <tr aria-selected={openId === r.id}
                onClick={() => setOpenId(openId === r.id ? null : r.id)}>
              <td>
                {r.merchant}
                {r.usage_based && <span className="pill" style={{ marginLeft: 8 }}>usage-based</span>}
                {r.status !== "active" && <span className="pill" style={{ marginLeft: 8 }}>{r.status}</span>}
              </td>
              <td className="muted">{r.cadence}</td>
              <td className="num">{money(r.current_amount_cents)}</td>
              <td className="num">{money(r.annual_cents)}</td>
              <td className="bar-cell">
                <div className="bar-track">
                  <div className="bar-fill" style={{ width: `${(r.annual_cents / max) * 100}%` }} />
                </div>
              </td>
              <td className="num muted">{Number(r.confidence).toFixed(2)}</td>
            </tr>
            {openId === r.id && (
              <tr>
                <td colSpan={6} style={{ paddingBottom: 18 }}>
                  {history.data ? <PriceHistory points={history.data} title={r.merchant} />
                                : <span className="muted">loading…</span>}
                </td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </table>
  );
}

function tokenFromUrl() {
  const path = window.location.pathname;
  const token = new URLSearchParams(window.location.search).get("token");
  if (!token) return null;
  if (path.startsWith("/verify")) return { kind: "verify", token };
  if (path.startsWith("/reset")) return { kind: "reset", token };
  return null;
}

function Dashboard({ me, onSignedOut }) {
  const [reload, setReload] = useState(0);
  const bump = () => setReload((n) => n + 1);

  const summary = useApi(() => api.summary(), [], reload);
  const subs = useApi(() => api.subscriptions(), [], reload);
  const upcoming = useApi(() => api.upcoming(30), [], reload);
  const increases = useApi(() => api.increases(), [], reload);
  const queue = useApi(() => api.reviewQueue(), [], reload);

  const s = summary.data;
  const hasData = s && (subs.data?.length || queue.data?.length);

  return (
    <div className="wrap">
      <header>
        <h1>Recur</h1>
        <p>
          What you&rsquo;re actually paying for
          {s?.as_of && <> · statement through {s.as_of}</>}
        </p>
      </header>

      <AccountPanel me={me} onSignedOut={onSignedOut} />

      <div style={{ marginTop: 16 }}>
        <Upload onLoaded={bump} />
      </div>

      {!hasData && !summary.loading && (
        <p className="muted" style={{ marginTop: 24 }}>
          Nothing loaded yet. Upload a statement above to see what you&rsquo;re paying for.
        </p>
      )}

      {s && subs.data?.length > 0 && (
        <>
          <div className="tiles" style={{ marginTop: 28 }}>
            <div className="card">
              <div className="tile-label">Recurring spend</div>
              <div className="tile-value hero">{money(s.annual_cents)}</div>
              <div className="tile-note">per year · {money(s.monthly_cents)}/mo</div>
            </div>
            <div className="card">
              <div className="tile-label">Active subscriptions</div>
              <div className="tile-value">{s.active_count}</div>
              <div className="tile-note">
                {s.inactive_count ? `${s.inactive_count} lapsed or cancelled` : "none lapsed"}
              </div>
            </div>
            <div className="card">
              <div className="tile-label">Price rises</div>
              <div className="tile-value" style={{
                color: s.price_increase_annual_cents > 0 ? "var(--status-critical)" : undefined }}>
                {s.price_increase_annual_cents > 0 ? "+" : ""}{money(s.price_increase_annual_cents)}
              </div>
              <div className="tile-note">added per year</div>
            </div>
          </div>

          <h2>Subscriptions <span>· click a row for its price history</span></h2>
          <div className="card"><Subscriptions rows={subs.data} /></div>

          <h2>Next 30 days</h2>
          <div className="card">
            <div className="rows">
              {upcoming.data?.length ? (
                <>
                  {upcoming.data.map((r, i) => (
                    <div className="row" key={i}>
                      <span className="date">{day(r.next_due)}</span>
                      <span className="grow">{r.merchant}</span>
                      <span>{money(r.current_amount_cents)}</span>
                    </div>
                  ))}
                  <div className="row" style={{ fontWeight: 650 }}>
                    <span className="date"></span>
                    <span className="grow">Total</span>
                    <span>{money(upcoming.data.reduce((a, r) => a + r.current_amount_cents, 0))}</span>
                  </div>
                </>
              ) : <div className="empty">Nothing due in the next 30 days.</div>}
            </div>
          </div>

          <h2>Price changes</h2>
          <div className="card">
            <div className="rows">
              {increases.data?.length ? increases.data.map((r, i) => {
                const up = r.new_amount_cents > r.old_amount_cents;
                return (
                  <div className="row" key={i}>
                    <span className="date">{day(r.effective_date)}</span>
                    <span className="grow">{r.merchant}</span>
                    <span className="muted">
                      {money(r.old_amount_cents)} → {money(r.new_amount_cents)}
                    </span>
                    <span className={up ? "up" : "down"}
                          style={{ minWidth: 118, textAlign: "right" }}>
                      {up ? "▲" : "▼"} {up ? "+" : ""}{Number(r.pct_change).toFixed(1)}% ·{" "}
                      {money(r.annual_impact_cents)}/yr
                    </span>
                  </div>
                );
              }) : <div className="empty">No price changes detected.</div>}
            </div>
          </div>
        </>
      )}

      <ReviewQueue items={queue.data} onResolved={bump} />
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState(undefined);   // undefined = still checking
  const [link] = useState(tokenFromUrl);

  const load = () => api.me().then(setMe).catch(() => setMe(null));
  useEffect(() => { load(); }, []);

  if (link) {
    return <TokenScreen kind={link.kind} token={link.token}
                        onDone={() => { window.location.href = "/"; }} />;
  }
  if (me === undefined) return <div className="wrap muted">loading…</div>;
  if (me === null) return <AuthScreen onSignedIn={load} />;
  return <Dashboard me={me} onSignedOut={() => setMe(null)} />;
}
