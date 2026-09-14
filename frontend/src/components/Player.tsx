import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, getToken, renewToken } from "../lib/api";
import type { DetectionRow, Zone } from "../lib/types";
import { DetectionOverlay } from "./DetectionOverlay";

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

export function Player(
  { cameraRef, zones, detections, active = true, compact = false }:
  {
    cameraRef: string;
    zones?: Zone[];
    /** Detector boxes to draw over the picture. Empty or absent draws nothing. */
    detections?: DetectionRow[];
    /** False tears the decoder down and never creates one.
     *
     *  The wall needs this and nothing else does. Nine simultaneous 1080p HLS
     *  decoders is not a thing a laptop does, and nine independent retry loops
     *  against the police gateway is the failure this player was flooding it
     *  with earlier today, multiplied by nine. A tile that is not decoding
     *  holds no hls.js instance at all — not a paused one. */
    active?: boolean;
    /** Smaller chrome, for a tile rather than a stage. */
    compact?: boolean;
  },
) {
  const video = useRef<HTMLVideoElement>(null);
  const [err, setErr] = useState<string | null>(null);
  const [live, setLive] = useState<boolean | null>(null);
  const [playing, setPlaying] = useState(false);
  const [fitBox, setFitBox] = useState<
    { left: number; top: number; width: number; height: number } | null>(null);

  // Where the picture actually is inside the element.
  //
  // `object-fit: contain` letterboxes the video, so the element and the picture
  // are different rectangles whenever their aspect ratios differ — which is
  // always, on a stage sized to the viewport. Anything drawn over the frame has
  // to be positioned against the picture, not the element. Publishing that one
  // rectangle here means each overlay can use the frame's own coordinates and
  // none of them has to know about letterboxing.
  //
  // This is also the fix for the zone polygons, which were stretched across the
  // element and so sat off by the size of the letterbox on every camera.
  useEffect(() => {
    const el = video.current;
    if (!el) return;
    const measure = () => {
      const { videoWidth: vw, videoHeight: vh, clientWidth: bw, clientHeight: bh } = el;
      if (!vw || !vh || !bw || !bh) { setFitBox(null); return; }
      const s = Math.min(bw / vw, bh / vh);
      const w = vw * s, h = vh * s;
      setFitBox({ left: (bw - w) / 2, top: (bh - h) / 2, width: w, height: h });
    };
    measure();
    // `resize` on a media element fires when the intrinsic size arrives or
    // changes level; ResizeObserver covers the element being laid out
    // differently — fullscreen, a density change, a dragged window.
    el.addEventListener("loadedmetadata", measure);
    el.addEventListener("resize", measure);
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => {
      el.removeEventListener("loadedmetadata", measure);
      el.removeEventListener("resize", measure);
      ro.disconnect();
    };
  }, []);

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
    if (!el || !playable || !active) return;
    setErr(null);

    let hls: import("hls.js").default | null = null;
    let dead = false;
    // One renewal per player instance. If a freshly-minted token is also
    // refused, the session is genuinely over and retrying cannot help.
    let renewed = false;
    // And one restart after a fatal network error. `startLoad()` restarts
    // hls.js's whole retry cycle, so calling it on every fatal error is an
    // unbounded loop wearing the costume of a recovery: four wall tiles against
    // an archive with missing segments produced 87 requests for 10 segments,
    // 38 of them 404, in nine seconds. A gateway that is answering 404 will
    // answer 404 again.
    let restarts = 0;

    (async () => {
      // hls.js first, native HLS only as the fallback.
      //
      // The order used to be the other way round, on the reasonable-sounding
      // basis that a browser's own decoder is cheaper than 130 kB of JavaScript.
      // Chrome answers `canPlayType("application/vnd.apple.mpegurl")` with
      // "maybe" — truthy — so it took that branch, and then the media element
      // fetched the playlist's key and segment URLs itself, with no way to
      // attach a header to either. The playlist loaded and every segment under
      // it returned 401: a black player and no error, for a camera that was
      // fine.
      //
      // hls.js issues those sub-requests through `xhrSetup` below, so one hook
      // authenticates the playlist, the AES key and every segment, with the
      // token in a header where it belongs.
      const { default: Hls } = await import("hls.js");
      if (dead) return;
      if (!Hls.isSupported()) {
        // Genuine native HLS (Safari, iOS). No hook exists to add a header, so
        // the token travels in the query string — the same trade the SSE stream
        // makes, for the same reason, and confined to the one browser that
        // needs it. The relay propagates it onto the URLs it rewrites.
        if (el.canPlayType("application/vnd.apple.mpegurl")) {
          const t = getToken();
          el.src = t
            ? `${url}${url.includes("?") ? "&" : "?"}access_token=${encodeURIComponent(t)}`
            : url;
          return;
        }
        setErr("This browser cannot play HLS.");
        return;
      }
      hls = new Hls({
        // Tuned for whichever shape the gateway actually serves.
        //
        // This was configured for a live edge — `lowLatencyMode`, a 12 s back
        // buffer, `maxBufferLength: 8`, `liveSyncDurationCount: 3`. The gateway
        // serves `#EXT-X-PLAYLIST-TYPE:VOD`: 4,306 segments, twelve hours, with
        // an ENDLIST. It is a recorded archive, not a live stream.
        //
        // The fatal part was `maxBufferLength: 8` against a
        // `#EXT-X-TARGETDURATION:10`. hls.js will not buffer past its limit, and
        // the limit was smaller than a single segment, so playback stalled
        // before it began — a black player with no error.
        //
        // 30 s is comfortably above any sane target duration and is fine on a
        // live edge too; hls.js applies its own live-window logic when the
        // playlist says it is live, which is the right place for that decision
        // rather than a constant chosen for one gateway.
        maxBufferLength: 30,
        backBufferLength: 30,
        lowLatencyMode: false,
        enableWorker: true,
        // Bound the retries. hls.js will otherwise re-request a failing segment
        // indefinitely: an expired token produced 195 requests for four
        // segments — 49 attempts each — against the police gateway, from one
        // open tab. A player left open is not allowed to become a load
        // generator.
        fragLoadPolicy: {
          default: {
            maxTimeToFirstByteMs: 10_000,
            maxLoadTimeMs: 60_000,
            timeoutRetry: { maxNumRetry: 2, retryDelayMs: 1000, maxRetryDelayMs: 8000 },
            errorRetry: { maxNumRetry: 3, retryDelayMs: 1000, maxRetryDelayMs: 8000 },
          },
        },
        // Every request hls.js makes — playlist, segments and the AES key —
        // goes through here, so one hook authenticates all three. Without it
        // the relay answered 401 and the player showed an empty black box with
        // no explanation, which reads as a dead camera rather than a dead
        // session.
        xhrSetup: (xhr: XMLHttpRequest) => {
          const t = getToken();
          if (t) xhr.setRequestHeader("Authorization", `Bearer ${t}`);
        },
      });
      hls.on(Hls.Events.ERROR, (_e, data) => {
        // 401 first, and on non-fatal errors too.
        //
        // The operator's access token lives fifteen minutes. hls.js requests do
        // not go through the API client, so nothing was triggering the silent
        // renewal that every other request gets — `xhrSetup` kept attaching the
        // same dead token and hls.js kept retrying. Renewing here, once, and
        // then resuming is what makes a player survive its own session.
        const code = (data as { response?: { code?: number } }).response?.code;
        if (code === 401 || code === 403) {
          if (renewed) { setErr("Your session ended. Sign in again to keep watching."); hls?.stopLoad(); return; }
          renewed = true;
          renewToken().then((ok) => {
            if (dead) return;
            if (ok) hls?.startLoad();
            else { setErr("Your session ended. Sign in again to keep watching."); hls?.stopLoad(); }
          });
          return;
        }
        if (!data.fatal) return;
        // A fatal network error that is not an auth failure is the gateway
        // itself. One reload attempt, then say so — retrying against something
        // that is refusing us is just load.
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          if (restarts >= 1) {
            hls?.stopLoad();
            setErr(code === 404
              ? "The gateway no longer has this part of the recording."
              : "The camera gateway stopped responding.");
            return;
          }
          restarts += 1;
          hls?.startLoad();
        } else setErr(`${data.details}`);
      });
      // The playlist knows whether it is live; nothing else does. Surfaced so
      // the UI can say "LIVE" only when it is true — a VMS must never let an
      // operator mistake recorded footage for a live feed.
      hls.on(Hls.Events.LEVEL_LOADED, (_e, data) => {
        if (!dead) setLive(Boolean((data as { details?: { live?: boolean } }).details?.live));
      });
      hls.loadSource(url);
      hls.attachMedia(el);
      // Autoplay is commonly refused until the user has interacted with the
      // page. Failing silently leaves a loaded-but-paused player that looks
      // broken, so the refusal is caught and the controls are left to the
      // operator rather than retried.
      el.play().catch(() => {});
    })();

    return () => {
      dead = true;
      hls?.destroy();
      el.removeAttribute("src");
      el.load();
    };
  }, [url, playable, active]);

  const reason = probe.data?.reason;

  return (
    <div className={`player${compact ? " compact" : ""}`}>
      <video ref={video} controls muted playsInline
             onPlaying={() => setPlaying(true)} onPause={() => setPlaying(false)} />
      {/* Both overlays live in the picture's rectangle, so each can work in the
          coordinates its data is stored in and neither repeats the fit maths. */}
      {fitBox && (zones?.length || detections?.length) ? (
        <div className="fitbox" style={fitBox}>
          {zones?.length ? <ZoneOverlay zones={zones} /> : null}
          {detections?.length ? <DetectionOverlay detections={detections} /> : null}
        </div>
      ) : null}
      <div className="hud">
        {/* Says what the playlist says, not what the player is doing.
            This used to read "live" whenever anything was playing, and the
            gateway serves `#EXT-X-PLAYLIST-TYPE:VOD` — twelve hours of recorded
            segments with an ENDLIST. An operator cannot be allowed to mistake
            an archive for a live feed; that confusion is the one a VMS exists to
            prevent. When a genuinely live feed arrives the playlist says so and
            this turns green on its own. */}
        {playable && playing && (
          live === true ? <span className="pill ok">● live</span>
          : live === false ? <span className="pill" title="Recorded footage relayed from the gateway, not a live edge">recorded</span>
          : <span className="pill">playing</span>
        )}
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
