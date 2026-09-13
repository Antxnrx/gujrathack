import { Suspense, lazy, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useAlertSummary, useEventSummary, useHealth, useLiveSync } from "./lib/queries";
import { watchState, type LiveState } from "./lib/live";
import { Clock, Skeleton } from "./components/ui";
import { setToken } from "./lib/api";
import Login, { consumeTokenFromUrl } from "./routes/Login";

/**
 * Route-level code splitting.
 *
 * Every route below is lazy, which is what keeps the first paint to the shell plus the
 * dashboard. The two heavyweights are the reason: the camera view pulls hls.js and the
 * map pulls leaflet, roughly 150 kB between them, and an operator who spends the shift
 * on the alert queue should never download either.
 */
const Dashboard   = lazy(() => import("./routes/Dashboard"));
const Playbooks   = lazy(() => import("./routes/Playbooks"));
const Timeline    = lazy(() => import("./routes/Timeline"));
const Cameras     = lazy(() => import("./routes/Cameras"));
const CameraView  = lazy(() => import("./routes/CameraView"));
const SearchView  = lazy(() => import("./routes/SearchView"));
const Alerts      = lazy(() => import("./routes/Alerts"));
const Incidents   = lazy(() => import("./routes/Incidents"));
const Zones       = lazy(() => import("./routes/Zones"));
const Investigate = lazy(() => import("./routes/Investigate"));
const Reconstruct = lazy(() => import("./routes/Reconstruct"));
const Evidence    = lazy(() => import("./routes/Evidence"));
const Saved       = lazy(() => import("./routes/Saved"));

function LiveDot() {
  const [s, setS] = useState<LiveState>("idle");
  useEffect(() => watchState(setS), []);
  const text = { open: "live", connecting: "connecting", error: "offline", idle: "idle" }[s];
  return (
    <span className="row tiny dim" title={`Event bus: ${s}`}>
      <i className={`live-dot ${s}`} /> {text}
    </span>
  );
}

function Nav() {
  const events = useEventSummary({});
  const alerts = useAlertSummary();
  const open = events.data?.totals?.open ?? 0;
  const pending = alerts.data?.pending ?? alerts.data?.by_status?.pending ?? 0;

  const link = (to: string, label: string, badge?: number) => (
    <NavLink to={to} className={({ isActive }) => `nav-link ${isActive ? "on" : ""}`} end={to === "/"}>
      <span className="lbl">{label}</span>
      {badge ? <span className={`badge ${badge > 0 && to === "/alerts" ? "hot" : ""}`}>{badge}</span> : null}
    </NavLink>
  );

  return (
    <nav className="side">
      <div className="brand"><b>Setu</b><span>control room</span></div>
      {link("/", "Dashboard")}
      {link("/playbooks", "Do a job")}
      {link("/timeline", "Timeline", open)}
      {link("/alerts", "Alerts", pending)}
      <div className="nav-group">Watch</div>
      {link("/cameras", "Cameras")}
      {link("/zones", "Zones & rules")}
      <div className="nav-group">Investigate</div>
      {link("/search", "Search")}
      {link("/investigate", "Next camera")}
      {link("/reconstruct", "Reconstruction")}
      <div className="nav-group">Case work</div>
      {link("/incidents", "Incidents")}
      {link("/evidence", "Evidence")}
      {link("/saved", "Saved")}
      <RegistryLink />
    </nav>
  );
}

/**
 * Out to the registry.
 *
 * The estate this console reasons over is owned by Model 1 — camera identity,
 * department, location and stream address all come from there, and anything an
 * operator wants to *change* about a camera is changed there too. Leaving them
 * to find that themselves would be the wrong kind of separation: two systems on
 * one account should feel like two halves, not two products.
 *
 * The URL is whatever the API reports it is talking to, so this follows the
 * deployment rather than a build-time constant.
 */
function RegistryLink() {
  const health = useHealth();
  const base = health.data?.checks?.model1?.portal_url;
  if (!base) return null;
  return (
    <>
      <div className="nav-group">Estate</div>
      <a className="nav-link" href={base} target="_blank" rel="noreferrer"
         title="Camera registry — onboarding, coverage, health and GIS">
        <span className="lbl">Registry ↗</span>
      </a>
    </>
  );
}

export default function App() {
  useLiveSync();
  const health = useHealth();
  const d = health.data?.checks?.data;
  const m1 = health.data?.checks?.model1;

  // Sign-in state lives here rather than in a context: exactly one thing
  // depends on it — whether the shell renders at all.
  const [signedIn, setSignedIn] = useState<boolean>(() => {
    if (consumeTokenFromUrl()) return true;              // arrived from the portal
    return Boolean(localStorage.getItem("setu.token"));
  });

  // The API drops the token on a 401 and announces it here. Without this the
  // console stays on a shell whose every panel is empty, which looks like the
  // system being broken rather than the session having ended.
  useEffect(() => {
    const onExpired = () => setSignedIn(false);
    window.addEventListener("setu:unauthorized", onExpired);
    return () => window.removeEventListener("setu:unauthorized", onExpired);
  }, []);

  // `requires_auth` is reported by the API rather than guessed from a build
  // flag, so the console follows the deployment it is actually talking to. The
  // offline demo runs auth-optional and never shows this screen.
  if (m1?.requires_auth && !signedIn) {
    return (
      <Suspense fallback={<Skeleton rows={4} />}>
        <Login
          model1={m1.base_url || "http://localhost:8000"}
          onSignedIn={() => setSignedIn(true)}
        />
      </Suspense>
    );
  }

  return (
    <div className="shell">
      <Nav />
      <div className="main">
        <header className="topbar">
          <LiveDot />
          <span className="grow" />
          {d && (
            <span className="tiny faint mono">
              {d.cameras} cameras · {d.detections?.toLocaleString("en-IN")} detections · {d.tracks?.toLocaleString("en-IN")} tracks
            </span>
          )}
          {health.data?.checks?.minio?.bucket_exists === false && (
            <span className="pill warn" title="No thumbnail bucket, so crops render as placeholders">no imagery</span>
          )}
          {health.data?.checks?.model1?.configured === false && (
            <span className="pill" title="MODEL1_BASE_URL is unset — the API runs in its documented offline mode">
              model 1 offline
            </span>
          )}
          {signedIn && m1?.requires_auth && (
            <button
              className="btn tiny"
              title="Clear this token. The registry account is unaffected."
              onClick={() => { setToken(null); setSignedIn(false); }}
            >
              Sign out
            </button>
          )}
          <Clock />
        </header>
        <Suspense fallback={<Skeleton rows={8} />}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/playbooks" element={<Playbooks />} />
            <Route path="/timeline" element={<Timeline />} />
            <Route path="/timeline/:eventId" element={<Timeline />} />
            <Route path="/alerts" element={<Alerts />} />
            <Route path="/cameras" element={<Cameras />} />
            <Route path="/cameras/:ref" element={<CameraView />} />
            <Route path="/zones" element={<Zones />} />
            <Route path="/search" element={<SearchView />} />
            <Route path="/investigate" element={<Investigate />} />
            <Route path="/reconstruct" element={<Reconstruct />} />
            <Route path="/reconstruct/:pursuitId" element={<Reconstruct />} />
            <Route path="/incidents" element={<Incidents />} />
            <Route path="/incidents/:incidentId" element={<Incidents />} />
            <Route path="/evidence" element={<Evidence />} />
            <Route path="/saved" element={<Saved />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </div>
    </div>
  );
}
