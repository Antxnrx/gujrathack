import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useCameras, useTracks } from "../lib/queries";
import { Empty, Help, Skeleton, Thumb } from "../components/ui";
import { Player } from "../components/Player";
import type { Camera } from "../lib/types";

/**
 * The camera wall.
 *
 * What makes this hard is not the grid, it is the decoders. Nine simultaneous
 * 1080p HLS streams is not something a laptop does, and a real VMS solves it
 * with sub-streams — a low-resolution second encode published by the camera for
 * exactly this view. This estate publishes one stream per camera at full
 * resolution, so that option does not exist and pretending otherwise would give
 * an operator a wall that melts their machine.
 *
 * So decoding is explicit and budgeted. Every tile starts as a *still* — the
 * most recent crop the pipeline stored for that camera, which is a real frame
 * from that camera and is labelled as a still rather than passed off as live.
 * Turning a tile live starts one decoder; the budget caps how many can run at
 * once, and the oldest is turned back to a still when the cap is reached rather
 * than silently refusing. An operator watching four feeds and wanting a fifth
 * gets the fifth.
 *
 * The tab going to the background stops every decoder. A wall left open on a
 * second monitor is the normal case, and nine feeds decoding behind a window
 * nobody is looking at is pure waste — and, after this morning, nine retry loops
 * pointed at a police gateway if anything goes wrong.
 *
 * Keyboard, as everywhere else here:
 *   1 4 9   grid size            ← ↑ → ↓  move focus
 *   enter   open that camera     l        toggle live on the focused tile
 *   f       fullscreen it        esc      back to the index
 *
 * The layout is remembered per browser, because a wall is something an operator
 * arranges once for their post and expects to find again.
 */

type Grid = 1 | 4 | 9;
const STORE = "setu.wall";

/** How many tiles may decode at once. Four 1080p HLS streams is already a lot
 *  of a laptop; beyond that the wall stops being usable for the thing it is
 *  for, which is noticing something. */
const LIVE_BUDGET = 4;

type Saved = { grid: Grid; slots: (string | null)[] };

function load(): Saved {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE) || "");
    if (raw && [1, 4, 9].includes(raw.grid) && Array.isArray(raw.slots)) return raw;
  } catch { /* a corrupt or absent layout is not an error, it is a first visit */ }
  return { grid: 4, slots: [] };
}

export default function Wall() {
  const nav = useNavigate();
  const cams = useCameras();
  const [grid, setGrid] = useState<Grid>(() => load().grid);
  const [slots, setSlots] = useState<(string | null)[]>(() => load().slots);
  const [liveSlots, setLiveSlots] = useState<number[]>([]);
  const [focus, setFocus] = useState(0);
  const [hidden, setHidden] = useState(() => document.hidden);

  const all = useMemo(
    () => [...(cams.data?.items ?? [])].sort((a, b) => a.external_id.localeCompare(b.external_id)),
    [cams.data],
  );

  // Fill any empty slot from the estate, so a first visit shows cameras rather
  // than a grid of dropdowns.
  const filled = useMemo(() => {
    const out = Array.from({ length: grid }, (_, i) => slots[i] ?? null);
    const used = new Set(out.filter(Boolean));
    let next = 0;
    for (let i = 0; i < out.length; i++) {
      if (out[i]) continue;
      while (next < all.length && used.has(all[next].external_id)) next++;
      out[i] = all[next]?.external_id ?? null;
      if (out[i]) used.add(out[i] as string);
    }
    return out;
  }, [grid, slots, all]);

  useEffect(() => {
    localStorage.setItem(STORE, JSON.stringify({ grid, slots: filled }));
  }, [grid, filled]);

  // Nothing decodes behind a hidden tab.
  useEffect(() => {
    const onVis = () => setHidden(document.hidden);
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  const toggleLive = useCallback((slot: number) => {
    setLiveSlots((cur) => {
      if (cur.includes(slot)) return cur.filter((s) => s !== slot);
      // At the budget, the longest-running tile gives up its decoder. Refusing
      // would be the easy behaviour and the wrong one: the operator asking for
      // a fifth feed wants the fifth feed.
      const next = [...cur, slot];
      return next.length > LIVE_BUDGET ? next.slice(next.length - LIVE_BUDGET) : next;
    });
  }, []);

  const cols = grid === 1 ? 1 : grid === 4 ? 2 : 3;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === "1" || k === "4" || k === "9") {
        e.preventDefault(); setGrid(Number(k) as Grid); setFocus(0); setLiveSlots([]);
      } else if (k === "arrowright") { e.preventDefault(); setFocus((f) => (f + 1) % grid); }
      else if (k === "arrowleft") { e.preventDefault(); setFocus((f) => (f - 1 + grid) % grid); }
      else if (k === "arrowdown") { e.preventDefault(); setFocus((f) => (f + cols) % grid); }
      else if (k === "arrowup") { e.preventDefault(); setFocus((f) => (f - cols + grid) % grid); }
      else if (k === "l") { e.preventDefault(); toggleLive(focus); }
      else if (k === "enter") { const c = filled[focus]; if (c) nav(`/cameras/${c}`); }
      else if (k === "escape") nav("/cameras");
      else if (k === "f") {
        e.preventDefault();
        document.querySelectorAll(".wall-tile")[focus]?.requestFullscreen?.().catch(() => {});
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [grid, cols, focus, filled, nav, toggleLive]);

  if (cams.isLoading) return <div className="view"><Skeleton rows={8} /></div>;

  return (
    <div className="wall-page">
      <header className="wall-bar">
        <b>Camera wall</b>
        <Help>
          Tiles start as the most recent stored frame from that camera — a real
          frame, labelled a still. Press <b>L</b> or the tile’s button to start a
          decoder. At most {LIVE_BUDGET} decode at once: this estate publishes
          only full-resolution streams, with no low-resolution sub-stream for a
          wall, so a decoder here costs what a decoder costs.
        </Help>
        <span className="grow" />
        <span className="tiny faint">Grid</span>
        {([1, 4, 9] as const).map((g) => (
          <button key={g} className={grid === g ? "primary sm" : "ghost sm"}
                  onClick={() => { setGrid(g); setFocus(0); setLiveSlots([]); }}
                  title={`${g === 1 ? "single" : g === 4 ? "2 × 2" : "3 × 3"} (press ${g})`}>
            {g === 1 ? "1" : g === 4 ? "2×2" : "3×3"}
          </button>
        ))}
        <span className="wall-sep" />
        <span className="tiny faint">
          {liveSlots.length}/{LIVE_BUDGET} live
          {hidden ? " · paused, tab in background" : ""}
        </span>
        <span className="tiny faint wall-keys">
          ←↑→↓ focus · <b>L</b> live · <b>Enter</b> open · <b>F</b> full
        </span>
      </header>

      <div className={`wall-grid g${grid}`}>
        {filled.map((ext, i) => (
          <WallTile
            key={i}
            slot={i}
            cameraRef={ext}
            cameras={all}
            focused={focus === i}
            live={liveSlots.includes(i) && !hidden}
            onFocus={() => setFocus(i)}
            onToggleLive={() => toggleLive(i)}
            onAssign={(next) => setSlots(() => {
              const copy = [...filled];
              copy[i] = next;
              return copy;
            })}
            onOpen={() => ext && nav(`/cameras/${ext}`)}
          />
        ))}
      </div>
    </div>
  );
}

function WallTile({ slot, cameraRef, cameras, focused, live, onFocus, onToggleLive, onAssign, onOpen }: {
  slot: number;
  cameraRef: string | null;
  cameras: Camera[];
  focused: boolean;
  live: boolean;
  onFocus: () => void;
  onToggleLive: () => void;
  onAssign: (ext: string | null) => void;
  onOpen: () => void;
}) {
  const cam = cameras.find((c) => c.external_id === cameraRef);
  // The still: the latest crop the pipeline stored for this camera. One row, and
  // it is cached across tiles that show the same camera.
  const still = useTracks(cameraRef ? { camera: cameraRef, limit: 1 } : { limit: 0 });
  const shot = still.data?.items?.[0]?.thumbnail_url ?? null;

  return (
    <section className={`wall-tile${focused ? " on" : ""}`} onMouseDown={onFocus}
             aria-label={cam ? `Tile ${slot + 1}: ${cam.external_id}` : `Tile ${slot + 1}, empty`}>
      <header>
        <span className="mono">{cam?.external_id ?? "—"}</span>
        <span className="trunc dim">{cam?.name ?? "empty"}</span>
        <span className="grow" />
        {cam && (cam.open_event_count ?? 0) > 0 && (
          <span className="badge hot">{cam.open_event_count}</span>
        )}
        <button className={live ? "primary sm" : "ghost sm"} onClick={onToggleLive}
                disabled={!cameraRef}
                title={live ? "Stop this decoder (L)" : "Start a decoder for this tile (L)"}>
          {live ? "live" : "still"}
        </button>
      </header>

      <div className="wall-screen">
        {!cameraRef ? (
          <Empty>No camera in this tile.</Empty>
        ) : live ? (
          <Player cameraRef={cameraRef} active compact />
        ) : (
          <button className="wall-still" onClick={onOpen}
                  title="Most recent stored frame — press L for the live feed">
            <Thumb src={shot} zoomable={false}
                   label={still.isLoading ? "…" : "no frame stored"} />
            <span className="wall-still-tag">still</span>
          </button>
        )}
      </div>

      <footer>
        <select value={cameraRef ?? ""} onChange={(e) => onAssign(e.target.value || null)}
                aria-label={`Camera for tile ${slot + 1}`}>
          <option value="">— empty —</option>
          {cameras.map((c) => (
            <option key={c.external_id} value={c.external_id}>
              {c.external_id} — {c.name}
            </option>
          ))}
        </select>
      </footer>
    </section>
  );
}
