import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Zone } from "../lib/types";

/**
 * The unified viewer's video element.
 *
 * hls.js is imported lazily *inside* the effect, not at module scope, so a route that
 * merely links to a camera does not pull the decoder. Safari plays HLS natively and is
 * given the URL directly — loading hls.js there would be 130 kB spent to reimplement
 * something the platform already does better.
 *
 * `probe` runs first and decides what to render. The relay reports upstream trouble as a
 * structured reason rather than a broken player, and this surfaces it verbatim: right
 * now the sandbox gateway refuses this service's login with a 403, so `reason` is
 * `gateway_auth` and there is nothing a retry will fix. Telling the operator that is
 * more useful than an endless spinner over a black rectangle.
 */

type Probe = {
  camera: string; playable: boolean; reason: string | null; detail?: string | null;
  authenticated: boolean; segments: number; encrypted: boolean; latency_ms: number | null;
  relay_url: string;
};

const REASON: Record<string, string> = {
  gateway_auth: "The departmental gateway refused this service's login. Credentials or IP allow-listing, upstream — not the console.",
  no_stream: "This camera has no ingest URL on record, so there is nothing to relay.",
  not_configured: "No gateway is configured for this deployment.",
  upstream_error: "The gateway answered, but not with a playlist.",
  timeout: "The gateway did not answer inside the playlist budget.",
};

export function Player({ cameraRef, zones }: { cameraRef: string; zones?: Zone[] }) {
  const video = useRef<HTMLVideoElement>(null);
  const [err, setErr] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);

  const probe = useQuery({
    queryKey: ["probe", cameraRef],
    queryFn: ({ signal }) => api.get<Probe>(`/stream/${cameraRef}/probe`, undefined, signal),
    staleTime: 30_000,
    retry: false,
  });

  const url = probe.data?.relay_url ?? `/api/stream/${cameraRef}/index.m3u8`;
  const playable = probe.data?.playable === true;

  useEffect(() => {
    const el = video.current;
    if (!el || !playable) return;
    setErr(null);

    let hls: import("hls.js").default | null = null;
    let dead = false;

    (async () => {
      if (el.canPlayType("application/vnd.apple.mpegurl")) {
        el.src = url;
        return;
      }
      const { default: Hls } = await import("hls.js");
      if (dead || !Hls.isSupported()) {
        setErr("This browser cannot play HLS.");
        return;
      }
      hls = new Hls({
        // Live-edge tuning. The default 30 s back-buffer is for VOD; on a live wall it
        // just delays recovery after a stall and holds memory per open player.
        lowLatencyMode: true,
        backBufferLength: 12,
        maxBufferLength: 8,
        liveSyncDurationCount: 3,
        enableWorker: true,
      });
      hls.on(Hls.Events.ERROR, (_e, data) => {
        if (!data.fatal) return;
        // A fatal network error on a relayed live playlist is usually the upstream
        // session expiring. One reload attempt, then say so — an automatic retry loop
        // against a gateway that is refusing us is just load.
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls?.startLoad();
        else setErr(`${data.details}`);
      });
      hls.loadSource(url);
      hls.attachMedia(el);
    })();

    return () => {
      dead = true;
      hls?.destroy();
      el.removeAttribute("src");
      el.load();
    };
  }, [url, playable]);

  const reason = probe.data?.reason;

  return (
    <div className="player">
      <video ref={video} controls muted playsInline
             onPlaying={() => setPlaying(true)} onPause={() => setPlaying(false)} />
      {zones?.length ? <ZoneOverlay zones={zones} /> : null}
      <div className="hud">
        {playable && playing && <span className="pill ok">live</span>}
        {probe.data?.encrypted && <span className="pill accent" title="AES-128 playlist">encrypted</span>}
        {probe.data?.latency_ms != null && (
          <span className="pill" title="Gateway round trip for the playlist">
            {Math.round(probe.data.latency_ms)} ms
          </span>
        )}
      </div>
      {(!playable || err) && (
        <div className="overlay">
          <div>
            {probe.isLoading ? (
              "Probing the departmental gateway…"
            ) : (
              <>
                <div><b>{err ? "Playback stopped" : "Not playable right now"}</b></div>
                <div className="dim" style={{ marginTop: 6, maxWidth: 460 }}>
                  {err ?? (reason ? (REASON[reason] ?? reason) : "The relay reported no playable playlist.")}
                </div>
                {probe.data?.detail && (
                  <div className="tiny faint mono" style={{ marginTop: 8, maxWidth: 460, wordBreak: "break-word" }}>
                    {probe.data.detail}
                  </div>
                )}
                <button className="sm" style={{ marginTop: 10 }} onClick={() => probe.refetch()}>Probe again</button>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Zones drawn over the frame, in the normalised coordinates they are stored in.
 *
 * An SVG with `viewBox="0 0 1 1"` and `preserveAspectRatio="none"` means the polygon
 * needs no pixel arithmetic and stays correct when the feed changes resolution — which
 * is the whole reason ZoneCreate rejects un-normalised coordinates.
 */
function ZoneOverlay({ zones }: { zones: Zone[] }) {
  const tint: Record<string, string> = {
    restricted: "#f43f5e", direction: "#38bdf8", crowd: "#a78bfa",
    abandon_watch: "#fbbf24", count_line: "#34d399",
  };
  return (
    <svg className="zonelay" viewBox="0 0 1 1" preserveAspectRatio="none">
      {zones.filter((z) => z.active).map((z) => (
        <polygon key={z.id}
                 points={z.polygon.map((p) => p.join(",")).join(" ")}
                 fill={(tint[z.kind] ?? "#64748b") + "22"}
                 stroke={tint[z.kind] ?? "#64748b"}
                 strokeWidth={0.004} vectorEffect="non-scaling-stroke" />
      ))}
    </svg>
  );
}
