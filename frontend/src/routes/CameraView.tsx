import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useCamera, useCameraTimeline, useTracks } from "../lib/queries";
import { ago, dur, fullStamp, hhmmss, num, pct, stamp, titleCase } from "../lib/format";
import { Appearance, Bar, Empty, ErrorBox, Pill, SevDot, Skeleton, Status, Thumb } from "../components/ui";
import { Player } from "../components/Player";

/**
 * One camera: the live viewer, what has been indexed from it, and where a vehicle
 * leaving it can go next.
 *
 * The indexing histogram is the honest answer to "is this camera covered". A camera can
 * be `active`, have a working relay and still have contributed nothing to the index,
 * because indexing is a batch pass over recorded footage and the two are independent.
 * Showing detections-per-bucket next to the player keeps those separate in the
 * operator's head instead of implying that a playing feed is a searched feed.
 */
export default function CameraView() {
  const { ref = "" } = useParams();
  const q = useCamera(ref);
  const [bucket, setBucket] = useState(15);
  const [hours, setHours] = useState(6);
  const tl = useCameraTimeline(ref, { bucket_minutes: bucket, hours });

  const cam = q.data?.camera;
  const act = q.data?.activity;
  const zones = q.data?.zones ?? [];
  const neighbours = q.data?.neighbours ?? [];
  const recent = q.data?.recent_events ?? [];

  const tracks = useTracks({ camera: ref, limit: 12, has_plate: true });

  const peak = useMemo(
    () => Math.max(1, ...(tl.data?.buckets ?? []).map((b: any) => b.detections ?? 0)),
    [tl.data],
  );

  if (q.isError) return <div className="view"><ErrorBox error={q.error} what={`Camera ${ref}`} /></div>;
  if (q.isLoading || !cam) return <div className="view"><Skeleton rows={10} /></div>;

  return (
    <div className="view col" style={{ gap: 14 }}>
      <div className="row wrap" style={{ gap: 10 }}>
        <div>
          <div className="row" style={{ gap: 8 }}>
            <b style={{ fontSize: 16 }}>{cam.name}</b>
            <span className="mono dim">{cam.external_id}</span>
            <Status s={cam.status} />
          </div>
          <div className="tiny faint">
            {[cam.area, cam.district].filter(Boolean).join(", ") || "location unknown"}
            {cam.lat != null && ` · ${cam.lat.toFixed(4)}, ${cam.lng?.toFixed(4)}`}
            {cam.geocode_precision && ` · ${cam.geocode_precision}-level geocode`}
          </div>
        </div>
        <div className="right row wrap tiny" style={{ gap: 6 }}>
          <Pill k="accent">{titleCase(cam.department ?? "")}</Pill>
          <span className="pill">{String(cam.metadata?.source_system ?? "unknown system")}</span>
          <span className="pill mono">{cam.camera_uid ?? "no UID"}</span>
          <span className="pill">{String(cam.metadata?.stream_protocol ?? "—").toUpperCase()}</span>
        </div>
      </div>

      <div className="split">
        <div className="l col" style={{ gap: 14 }}>
          <Player cameraRef={ref} zones={zones} />

          <section className="card">
            <header>
              <b>Indexed activity</b>
              <span className="grow" />
              <select className="sm" value={hours} onChange={(e) => setHours(+e.target.value)}
                      aria-label="Window">
                {[1, 6, 24, 72, 168].map((h) => <option key={h} value={h}>{h}h</option>)}
              </select>
              <select className="sm" value={bucket} onChange={(e) => setBucket(+e.target.value)}
                      aria-label="Bucket size">
                {[1, 5, 15, 60].map((m) => <option key={m} value={m}>{m} min</option>)}
              </select>
            </header>
            <div className="body">
              {tl.isLoading && <Skeleton rows={3} />}
              {(tl.data?.buckets ?? []).length === 0 && !tl.isLoading && (
                <Empty>Nothing indexed from this camera in the last {hours}h. Run the batch indexer to cover it.</Empty>
              )}
              <div className="row" style={{ alignItems: "flex-end", gap: 2, height: 90 }}>
                {(tl.data?.buckets ?? []).map((b: any) => (
                  <div key={b.bucket} title={`${fullStamp(b.bucket)}\n${num(b.detections)} detections · ${num(b.tracks)} tracks · ${num(b.plates)} plates`}
                       style={{
                         flex: 1, minWidth: 2, borderRadius: 2,
                         height: `${Math.max(2, (b.detections / peak) * 100)}%`,
                         background: b.plates > 0 ? "var(--accent)" : "var(--accent-dim)",
                       }} />
                ))}
              </div>
              {(tl.data?.buckets ?? []).length > 1 && (
                <div className="row tiny faint" style={{ marginTop: 4 }}>
                  <span>{hhmmss(tl.data.buckets[0].bucket)}</span>
                  <span className="right">{hhmmss(tl.data.buckets[tl.data.buckets.length - 1].bucket)}</span>
                </div>
              )}
              <div className="tiny faint" style={{ marginTop: 6 }}>
                Bars are detections per {bucket}-minute bucket; brighter bars are buckets that also yielded a plate read.
              </div>
            </div>
          </section>

          <section className="card">
            <header><b>Recent plate reads</b>
              <span className="grow" />
              <Link className="tiny" to={`/search?camera=${ref}`}>Search this camera →</Link>
            </header>
            <div className="body">
              {tracks.isLoading && <Skeleton rows={3} />}
              {(tracks.data?.items ?? []).length === 0 && !tracks.isLoading && (
                <Empty>No track carried a plate read here.</Empty>
              )}
              <div className="grid g3">
                {(tracks.data?.items ?? []).map((t) => (
                  <Link key={t.id} to={`/investigate?track=${t.id}`} className="item col"
                        style={{ gap: 5, color: "inherit" }}>
                    <Thumb src={t.thumbnail_url} label={t.class ?? "crop"} />
                    <div className="row" style={{ gap: 6 }}>
                      <b className="mono">{t.plate_text}</b>
                      {t.plate_confidence != null && (
                        <span className="tiny faint">{pct(t.plate_confidence, 0)}</span>
                      )}
                      <span className="right tiny faint">{t.plate_read_count} reads</span>
                    </div>
                    <div className="tiny faint trunc">
                      <Appearance colour={t.dominant_color} klass={t.class}
                                  colourUncertain={t.colour_uncertain} classUncertain={t.class_uncertain} /> · {stamp(t.first_seen)} · {dur(t.duration_s)}
                    </div>
                  </Link>
                ))}
              </div>
            </div>
          </section>
        </div>

        <div className="r col" style={{ gap: 14 }}>
          <section className="card">
            <header><b>Index</b></header>
            <div className="body">
              <dl className="kv">
                <dt>Detections</dt><dd>{num(act?.detection_count ?? 0)}</dd>
                <dt>Tracks</dt><dd>{num(act?.track_count ?? 0)}</dd>
                <dt>Plate reads</dt><dd>{num(act?.plate_count ?? 0)}</dd>
                <dt>Events</dt><dd>{num(act?.event_count ?? 0)}</dd>
                <dt>Open</dt><dd>{num(act?.open_event_count ?? 0)}</dd>
                <dt>Zones</dt><dd>{num(act?.zone_count ?? 0)}</dd>
                <dt>First</dt><dd>{act?.first_ts ? stamp(act.first_ts) : "—"}</dd>
                <dt>Last</dt><dd>{act?.last_ts ? `${stamp(act.last_ts)} (${ago(act.last_ts)})` : "—"}</dd>
              </dl>
              {Object.keys(act?.class_mix ?? {}).length > 0 && (
                <>
                  <hr className="sep" />
                  <div className="tiny faint" style={{ marginBottom: 5 }}>Class mix</div>
                  {Object.entries(act.class_mix as Record<string, number>)
                    .sort((a, b) => b[1] - a[1]).map(([k, v]) => (
                      <div key={k} className="row tiny" style={{ gap: 6 }}>
                        <span style={{ width: 88 }} className="trunc">{k}</span>
                        <Bar value={v} max={Math.max(...Object.values(act.class_mix as Record<string, number>))} />
                        <span className="mono" style={{ width: 40, textAlign: "right" }}>{num(v)}</span>
                      </div>
                    ))}
                </>
              )}
            </div>
          </section>

          <section className="card">
            <header><b>Reachable next</b><span className="grow" />
              <Link className="tiny" to={`/investigate?from=${ref}`}>Walk →</Link>
            </header>
            <div className="body col" style={{ gap: 7 }}>
              {neighbours.length === 0 && (
                <Empty>No transition edge from this camera. Rebuild the graph after indexing.</Empty>
              )}
              {neighbours.map((n: any) => (
                <Link key={n.camera_id} to={`/cameras/${n.external_id}`} className="item col"
                      style={{ gap: 3, color: "inherit" }}>
                  <div className="row" style={{ gap: 6 }}>
                    <span className="mono">{n.external_id}</span>
                    <span className="trunc dim tiny">{n.name}</span>
                    <span className="right">
                      <Pill k={n.evidence === "confirmed" ? "ok" : n.evidence === "plate" ? "accent" : undefined}>
                        {n.evidence}
                      </Pill>
                    </span>
                  </div>
                  <div className="tiny faint">
                    {dur(n.median_transit_s)} typical · window {dur(n.p10_transit_s)}–{dur(n.p90_transit_s)}
                    {n.distance_m != null && ` · ${(n.distance_m / 1000).toFixed(1)} km`}
                    {n.observations ? ` · n=${n.observations}` : " · no sightings"}
                  </div>
                </Link>
              ))}
            </div>
          </section>

          <section className="card">
            <header><b>Zones</b><span className="grow" />
              <Link className="tiny" to={`/zones?camera=${ref}`}>Edit →</Link>
            </header>
            <div className="body col" style={{ gap: 6 }}>
              {zones.length === 0 && <Empty>No zone drawn, so the zone-scoped rules cannot fire here.</Empty>}
              {zones.map((z: any) => (
                <div key={z.id} className="row tiny" style={{ gap: 6 }}>
                  <span className={`dot ${z.active ? "low" : "info"}`} />
                  <span className="trunc">{z.name}</span>
                  <span className="right"><Pill>{z.kind}</Pill></span>
                </div>
              ))}
            </div>
          </section>

          <section className="card">
            <header><b>Recent events</b><span className="grow" />
              <Link className="tiny" to={`/timeline?camera=${ref}`}>All →</Link>
            </header>
            <div className="body col" style={{ gap: 6 }}>
              {recent.length === 0 && <Empty>No event on this camera.</Empty>}
              {recent.map((e: any) => (
                <Link key={e.id} to={`/timeline/${e.id}`} className="item row"
                      style={{ gap: 7, color: "inherit" }}>
                  <SevDot s={e.severity} />
                  <span className="trunc grow">{e.label}</span>
                  <span className="tiny faint">{hhmmss(e.started_at)}</span>
                </Link>
              ))}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
