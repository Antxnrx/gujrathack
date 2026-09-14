import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useCamera, useCameraTimeline, useCameras, useLiveDetections, useTracks,
} from "../lib/queries";
import { ago, dur, hhmmss, num, pct, stamp, titleCase } from "../lib/format";
import {
  Appearance, Bar, Empty, ErrorBox, Help, Pill, Plate, SevDot, Skeleton, Status, Thumb,
} from "../components/ui";
import { Player } from "../components/Player";

/**
 * One camera, laid out the way a VMS is: the picture first and large, then the
 * facts about it, then the plates it has read — all in the first screen, with
 * the indexed detail below.
 *
 * The stage (bar · player · live strip · plate strip) is sized to the viewport,
 * so opening a camera puts you in front of the image rather than in front of a
 * form about the image. Everything an operator glances at while watching is on
 * that screen; everything they'd sit down to read scrolls under it.
 *
 * The camera bar is sticky. Scrolling into the detail must never leave you
 * unsure which camera you are reading about, and switching camera has to be
 * reachable from anywhere on the page.
 *
 * Keyboard, because a control room is not driven by a mouse:
 *   ← →  previous / next camera        f  fullscreen
 *   space  play / pause                esc  back to the index
 * Handlers are skipped while a field has focus, so typing in a search box does
 * not change camera.
 */
export default function CameraView() {
  const { ref = "" } = useParams();
  const nav = useNavigate();
  const q = useCamera(ref);
  const all = useCameras();
  const [bucket, setBucket] = useState(15);
  const [hours, setHours] = useState(6);
  const tl = useCameraTimeline(ref, { bucket_minutes: bucket, hours });
  const tracks = useTracks({ camera: ref, limit: 24, has_plate: true });
  const stageRef = useRef<HTMLDivElement>(null);

  // Off by default, because this gateway serves a recorded archive rather than a
  // live edge and the two playheads are independent.
  //
  // The worker walks the segment list from the start of the archive; the browser
  // plays from wherever it seeked to — 13:36:20 of 24:28:58, in the case that
  // caught this. Both are real, both are the same camera, and they are almost
  // never the same moment, so a box drawn from the worker's position lands on a
  // frame the browser is showing from somewhere else entirely. It reads as
  // latency or as a tracker that cannot follow a car; it is neither.
  //
  // Correctly synchronising this needs each detection to carry its absolute
  // offset into the archive so the overlay can be driven from the element's
  // currentTime instead of wall-clock. Until that exists, the overlay is opt-in
  // and labelled for what it is: what the detector is finding on this camera
  // right now, not an annotation of the frame on screen.
  const [boxes, setBoxes] = useState(false);
  const live = useLiveDetections(ref, boxes);
  const dets = live.data?.items ?? [];

  // "On screen" is the same four-second window the overlay fades over, so the
  // count and the picture never disagree. "Detector running" is a looser
  // question — a camera can go quiet for a few seconds on an empty road without
  // the worker having stopped — so it is asked over a longer window.
  const seen = useMemo(() => {
    const now = Date.now();
    const age = (d: { ts: string }) => (now - new Date(d.ts).getTime()) / 1000;
    return {
      onScreen: dets.filter((d) => age(d) <= 4).length,
      plates: dets.filter((d) => d.plate_text && age(d) <= 60).length,
      running: dets.some((d) => age(d) <= 20),
    };
  }, [dets]);

  const cam = q.data?.camera;
  const act = q.data?.activity;
  const zones = useMemo(() => q.data?.zones ?? [], [q.data]);
  const neighbours = q.data?.neighbours ?? [];
  const recent = q.data?.recent_events ?? [];

  // Ordered estate, so ← → step through it in the order the index shows.
  const order = useMemo(
    () => [...(all.data?.items ?? [])].sort((a, b) => a.external_id.localeCompare(b.external_id)),
    [all.data],
  );
  const at = order.findIndex((c) => c.external_id === ref);
  const step = useCallback((by: number) => {
    if (order.length === 0) return;
    const i = at < 0 ? 0 : (at + by + order.length) % order.length;
    nav(`/cameras/${order[i].external_id}`);
  }, [order, at, nav]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Never steal a key from a field, a select or anything the operator is
      // editing — a search box that changes camera on every arrow press is
      // worse than no shortcuts at all.
      const t = e.target as HTMLElement | null;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
      else if (e.key === "Escape") { nav("/cameras"); }
      else if (e.key.toLowerCase() === "b") { e.preventDefault(); setBoxes((b) => !b); }
      else if (e.key.toLowerCase() === "f") {
        e.preventDefault();
        const el = stageRef.current?.querySelector("video");
        if (document.fullscreenElement) document.exitFullscreen();
        else el?.requestFullscreen?.().catch(() => {});
      } else if (e.key === " ") {
        const v = stageRef.current?.querySelector("video");
        if (v) { e.preventDefault(); v.paused ? v.play().catch(() => {}) : v.pause(); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, nav]);

  if (q.isError) return <div className="view"><ErrorBox error={q.error} what={`Camera ${ref}`} /></div>;
  if (q.isLoading || !cam) return <div className="view"><Skeleton rows={10} /></div>;

  const stream = (cam.metadata?.streams as any[] | undefined)?.[0];
  const anpr = cam.metadata?.anpr as
    | { tier?: string; read_rate?: number; median_vehicle_width_px?: number } | undefined;
  const plated = tracks.data?.items ?? [];

  return (
    <div className="cam-page">
      {/* ── The stage: one screen, picture first ────────────────────────── */}
      <div className="cam-stage" ref={stageRef}>
        <header className="cam-bar">
          <button className="ghost sm" onClick={() => step(-1)} title="Previous camera (←)"
                  aria-label="Previous camera">◀</button>
          <span className="mono cam-id">{cam.external_id}</span>
          <b className="trunc">{cam.name}</b>
          <span className="tiny faint trunc">
            {[cam.area, cam.district].filter(Boolean).join(", ") || "location unknown"}
          </span>
          <Status s={cam.status} />
          <span className="grow" />
          <button className={boxes ? "primary sm" : "ghost sm"} onClick={() => setBoxes((b) => !b)}
                  aria-pressed={boxes}
                  title="Draw what the detector is finding on this camera now (B). On a recorded feed these do not correspond to the frame on screen.">
            Boxes
          </button>
          <span className="tiny faint">{at >= 0 ? `${at + 1} / ${order.length}` : ""}</span>
          <button className="ghost sm" onClick={() => step(1)} title="Next camera (→)"
                  aria-label="Next camera">▶</button>
          <Help>
            <b>←</b> / <b>→</b> previous or next camera · <b>B</b> detector boxes ·
            {" "}<b>F</b> fullscreen · <b>Space</b> play or pause · <b>Esc</b> back.
          </Help>
        </header>

        <div className="cam-screen">
          <Player cameraRef={ref} zones={zones} detections={boxes ? dets : undefined} />
        </div>

        {/* What is true *now*. Four figures, not thirteen.

            This strip carried resolution, codec, protocol, relay, detections,
            tracks, plates, open events, ANPR tier, read rate, median vehicle
            width, last-indexed and zone count — thirteen numbers, of which
            twelve describe the camera's history or its spec sheet and do not
            change while you watch. Reading them is a separate job from watching
            the picture, so they moved below the fold and this strip answers only
            the question an operator has with their eyes on the feed: is the
            detector working, and what is it seeing? */}
        <div className="cam-live" role="group" aria-label="Live detector status">
          <Fact
            v={seen.running ? `${seen.onScreen}` : "—"}
            l={seen.running ? "on screen" : "detector idle"}
            tone={seen.running ? "ok" : "dim"}
            help="Objects the detector has reported in the last four seconds — the same window the boxes fade over, so this figure and the picture always agree." />
          <Fact v={seen.running ? num(seen.plates) : "—"} l="plates this minute" />
          <Fact v={num(act?.open_event_count ?? 0)} l="open events"
                tone={(act?.open_event_count ?? 0) > 0 ? "hot" : undefined} />
          <Fact v={cam.has_stream ? "yes" : "no"} l="relay"
                tone={cam.has_stream ? "ok" : "dim"} />
          <span className="grow" />
          {/* The honest empty state. Nothing is broken when this shows — the
              inference worker simply is not pointed at this camera, and saying
              so beats a picture with no boxes that looks like a detector which
              has stopped noticing anything. */}
          {boxes && !seen.running && (
            <span className="tiny faint cam-idle">
              No detector running on this camera. Start one with{" "}
              <code>CAMERAS={cam.external_id} .venv/bin/python -m ml.worker</code>.
            </span>
          )}
        </div>

        {/* The plates this camera has read, newest first. Horizontal because it
            is scanned, not read — and it is the answer to the question an
            officer opens a camera with. */}
        <div className="cam-plates">
          <div className="cam-plates-head">
            <b>Recent plate reads</b>
            <Help>
              The track-level answer, not a single frame. Confidence bands are
              measured: act on the green ones, confirm the rest against the crop.
            </Help>
            <span className="grow" />
            <Link className="tiny" to={`/search?camera=${ref}`}>Search this camera →</Link>
          </div>
          <div className="cam-plates-rail">
            {tracks.isLoading && <span className="tiny faint">Loading…</span>}
            {!tracks.isLoading && plated.length === 0 && (
              <span className="tiny faint">No track has carried a plate read here.</span>
            )}
            {plated.map((t) => (
              <Link key={t.id} to={`/investigate?track=${t.id}`} className="plate-card">
                <Thumb src={t.thumbnail_url} label={t.class ?? "crop"} zoomable={false} />
                <Plate text={t.plate_text} trust={t.plate_trust}
                       origin={t.plate_origin} rtoKnown={t.rto_known} />
                <span className="tiny faint trunc">
                  {hhmmss(t.first_seen)} · {t.plate_read_count} reads
                </span>
                {/* What the vehicle looked like, with the parts this system is
                    unsure of marked — a plate beside a contested colour is a
                    different lead from a plate beside a confident one. */}
                <span className="tiny trunc">
                  <Appearance colour={t.dominant_color} klass={t.class}
                              colourUncertain={t.colour_uncertain}
                              classUncertain={t.class_uncertain} />
                </span>
              </Link>
            ))}
          </div>
        </div>
      </div>

      {/* ── Below the fold: what has been indexed ───────────────────────── */}
      <div className="cam-detail">
        {/* The spec sheet and the totals. These were in the live strip, where
            they competed with the picture for attention and changed roughly
            never. Here they are a thing you read once when you want them. */}
        <section className="card">
          <header>
            <b>This camera</b>
            <Help>
              What the relay delivers and what the archive holds. Separate from
              the strip above the fold, which is only about right now.
            </Help>
          </header>
          <div className="body cam-spec">
            <Spec l="Resolution" v={stream?.resolution ?? "—"} />
            <Spec l="Codec" v={(stream?.codec ?? "—").toUpperCase()} />
            <Spec l="Protocol" v={(stream?.protocol ?? "—").toUpperCase()} />
            <Spec l="Zones" v={num(zones.length)} />
            <Spec l="Detections" v={num(act?.detection_count ?? 0)} />
            <Spec l="Tracks" v={num(act?.track_count ?? 0)} />
            <Spec l="Plates read" v={num(act?.plate_count ?? 0)} />
            <Spec l="Indexed to" v={act?.last_ts ? ago(act.last_ts) : "never"} />
            <Spec l="ANPR" v={anpr?.tier ? titleCase(anpr.tier.replace(/_/g, " ")) : "—"}
                  help="Measured from what this camera actually delivered, not a spec sheet. A plate needs roughly 90 px of width to read." />
            <Spec l="Read rate" v={anpr?.read_rate != null ? pct(anpr.read_rate, 1) : "—"} />
            <Spec l="Median vehicle"
                  v={anpr?.median_vehicle_width_px ? `${Math.round(anpr.median_vehicle_width_px)} px` : "—"}
                  help="Half the vehicles seen here are narrower than this. Below about 90 px a plate cannot be resolved at all." />
          </div>
        </section>

        <section className="card">
          <header>
            <b>Indexed activity</b>
            <Help>
              Detections per bucket over recorded footage. Separate from the feed
              above: indexing is a batch pass, so a camera can be streaming and
              contribute nothing to search.
            </Help>
            <span className="grow" />
            <select value={hours} onChange={(e) => setHours(Number(e.target.value))}
                    aria-label="Window">
              {[3, 6, 12, 24].map((h) => <option key={h} value={h}>{h}h</option>)}
            </select>
            <select value={bucket} onChange={(e) => setBucket(Number(e.target.value))}
                    aria-label="Bucket size">
              {[5, 15, 30, 60].map((m) => <option key={m} value={m}>{m}m</option>)}
            </select>
          </header>
          <div className="body">
            {tl.isLoading && <Skeleton rows={3} />}
            {(tl.data?.buckets ?? []).length === 0 && !tl.isLoading && (
              <Empty>Nothing indexed in this window.</Empty>
            )}
            <div className="col" style={{ gap: 4 }}>
              {(tl.data?.buckets ?? []).map((b: any) => (
                <div key={b.bucket} className="col" style={{ gap: 2 }}>
                  <div className="row tiny">
                    <span className="grow faint">{stamp(b.bucket)}</span>
                    <span className="mono faint">{num(b.detections)}</span>
                  </div>
                  <Bar value={b.detections}
                       max={Math.max(1, ...(tl.data?.buckets ?? []).map((x: any) => x.detections ?? 0))} />
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="card">
          <header><b>Reachable next</b>
            <Help>
              Cameras a vehicle leaving here could arrive at, ranked by the
              transition graph. Used to seed a reconstruction.
            </Help>
            <span className="grow" />
            <Link className="tiny" to={`/investigate?camera=${ref}`}>Open the walk →</Link>
          </header>
          <div className="body col" style={{ gap: 6 }}>
            {neighbours.length === 0 && <Empty>No modelled route out of this camera.</Empty>}
            {neighbours.map((n: any) => (
              <Link key={n.external_id ?? n.dst} to={`/cameras/${n.external_id ?? n.dst}`}
                    className="row small" style={{ gap: 8, color: "inherit" }}>
                <span className="mono">{n.external_id ?? n.dst}</span>
                <span className="grow trunc dim">{n.name ?? ""}</span>
                {n.median_s != null && <span className="tiny faint">~{dur(n.median_s)}</span>}
                {n.evidence && <Pill>{n.evidence}</Pill>}
              </Link>
            ))}
          </div>
        </section>

        <section className="card">
          <header><b>Zones</b>
            <Help>The geometry the detector rules are evaluated against on this camera.</Help>
            <span className="grow" />
            <Link className="tiny" to="/zones">Edit zones →</Link>
          </header>
          <div className="body col" style={{ gap: 6 }}>
            {zones.length === 0 && <Empty>No zone drawn on this camera.</Empty>}
            {zones.map((z: any) => (
              <div key={z.id} className="row small" style={{ gap: 8 }}>
                <span className="grow trunc">{z.name}</span>
                <Pill>{z.kind}</Pill>
                {z.created_by === "derived" && <span className="tiny faint">derived</span>}
              </div>
            ))}
          </div>
        </section>

        <section className="card">
          <header><b>Recent events</b>
            <span className="grow" />
            <Link className="tiny" to={`/timeline?camera=${ref}`}>Timeline →</Link>
          </header>
          <div className="list">
            {recent.length === 0 && <Empty>No event from this camera yet.</Empty>}
            {recent.map((e: any) => (
              <Link key={e.id} to={`/timeline/${e.id}`} className="item" style={{ color: "inherit" }}>
                <SevDot s={e.severity} />
                <Thumb src={e.thumbnail_url} className="sm" zoomable={false}
                       label={titleCase(e.event_type)} />
                <span className="grow col" style={{ gap: 1 }}>
                  <span className="t trunc">{e.label}</span>
                  <span className="m">{stamp(e.started_at)}</span>
                </span>
                <Status s={e.status} />
              </Link>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

/** One label/value pair in the detail grid below the fold. */
function Spec({ l, v, help }: { l: string; v: React.ReactNode; help?: string }) {
  return (
    <div className="cam-spec-row">
      <span>{l}{help ? <Help>{help}</Help> : null}</span>
      <b>{v}</b>
    </div>
  );
}

/** One figure in the horizontal status strip. */
function Fact({ v, l, tone, help }:
  { v: React.ReactNode; l: string; tone?: "ok" | "hot" | "dim"; help?: string }) {
  return (
    <div className={`cam-fact ${tone ?? ""}`}>
      <b>{v}</b>
      <span>{l}{help ? <Help>{help}</Help> : null}</span>
    </div>
  );
}
